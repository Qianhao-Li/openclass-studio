"""YouTube transcripts are versioned source snapshots, never video-frame claims."""
from __future__ import annotations
from dataclasses import asdict, dataclass
import hashlib
import json
import math
from urllib.parse import urlparse
from openclass_core.models import now_iso
from openclass_core.services.youtube_transcript_adapter import YouTubeNoCaptionsError, YouTubeTranscript, YouTubeTranscriptAdapter, YouTubeTranscriptAdapterError, _validate_access
from .config import GoogleSourceError
from .storage import scope_prefix
from .web_snapshot import fetch_public
MAX_PUBLIC_AUDIO_BYTES = 32 * 1024 * 1024
MAX_PUBLIC_AUDIO_SECONDS = 900
AUDIO_MIMES = {'m4a': 'audio/mp4', 'mp3': 'audio/mpeg', 'wav': 'audio/wav', 'flac': 'audio/flac', 'ogg': 'audio/ogg', 'opus': 'audio/ogg', 'webm': 'audio/webm'}

@dataclass(frozen=True)
class PublicYouTubeAudio:
    content: bytes
    file_name: str
    mime_type: str
    metadata: dict

def download_public_audio(info: dict, *, fetch=fetch_public) -> PublicYouTubeAudio:
    """Fetch only the anonymous HTTPS audio URL exposed by the ordinary extractor."""
    _validate_access(info)
    duration = info.get('duration')
    if isinstance(duration, bool) or not isinstance(duration, (int, float)) or (not math.isfinite(duration)) or (not 0 < duration <= MAX_PUBLIC_AUDIO_SECONDS):
        raise YouTubeTranscriptAdapterError('This video has no captions. Google transcription currently accepts public audio with a verifiable duration of up to 15 minutes. Upload captions or split the audio into shorter segments.')
    formats = info.get('formats')
    if not isinstance(formats, list):
        raise YouTubeTranscriptAdapterError('This video has no captions or publicly accessible audio track. Upload captions or audio instead.')
    candidates = []
    for track in formats:
        if not isinstance(track, dict) or track.get('has_drm') or track.get('vcodec') != 'none' or (track.get('acodec') in {None, 'none'}):
            continue
        extension = str(track.get('ext') or '').lower()
        parsed = urlparse(str(track.get('url') or ''))
        host = (parsed.hostname or '').lower()
        if extension not in AUDIO_MIMES or parsed.scheme != 'https' or (not host.endswith('.googlevideo.com')) or parsed.username or parsed.password:
            continue
        if track.get('protocol') not in {None, 'https'}:
            continue
        size = track.get('filesize') or track.get('filesize_approx')
        if isinstance(size, (int, float)) and size > MAX_PUBLIC_AUDIO_BYTES:
            continue
        candidates.append(track)
    if not candidates:
        raise YouTubeTranscriptAdapterError('This video has no usable captions or supported public audio track. Login credentials, decryption, and access-restriction bypasses are not used.')

    def bitrate(track):
        try:
            value = float(track.get('abr') or 0)
            return value if math.isfinite(value) else 0
        except (ValueError, TypeError):
            return 0
    candidates.sort(key=bitrate, reverse=True)
    track = candidates[0]
    try:
        content, _mime, final_uri = fetch(track['url'])
    except Exception as exc:
        raise YouTubeTranscriptAdapterError('The public audio track download failed. Login, region, or CAPTCHA restrictions will not be bypassed. Upload captions or an audio file instead.') from exc
    parsed = urlparse(final_uri)
    if parsed.scheme != 'https' or not (parsed.hostname or '').lower().endswith('.googlevideo.com'):
        raise YouTubeTranscriptAdapterError('The public audio track returned an unsupported redirect.')
    if not content or len(content) > MAX_PUBLIC_AUDIO_BYTES:
        raise YouTubeTranscriptAdapterError('The public audio track is empty or exceeds the 32 MB download limit.')
    extension = str(track['ext']).lower()
    identity = str(info.get('id') or 'video')
    safe_id = ''.join((char for char in identity if char.isalnum() or char in '-_'))[:64] or 'video'
    return PublicYouTubeAudio(content, f'youtube-{safe_id}.{extension}', AUDIO_MIMES[extension], {'transcript_method': 'google_speech', 'audio_format_id': str(track.get('format_id') or ''), 'audio_sha256': hashlib.sha256(content).hexdigest(), 'duration_seconds': duration, 'audio_language': str(track.get('language') or info.get('language') or ''), 'visual_content_processed': False})

def queue_youtube_source(*, service, adapter: YouTubeTranscriptAdapter, owner_user_id: str, package_id: str, source_uri: str, title: str='', source_id: str | None=None, audio_downloader=download_public_audio):
    service.config.validate(write=True)
    try:
        transcript = adapter.extract(source_uri, title=title)
    except YouTubeNoCaptionsError as exc:
        audio = audio_downloader(exc.info)
        source = service.queue_file(owner_user_id=owner_user_id, package_id=package_id, source_id=source_id, file_name=audio.file_name, content=audio.content, mime_type=audio.mime_type, title=title.strip() or str(exc.info.get('title') or source_uri), source_uri=source_uri, origin_metadata={'origin': 'youtube', 'video_id': str(exc.info.get('id') or ''), 'original_url': source_uri, 'snapshot_kind': 'public_audio', 'captured_at': now_iso(), **audio.metadata})
        return service.state.update(source, source_type='video_url', metadata={'media_provider': 'youtube', 'media_kind': 'audio_transcript', 'transcript_method': 'google_speech', 'visual_content_processed': False})
    normalized = _index_text(transcript, source_uri)
    original_caption = transcript.raw_caption.encode('utf-8') if transcript.raw_caption else transcript.text.encode('utf-8')
    caption_hash = hashlib.sha256(original_caption).hexdigest()
    safe_id = ''.join((char for char in transcript.video_id if char.isalnum() or char in '-_'))[:64] or 'video'
    source = service.queue_file(owner_user_id=owner_user_id, package_id=package_id, source_id=source_id, file_name=f'youtube-{safe_id}-transcript.txt', content=normalized.encode('utf-8'), mime_type='text/plain', title=transcript.title, source_uri=source_uri, origin_metadata={'origin': 'youtube', 'video_id': transcript.video_id, 'original_url': source_uri, 'snapshot_kind': 'caption_transcript', 'caption_sha256': caption_hash, 'caption_kind': transcript.metadata.get('transcript_kind', ''), 'caption_format': transcript.caption_format or transcript.metadata.get('transcript_ext', ''), 'language': transcript.language, 'captured_at': now_iso(), 'visual_content_processed': False})
    if source.status == 'ready' and source.metadata.get('search_ready'):
        return source
    prefix = scope_prefix(owner_user_id, package_id, source.id, source.metadata['source_version'])
    timeline = {'schema': 'youtube_timeline_v1', 'video_id': transcript.video_id, 'original_url': source_uri, 'language': transcript.language, 'caption_kind': transcript.metadata.get('transcript_kind', ''), 'caption_sha256': caption_hash, 'segments': [asdict(segment) for segment in transcript.segments]}
    try:
        caption_ref = service.storage.put(bucket=service.config.artifact_bucket, name=f'{prefix}/youtube/caption-{caption_hash}', content=original_caption, mime_type='application/octet-stream')
        source = service.state.update(source, metadata={'google_objects': [*source.metadata.get('google_objects', []), caption_ref], 'youtube_caption': caption_ref})
        timeline_bytes = json.dumps(timeline, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        timeline_ref = service.storage.put(bucket=service.config.artifact_bucket, name=f'{prefix}/youtube/timeline-{hashlib.sha256(timeline_bytes).hexdigest()}.json', content=timeline_bytes, mime_type='application/json')
        source = service.state.update(source, source_type='video_url', metadata={'google_objects': [*source.metadata.get('google_objects', []), timeline_ref], 'youtube_timeline': timeline_ref, 'media_provider': 'youtube', 'media_kind': 'caption_transcript', 'transcript_language': transcript.language, 'transcript_kind': transcript.metadata.get('transcript_kind', ''), 'caption_sha256': caption_hash, 'transcript_segment_count': len(transcript.segments), 'visual_content_processed': False})
    except GoogleSourceError as exc:
        return service.state.update(source, status='failed', error=str(exc), metadata={'storage_status': 'failed', 'search_ready': False})
    return source

def _index_text(transcript: YouTubeTranscript, source_uri: str) -> str:
    if not transcript.segments:
        return transcript.text
    lines = [f'Title: {transcript.title}', f'Source: {source_uri}', f'Video ID: {transcript.video_id}', f'Caption language: {transcript.language}', 'Source scope: speech captions only; video frames were not inspected.', '', 'Transcript:']
    for segment in transcript.segments:
        start = _timestamp(segment.start_seconds)
        end = _timestamp(segment.end_seconds)
        lines.append(f'[{start} --> {end}] {segment.text}' if segment.start_seconds is not None else segment.text)
    return '\n'.join(lines)

def _timestamp(value: float | None) -> str:
    if value is None:
        return 'unknown'
    millis = round(value * 1000)
    hours, remainder = divmod(millis, 3600000)
    minutes, remainder = divmod(remainder, 60000)
    seconds, milliseconds = divmod(remainder, 1000)
    return f'{hours:02d}:{minutes:02d}:{seconds:02d}.{milliseconds:03d}'

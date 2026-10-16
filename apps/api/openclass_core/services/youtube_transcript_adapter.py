from __future__ import annotations
from dataclasses import dataclass
import hashlib
import html
import json
import math
import re
from typing import Any
from urllib.parse import urlparse
from xml.etree import ElementTree
YOUTUBE_HOSTS = {'youtube.com', 'www.youtube.com', 'm.youtube.com', 'music.youtube.com', 'youtu.be', 'www.youtu.be'}
DEFAULT_TRANSCRIPT_LANGUAGES = ('zh-Hans', 'zh-CN', 'zh', 'en', 'en-US')
CAPTION_EXT_PRIORITY = ('vtt', 'json3', 'srv3', 'ttml')
MAX_CAPTION_BYTES = 8 * 1024 * 1024
MAX_CAPTION_SEGMENTS = 100000

@dataclass(frozen=True)
class CaptionSegment:
    start_seconds: float | None
    end_seconds: float | None
    text: str

@dataclass(frozen=True)
class YouTubeTranscript:
    title: str
    video_id: str
    language: str
    text: str
    metadata: dict[str, Any]
    segments: tuple[CaptionSegment, ...] = ()
    raw_caption: str = ''
    caption_format: str = ''

class YouTubeTranscriptAdapterError(RuntimeError):
    pass

class YouTubeNoCaptionsError(YouTubeTranscriptAdapterError):

    def __init__(self, info: dict[str, Any]):
        super().__init__('The YouTube video has no usable captions or automatic captions.')
        self.info = info

class _QuietExtractorLogger:

    def debug(self, _message):
        pass

    def warning(self, _message):
        pass

    def error(self, _message):
        pass

class YouTubeTranscriptAdapter:

    def __init__(self, *, timeout_seconds: float=20.0) -> None:
        self.timeout_seconds = timeout_seconds

    def extract(self, source_uri: str, *, title: str='') -> YouTubeTranscript:
        if not is_youtube_url(source_uri):
            raise YouTubeTranscriptAdapterError('Enter a public YouTube video URL.')
        info = self._extract_info(source_uri)
        _validate_access(info)
        track = _select_caption_track(info)
        if not track:
            raise YouTubeNoCaptionsError(info)
        raw_caption = self._download_caption(str(track['url']))
        if len(raw_caption.encode('utf-8')) > MAX_CAPTION_BYTES:
            raise YouTubeTranscriptAdapterError('The YouTube captions exceed the 8 MB processing limit.')
        segments = _caption_segments(raw_caption, str(track.get('ext') or ''))
        if not segments:
            raise YouTubeTranscriptAdapterError('The YouTube caption track is empty or uses an unsupported format. No transcript was generated.')
        if len(segments) > MAX_CAPTION_SEGMENTS:
            raise YouTubeTranscriptAdapterError('The YouTube captions exceed the segment count limit.')
        video_title = title.strip() or _string_value(info, 'title') or source_uri
        video_id = _string_value(info, 'id')
        language = str(track.get('language') or track.get('language_code') or '')
        return YouTubeTranscript(title=video_title, video_id=video_id, language=language, text=_transcript_text(title=video_title, source_uri=source_uri, language=language, segments=segments), segments=tuple(segments), raw_caption=raw_caption, caption_format=str(track.get('ext') or ''), metadata={'adapter': 'youtube_transcript', 'media_provider': 'youtube', 'media_kind': 'video', 'video_id': video_id, 'duration': info.get('duration'), 'uploader': info.get('uploader'), 'webpage_url': source_uri, 'transcript_language': language, 'transcript_kind': track.get('kind') or '', 'transcript_ext': track.get('ext') or '', 'transcript_original_language': _string_value(info, 'language'), 'caption_sha256': hashlib.sha256(raw_caption.encode('utf-8')).hexdigest(), 'segment_count': len(segments), 'timed_segment_count': sum((segment.start_seconds is not None for segment in segments))})

    def _extract_info(self, source_uri: str) -> dict[str, Any]:
        try:
            import yt_dlp
        except ModuleNotFoundError as exc:
            raise YouTubeTranscriptAdapterError('The server is missing yt-dlp and cannot read YouTube captions.') from exc
        options = {'extract_flat': False, 'noplaylist': True, 'quiet': True, 'no_warnings': True, 'logger': _QuietExtractorLogger(), 'skip_download': True, 'writesubtitles': False, 'socket_timeout': self.timeout_seconds, 'retries': 1, 'extractor_retries': 1, 'geo_bypass': False, 'usenetrc': False, 'cookiefile': None, 'cookiesfrombrowser': None, 'username': None, 'password': None, 'cachedir': False}
        try:
            with yt_dlp.YoutubeDL(options) as downloader:
                info = downloader.extract_info(source_uri, download=False)
        except Exception as exc:
            if 'not a bot' in str(exc).lower() or "confirm you're" in str(exc).lower():
                raise YouTubeTranscriptAdapterError('YouTube requires sign-in or CAPTCHA verification on this network, so captions cannot be read. Retry later or upload captions or audio; verification will not be bypassed.') from exc
            raise YouTubeTranscriptAdapterError('The YouTube video is not publicly accessible. Check its availability. Sign-in, region, payment, and CAPTCHA restrictions will not be bypassed.') from exc
        if not isinstance(info, dict) or info.get('_type') in {'playlist', 'multi_video'}:
            raise YouTubeTranscriptAdapterError('Enter a single YouTube video URL. Channels and playlists cannot be imported.')
        return info

    def _download_caption(self, caption_uri: str) -> str:
        parsed = urlparse(caption_uri)
        host = (parsed.hostname or '').lower()
        if parsed.scheme != 'https' or not (host in YOUTUBE_HOSTS or host.endswith('.youtube.com') or host.endswith('.googlevideo.com')):
            raise YouTubeTranscriptAdapterError('YouTube returned a caption URL outside the supported public caption service.')
        from openclass_core.services.google_sources.web_snapshot import fetch_public
        try:
            content, _mime, _url = fetch_public(caption_uri)
            if len(content) > MAX_CAPTION_BYTES:
                raise YouTubeTranscriptAdapterError('The YouTube captions exceed the 8 MB processing limit.')
            return content.decode('utf-8-sig')
        except YouTubeTranscriptAdapterError:
            raise
        except Exception as exc:
            raise YouTubeTranscriptAdapterError('YouTube caption download failed. Retry later or upload a caption file.') from exc

def is_youtube_url(source_uri: str) -> bool:
    try:
        parsed = urlparse(source_uri)
        return bool(parsed.scheme in {'http', 'https'} and (not parsed.username) and (not parsed.password) and (parsed.port in {None, 80, 443}) and ((parsed.hostname or '').lower() in YOUTUBE_HOSTS))
    except ValueError:
        return False

def _validate_access(info: dict[str, Any]) -> None:
    if info.get('availability') not in {None, 'public', 'unlisted'}:
        raise YouTubeTranscriptAdapterError('This YouTube video requires sign-in, authorization, or payment and cannot be read as a public source.')
    if info.get('is_live') or info.get('live_status') in {'is_live', 'is_upcoming', 'post_live'}:
        raise YouTubeTranscriptAdapterError('Live or unfinished videos are not supported yet. Import the video after the complete recording is available.')

def _select_caption_track(info: dict[str, Any]) -> dict[str, Any] | None:
    original = _string_value(info, 'language')
    for kind, tracks_by_language in (('subtitles', info.get('subtitles')), ('automatic_captions', info.get('automatic_captions'))):
        if isinstance(tracks_by_language, dict):
            track = _select_caption_track_from_languages(tracks_by_language, kind=kind, original_language=original)
            if track:
                return track
    return None

def _select_caption_track_from_languages(tracks_by_language: dict[str, Any], *, kind: str, original_language: str='') -> dict[str, Any] | None:
    preferred = [original_language, original_language + '-orig'] if original_language else []
    preferred.extend((language for language in tracks_by_language if language.endswith('-orig')))
    preferred.extend(DEFAULT_TRANSCRIPT_LANGUAGES)
    candidate_languages = list(dict.fromkeys([*preferred, *tracks_by_language]))
    for language in candidate_languages:
        if not language or language == 'live_chat':
            continue
        tracks = tracks_by_language.get(language)
        if isinstance(tracks, list):
            track = _best_track(tracks)
            if track:
                return {**track, 'language': language, 'kind': kind}
    return None

def _best_track(tracks: list[Any]) -> dict[str, Any] | None:
    valid_tracks = [track for track in tracks if isinstance(track, dict) and isinstance(track.get('url'), str) and track['url'].strip()]
    for ext in CAPTION_EXT_PRIORITY:
        for track in valid_tracks:
            if str(track.get('ext') or '').lower() == ext:
                return track
    return None

def _caption_segments(raw: str, ext: str) -> list[CaptionSegment]:
    normalized_ext = ext.lower().strip()
    if normalized_ext == 'json3':
        return _json3_segments(raw)
    if normalized_ext in {'srv3', 'ttml'} or raw.lstrip().startswith('<'):
        return _xml_segments(raw)
    return _vtt_segments(raw)

def _vtt_segments(raw: str) -> list[CaptionSegment]:
    segments = []
    start, end, lines = (None, None, [])
    skip_block = False
    for raw_line in [*raw.splitlines(), '']:
        line = raw_line.strip()
        if not line:
            _append_segment(segments, start, end, ' '.join(lines))
            start, end, lines, skip_block = (None, None, [], False)
            continue
        if line.startswith(('NOTE', 'STYLE', 'REGION')):
            skip_block = True
        if skip_block or line.startswith(('WEBVTT', 'Kind:', 'Language:')):
            continue
        if '-->' in line:
            _append_segment(segments, start, end, ' '.join(lines))
            left, right = line.split('-->', 1)
            start, end, lines = (_seconds_from_timestamp(left), _seconds_from_timestamp(right), [])
            continue
        if start is not None:
            lines.append(line)
    return segments

def _json3_segments(raw: str) -> list[CaptionSegment]:
    try:
        payload = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return []
    if not isinstance(payload, dict) or not isinstance(payload.get('events', []), list):
        return []
    segments = []
    for event in payload.get('events', []):
        if not isinstance(event, dict) or not isinstance(event.get('segs', []), list):
            continue
        text = ''.join((str(segment.get('utf8') or '') for segment in event.get('segs', []) if isinstance(segment, dict)))
        start = _milliseconds_to_seconds(event.get('tStartMs'))
        duration = _milliseconds_to_seconds(event.get('dDurationMs'))
        _append_segment(segments, start, start + duration if start is not None and duration is not None else None, text)
    return segments

def _xml_segments(raw: str) -> list[CaptionSegment]:
    if '<!DOCTYPE' in raw.upper() or '<!ENTITY' in raw.upper():
        return []
    try:
        root = ElementTree.fromstring(raw)
    except ElementTree.ParseError:
        return []
    segments = []
    for element in root.iter():
        if str(element.tag).rsplit('}', 1)[-1].lower() not in {'p', 'text'}:
            continue
        start = _milliseconds_to_seconds(element.attrib.get('t'))
        if start is None:
            start = _seconds_from_timestamp(str(element.attrib.get('begin') or element.attrib.get('start') or ''))
        end = _seconds_from_timestamp(str(element.attrib.get('end') or ''))
        if end is None:
            duration = _milliseconds_to_seconds(element.attrib.get('d'))
            if duration is None:
                duration = _seconds_from_timestamp(str(element.attrib.get('dur') or ''))
            if start is not None and duration is not None:
                end = start + duration
        _append_segment(segments, start, end, ' '.join(element.itertext()))
    return segments

def _append_segment(segments: list[CaptionSegment], start: float | None, end: float | None, text: str) -> None:
    cleaned = _clean_caption_text(text)
    if cleaned and (start is None or end is None or end >= start):
        segment = CaptionSegment(start, end, cleaned)
        if not segments or segments[-1] != segment:
            segments.append(segment)

def _clean_caption_text(text: str) -> str:
    no_tags = re.sub('<[^>]+>', '', text)
    cleaned = html.unescape(no_tags).replace('\u200b', '')
    return re.sub('\\s+', ' ', cleaned).strip()

def _transcript_text(*, title: str, source_uri: str, language: str, segments: list[CaptionSegment]) -> str:
    header = [f'Title: {title}', f'Source: {source_uri}', 'Media type: YouTube video transcript (no video-frame analysis)', f"Transcript language: {language or 'unknown'}", '', 'Transcript:']
    lines = [f'[{_format_timestamp(segment.start_seconds)}] {segment.text}' if segment.start_seconds is not None else segment.text for segment in segments]
    return '\n'.join([*header, *lines]).strip()

def _seconds_from_timestamp(value: str) -> float | None:
    timestamp = value.strip().split(' ', 1)[0]
    try:
        if timestamp.endswith('ms'):
            result = float(timestamp[:-2]) / 1000
        elif timestamp.endswith('s'):
            result = float(timestamp[:-1])
        else:
            parts = timestamp.replace(',', '.').split(':')
            result = float(parts[-1])
            if len(parts) > 1:
                result += int(parts[-2]) * 60
            if len(parts) > 2:
                result += int(parts[-3]) * 3600
            if len(parts) > 3:
                return None
        return result if math.isfinite(result) and result >= 0 else None
    except (ValueError, TypeError, IndexError):
        return None

def _milliseconds_to_seconds(value: Any) -> float | None:
    try:
        result = float(value) / 1000
        return result if math.isfinite(result) and result >= 0 else None
    except (TypeError, ValueError):
        return None

def _format_timestamp(seconds: float | None) -> str:
    if seconds is None:
        return '--:--'
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f'{hours}:{minutes:02d}:{secs:02d}' if hours else f'{minutes:02d}:{secs:02d}'

def _string_value(info: dict[str, Any], key: str) -> str:
    value = info.get(key)
    return value if isinstance(value, str) else ''
youtube_transcript_adapter = YouTubeTranscriptAdapter()

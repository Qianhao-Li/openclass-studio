from __future__ import annotations
import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from openclass_core.services import workspace_state
from .adapters import AdaptedSource, ProcessingPart
from .config import GoogleSourceError

def transcribe(source, content, *, client, processor, checkpoint, save, check):
    executable = shutil.which('ffprobe')
    if executable is None:
        raise GoogleSourceError('Audio import requires ffprobe to verify duration. It is not configured on this server.')
    try:
        from .catalog_fallback import original_cache_root
        cache = original_cache_root() / '.audio-probe'
        cache.mkdir(parents=True, exist_ok=True, mode=448)
        suffix = Path(source.file_name).suffix.lower()
        with tempfile.NamedTemporaryFile(prefix='probe-', suffix=suffix, dir=cache) as audio_file:
            audio_file.write(content)
            audio_file.flush()
            probe = subprocess.run([executable, '-v', 'error', '-protocol_whitelist', 'file,pipe', '-show_entries', 'format=duration', '-of', 'json', '-i', audio_file.name], capture_output=True, timeout=30, check=True)
        duration = float(json.loads(probe.stdout)['format']['duration'])
    except (subprocess.SubprocessError, KeyError, ValueError) as exc:
        raise GoogleSourceError('The audio duration could not be verified. Convert it to WAV or FLAC and retry.') from exc
    if not 0 < duration <= 900:
        raise GoogleSourceError('Audio import currently supports up to 15 minutes. Split longer recordings before uploading.')
    config = client.config
    if checkpoint.get('submitting') and (not checkpoint.get('operation')):
        raise GoogleSourceError('The previous transcription submission has an unknown result. Check the cloud operation to avoid duplicate charges.')
    if not checkpoint.get('operation'):
        check()
        save({'submitting': True})
        response = client.json('POST', f'https://{config.speech_location}-speech.googleapis.com/v2/projects/{config.project_id}/locations/{config.speech_location}/recognizers/_:batchRecognize', json={'config': {'autoDecodingConfig': {}, 'languageCodes': ['auto'], 'model': 'chirp_3', 'features': {'enableWordTimeOffsets': True, 'enableAutomaticPunctuation': True}}, 'files': [{'uri': source.metadata['google_original']['uri']}], 'recognitionOutputConfig': {'inlineResponseConfig': {}}})
        checkpoint = {'operation': response.get('name'), 'submitting': False}
        if not checkpoint['operation']:
            raise GoogleSourceError('Google Speech did not return a resumable operation ID.')
        save(checkpoint)
    result = processor.wait_operation(checkpoint['operation'], check=check, speech=True)
    files = result.get('response', {}).get('results', {})
    file_result = files.get(source.metadata['google_original']['uri'], {})
    if file_result.get('error'):
        raise GoogleSourceError('Google audio transcription failed.')
    transcript = file_result.get('inlineResult', {}).get('transcript', file_result.get('transcript', {}))
    segments, lines = ([], [])
    for item in transcript.get('results', []):
        alternatives = item.get('alternatives', [])
        if not alternatives:
            continue
        best = alternatives[0]
        text = best.get('transcript', '').strip()
        if not text:
            continue
        words = best.get('words', [])
        start = words[0].get('startOffset', '0s') if words else '0s'
        end = words[-1].get('endOffset', item.get('resultEndOffset', '')) if words else item.get('resultEndOffset', '')
        segments.append({'text': text, 'start': start, 'end': end})
        lines.append(f'[{start}–{end}] {text}')
    if not lines:
        raise GoogleSourceError('No usable speech transcript was obtained. The original audio has been preserved.')
    adapted = AdaptedSource([ProcessingPart('transcript.txt', 'text/plain', '\n\n'.join(lines).encode(), False, locator={'kind': 'audio_transcript', 'duration_seconds': duration})], 'audio')
    return (adapted, {'duration_seconds': duration, 'segments': segments, 'speech_operation': checkpoint['operation']})

from __future__ import annotations
import os
import subprocess
from pathlib import Path
from urllib.parse import urlsplit
from openclass_core.services.source_archive import SafeSourceArchive, SourceArchiveError
from openclass_core.services.source_xml import SourceXmlError, parse_untrusted_xml
MAX_OOXML_RELATIONSHIP_FILES = 2048
MAX_OOXML_RELATIONSHIP_FILE_BYTES = 1024 * 1024
MAX_OOXML_RELATIONSHIP_TOTAL_BYTES = 16 * 1024 * 1024
MAX_OOXML_RELATIONSHIPS = 32768
_PROXY_ENVIRONMENT_KEYS = {'all_proxy', 'ftp_proxy', 'http_proxy', 'https_proxy', 'no_proxy'}

class LibreOfficeRenderError(RuntimeError):
    pass

class LibreOfficeRenderer:

    def __init__(self, executable: str | None=None) -> None:
        self._configured_executable = executable

    @property
    def executable(self) -> Path | None:
        raw = self._configured_executable or os.getenv('OPENCLASS_LIBREOFFICE_PATH', '')
        if not raw.strip():
            return None
        path = Path(raw).expanduser().resolve()
        return path if path.is_file() and os.access(path, os.X_OK) else None

    @property
    def available(self) -> bool:
        return self.executable is not None

    def validate_configuration(self) -> str:
        raw = self._configured_executable or os.getenv('OPENCLASS_LIBREOFFICE_PATH', '')
        if not raw.strip():
            return 'disabled'
        if self.executable is None:
            raise LibreOfficeRenderError('OPENCLASS_LIBREOFFICE_PATH must point to an executable LibreOffice binary.')
        return 'available'

    def status(self) -> dict[str, object]:
        try:
            state = self.validate_configuration()
        except LibreOfficeRenderError as exc:
            return {'status': 'invalid', 'available': False, 'error': str(exc)}
        return {'status': state, 'available': state == 'available'}

    def render_pdf(self, source_path: Path, *, output_dir: Path) -> Path:
        executable = self.executable
        if executable is None:
            raise LibreOfficeRenderError('OPENCLASS_LIBREOFFICE_PATH is not configured with an executable LibreOffice binary.')
        source_path = source_path.expanduser().resolve()
        if not source_path.is_file():
            raise LibreOfficeRenderError('LibreOffice source file is unavailable.')
        _validate_ooxml_relationships(source_path)
        output_dir = output_dir.expanduser().resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        profile_dir = output_dir / '.libreoffice-profile'
        profile_dir.mkdir(parents=True, exist_ok=True)
        process_environment = _libreoffice_environment(profile_dir=profile_dir, output_dir=output_dir)
        try:
            completed = subprocess.run([str(executable), f'-env:UserInstallation={profile_dir.resolve().as_uri()}', '--headless', '--invisible', '--nologo', '--norestore', '--nodefault', '--nofirststartwizard', '--convert-to', 'pdf', '--outdir', str(output_dir), str(source_path)], check=False, capture_output=True, text=True, timeout=90, cwd=output_dir, env=process_environment)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise LibreOfficeRenderError(f'LibreOffice rendering failed: {exc}') from exc
        output_path = output_dir / f'{source_path.stem}.pdf'
        if completed.returncode != 0 or not output_path.is_file():
            detail = (completed.stderr or completed.stdout or 'conversion produced no PDF').strip()
            raise LibreOfficeRenderError(f'LibreOffice rendering failed: {detail[:500]}')
        return output_path

def _validate_ooxml_relationships(source_path: Path) -> None:
    try:
        with SafeSourceArchive(source_path) as archive:
            relationship_paths = sorted((name for name in archive.namelist() if name.casefold().endswith('.rels')))
            if len(relationship_paths) > MAX_OOXML_RELATIONSHIP_FILES:
                raise LibreOfficeRenderError('LibreOffice rendering refused: the Office package contains too many relationship files.')
            total_bytes = 0
            relationship_count = 0
            for relationship_path in relationship_paths:
                content = archive.read(relationship_path, max_bytes=MAX_OOXML_RELATIONSHIP_FILE_BYTES)
                total_bytes += len(content)
                if total_bytes > MAX_OOXML_RELATIONSHIP_TOTAL_BYTES:
                    raise LibreOfficeRenderError('LibreOffice rendering refused: Office relationships exceed the safety budget.')
                root = parse_untrusted_xml(content)
                for node in root.iter():
                    if _xml_local_name(node.tag) != 'Relationship':
                        continue
                    relationship_count += 1
                    if relationship_count > MAX_OOXML_RELATIONSHIPS:
                        raise LibreOfficeRenderError('LibreOffice rendering refused: the Office package contains too many relationships.')
                    target_mode = str(node.attrib.get('TargetMode') or '').strip().casefold()
                    target = str(node.attrib.get('Target') or '').strip()
                    if target_mode not in {'', 'internal'} or _target_looks_external(target):
                        raise LibreOfficeRenderError('LibreOffice rendering refused: the Office package contains an external relationship.')
    except LibreOfficeRenderError:
        raise
    except (OSError, SourceArchiveError, SourceXmlError) as exc:
        raise LibreOfficeRenderError('LibreOffice rendering refused: the Office package could not be validated safely.') from exc

def _xml_local_name(tag: object) -> str:
    return str(tag).rsplit('}', 1)[-1]

def _target_looks_external(target: str) -> bool:
    if not target:
        return False
    if target.startswith(('//', '\\\\')):
        return True
    if len(target) >= 3 and target[0].isalpha() and (target[1] == ':') and (target[2] in {'/', '\\'}):
        return True
    return bool(urlsplit(target).scheme)

def _libreoffice_environment(*, profile_dir: Path, output_dir: Path) -> dict[str, str]:
    environment = {key: value for key, value in os.environ.items() if key.casefold() not in _PROXY_ENVIRONMENT_KEYS}
    environment.update({'HOME': str(profile_dir.resolve()), 'TMPDIR': str(output_dir.resolve()), 'SAL_USE_VCLPLUGIN': 'gen'})
    return environment
libreoffice_renderer = LibreOfficeRenderer()

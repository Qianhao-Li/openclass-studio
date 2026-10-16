from __future__ import annotations
import os
import shutil
import subprocess
from pathlib import Path
from typing import Literal
POPPLER_ROOT_ENV = 'OPENCLASS_POPPLER_ROOT'
REQUIRED_PDF_TOOLS = ('pdfinfo', 'pdftotext', 'pdftoppm')
SYSTEM_TOOL_DIRECTORIES = ('/usr/bin', '/bin', '/usr/sbin', '/sbin')
PREFLIGHT_TIMEOUT_SECONDS = 45
DIRECTORY_ONLY_MAX_PDF_TOOL_PAGE_SPAN = 32
BROAD_SYSTEM_PREFIXES = frozenset({Path('/'), Path('/usr'), Path('/usr/local'), Path('/opt/homebrew')})

class SourceDocumentToolchainError(RuntimeError):
    pass

def pdf_toolchain_health() -> dict[str, object]:
    configuration = 'explicit' if (os.getenv(POPPLER_ROOT_ENV) or '').strip() else 'path'
    try:
        resolve_poppler_root()
    except SourceDocumentToolchainError:
        status = 'unavailable'
    else:
        status = 'available'
    return {'status': status, 'configuration': configuration, 'required_tools': list(REQUIRED_PDF_TOOLS)}

def source_document_tool_path(toolbox_path: Path) -> str:
    directories = [str(toolbox_path / 'bin')]
    directories.extend((directory for directory in SYSTEM_TOOL_DIRECTORIES if directory not in directories))
    return os.pathsep.join(directories)

def prepare_source_document_toolbox(*, cwd: Path, source_path: Path, scratch_path: Path, inspection_scope: Literal['source', 'directory_only', 'catalog_v3']='source') -> Path:
    toolbox = cwd / 'toolbox'
    toolbox_bin = toolbox / 'bin'
    toolbox_bin.mkdir(parents=True, mode=493)
    if source_path.suffix.lower() != '.pdf':
        return toolbox
    poppler_root = resolve_poppler_root()
    source_bin = poppler_root / 'bin'
    for tool in REQUIRED_PDF_TOOLS:
        if inspection_scope in {'directory_only', 'catalog_v3'} and tool in {'pdftotext', 'pdftoppm'}:
            real_tool = toolbox / 'libexec' / tool
            real_tool.parent.mkdir(parents=True, exist_ok=True, mode=493)
            _link_or_copy_read_only(source_bin / tool, real_tool)
            _write_bounded_pdf_tool_wrapper(real_tool=real_tool, wrapper=toolbox_bin / tool)
        else:
            _link_or_copy_read_only(source_bin / tool, toolbox_bin / tool)
    if poppler_root not in BROAD_SYSTEM_PREFIXES:
        for relative_directory in ('lib', 'share'):
            source_directory = poppler_root / relative_directory
            if source_directory.is_dir():
                _link_directory_read_only(source_directory, toolbox / relative_directory)
    verify_pdf_toolbox(toolbox_path=toolbox, source_path=source_path, scratch_path=scratch_path)
    return toolbox

def _write_bounded_pdf_tool_wrapper(*, real_tool: Path, wrapper: Path) -> None:
    maximum_span = DIRECTORY_ONLY_MAX_PDF_TOOL_PAGE_SPAN
    wrapper.write_text(f"""#!/bin/sh\nfirst=\nlast=\nexpect=\nfor argument in "$@"; do\n  if [ "$expect" = first ]; then first=$argument; expect=; continue; fi\n  if [ "$expect" = last ]; then last=$argument; expect=; continue; fi\n  case "$argument" in\n    -f) expect=first ;;\n    -l) expect=last ;;\n  esac\ndone\ncase "$first:$last" in\n  *[!0-9:]*|:*|*:) echo 'directory-only PDF inspection requires numeric -f and -l page bounds' >&2; exit 64 ;;\nesac\nif [ "$first" -lt 1 ] || [ "$last" -lt "$first" ]; then\n  echo 'directory-only PDF inspection received invalid page bounds' >&2; exit 64\nfi\nrequested=$((last - first + 1))\nif [ "$requested" -gt {maximum_span} ]; then\n  echo 'directory-only PDF inspection is limited to {maximum_span} pages per command' >&2; exit 64\nfi\nexec {str(real_tool)!r} "$@"\n""", encoding='utf-8')
    wrapper.chmod(365)

def resolve_poppler_root() -> Path:
    configured = (os.getenv(POPPLER_ROOT_ENV) or '').strip()
    if configured:
        configured_path = Path(configured).expanduser()
        if not configured_path.is_absolute():
            raise SourceDocumentToolchainError(f'{POPPLER_ROOT_ENV} must be an absolute directory path.')
        return _validate_poppler_root(configured_path)
    candidate_text = shutil.which('pdfinfo')
    if not candidate_text:
        raise SourceDocumentToolchainError('PDF toolchain unavailable: configure OPENCLASS_POPPLER_ROOT with pdfinfo, pdftotext, and pdftoppm.')
    try:
        candidate = Path(candidate_text).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise SourceDocumentToolchainError('PDF toolchain unavailable: the discovered pdfinfo executable is invalid.') from exc
    bundled_root = _bundled_poppler_root_from_override(candidate)
    if bundled_root is not None:
        return _validate_poppler_root(bundled_root)
    return _validate_poppler_root(candidate.parent.parent)

def verify_pdf_toolbox(*, toolbox_path: Path, source_path: Path, scratch_path: Path) -> None:
    if not source_path.is_file() or source_path.suffix.lower() != '.pdf':
        raise SourceDocumentToolchainError('PDF toolchain preflight requires a staged PDF source file.')
    scratch_path.mkdir(parents=True, exist_ok=True, mode=448)
    environment = {'PATH': source_document_tool_path(toolbox_path), 'LANG': 'en_US.UTF-8', 'SHELL': '/bin/zsh'}
    probe_prefix = scratch_path / '.poppler-preflight'
    commands = (('pdfinfo', ['pdfinfo', '-f', '1', '-l', '1', str(source_path)]), ('pdftotext', ['pdftotext', '-f', '1', '-l', '1', '-layout', '-enc', 'UTF-8', str(source_path), '-']), ('pdftoppm', ['pdftoppm', '-f', '1', '-l', '1', '-singlefile', '-scale-to', '32', '-png', str(source_path), str(probe_prefix)]))
    try:
        for tool, command in commands:
            _run_preflight_command(tool=tool, command=command, environment=environment)
        rendered_probe = probe_prefix.with_suffix('.png')
        if not rendered_probe.is_file() or rendered_probe.stat().st_size < 1:
            raise SourceDocumentToolchainError('PDF toolchain preflight failed: pdftoppm produced no image.')
    finally:
        for candidate in scratch_path.glob(f'{probe_prefix.name}*'):
            if candidate.is_file() and (not candidate.is_symlink()):
                candidate.unlink(missing_ok=True)

def _validate_poppler_root(root: Path) -> Path:
    try:
        resolved = root.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise SourceDocumentToolchainError(f'{POPPLER_ROOT_ENV} does not identify an existing Poppler directory.') from exc
    if not resolved.is_dir():
        raise SourceDocumentToolchainError(f'{POPPLER_ROOT_ENV} must identify a Poppler directory.')
    missing = [tool for tool in REQUIRED_PDF_TOOLS if not _is_executable_file(resolved / 'bin' / tool)]
    if missing:
        raise SourceDocumentToolchainError('PDF toolchain is incomplete; missing executable tools: ' + ', '.join(missing))
    return resolved

def _is_executable_file(path: Path) -> bool:
    try:
        return path.is_file() and os.access(path, os.X_OK)
    except OSError:
        return False

def _bundled_poppler_root_from_override(pdfinfo_path: Path) -> Path | None:
    override = pdfinfo_path.parent
    if not (override.name == 'override' and override.parent.name == 'bin' and (override.parent.parent.name == 'dependencies') and ('codex-runtimes' in override.parts)):
        return None
    root = override.parent.parent / 'native' / 'poppler' / 'poppler'
    return root if root.is_dir() else None

def _run_preflight_command(*, tool: str, command: list[str], environment: dict[str, str]) -> None:
    try:
        completed = subprocess.run(command, check=False, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=PREFLIGHT_TIMEOUT_SECONDS, env=environment)
    except (OSError, subprocess.SubprocessError) as exc:
        raise SourceDocumentToolchainError(f'PDF toolchain preflight could not execute {tool}.') from exc
    if completed.returncode == 0:
        return
    detail = (completed.stderr or completed.stdout or '').strip().replace('\n', ' ')
    if len(detail) > 300:
        detail = detail[:297] + '...'
    suffix = f': {detail}' if detail else ''
    raise SourceDocumentToolchainError(f'PDF toolchain preflight failed for {tool}{suffix}')

def _link_directory_read_only(source: Path, destination: Path) -> None:
    for root_text, directory_names, file_names in os.walk(source):
        root = Path(root_text)
        relative = root.relative_to(source)
        target_root = destination / relative
        target_root.mkdir(parents=True, exist_ok=True, mode=493)
        for directory_name in directory_names:
            (target_root / directory_name).mkdir(exist_ok=True, mode=493)
        for file_name in file_names:
            source_file = root / file_name
            target_file = target_root / file_name
            try:
                resolved_source = source_file.resolve(strict=True)
            except (OSError, RuntimeError):
                continue
            if resolved_source.is_file():
                _link_or_copy_read_only(resolved_source, target_file)

def _link_or_copy_read_only(source: Path, destination: Path) -> None:
    try:
        os.link(source, destination)
    except OSError:
        shutil.copy2(source, destination)
        destination.chmod(source.stat().st_mode & 365)

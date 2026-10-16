"""Isolated Office/ODF conversion; converted PDFs always go through Layout Parser."""
from __future__ import annotations
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from openclass_core.services import workspace_state
from .config import GoogleSourceError

def convert_office(content: bytes, file_name: str, *, target: str='pdf') -> bytes:
    executable = os.getenv('OPENCLASS_LIBREOFFICE_PATH') or shutil.which('libreoffice') or shutil.which('soffice')
    if not executable or not Path(executable).is_file():
        raise GoogleSourceError('This source format requires LibreOffice conversion. Configure OPENCLASS_LIBREOFFICE_PATH.')
    from .catalog_fallback import original_cache_root
    cache = original_cache_root() / '.office-conversion'
    cache.mkdir(parents=True, exist_ok=True, mode=448)
    with tempfile.TemporaryDirectory(prefix='google-source-', dir=cache) as temporary:
        directory = Path(temporary).resolve()
        source = directory / ('source' + Path(file_name).suffix.lower())
        source.write_bytes(content)
        profile = directory / 'profile'
        profile.mkdir()
        (profile / 'user').mkdir()
        (profile / 'user' / 'registrymodifications.xcu').write_text('<?xml version="1.0" encoding="UTF-8"?><oor:items xmlns:oor="http://openoffice.org/2001/registry"><item oor:path="/org.openoffice.Office.Common/Security/Scripting"><prop oor:name="MacroSecurityLevel" oor:op="fuse"><value>3</value></prop><prop oor:name="DisableMacrosExecution" oor:op="fuse"><value>true</value></prop></item></oor:items>', encoding='utf-8')
        command = [str(Path(executable).resolve()), f'-env:UserInstallation={profile.as_uri()}', '--headless', '--invisible', '--nologo', '--norestore', '--nodefault', '--nofirststartwizard', '--convert-to', target, '--outdir', str(directory), str(source)]
        if sys.platform == 'darwin' and Path('/usr/bin/sandbox-exec').exists():
            escaped = str(directory).replace('\\', '\\\\').replace('"', '\\"')
            rules = f'(version 1) (allow default) (deny network*) (deny file-read* (subpath "/Users")) (allow file-read* (subpath "{escaped}")) (deny file-write*) (allow file-write* (subpath "{escaped}") (literal "/dev/null"))'
            command = ['/usr/bin/sandbox-exec', '-p', rules, *command]
        elif shutil.which('bwrap'):
            executable_path = Path(executable).resolve()
            runtime_roots = tuple((Path(root) for root in ('/usr', '/bin', '/lib', '/lib64')))
            if not any((executable_path.is_relative_to(root) for root in runtime_roots)):
                raise GoogleSourceError('On Linux, LibreOffice must be installed in a system runtime directory (/usr, /bin, /lib, or /lib64).')
            wrapper = [shutil.which('bwrap'), '--unshare-all', '--die-with-parent', '--new-session', '--proc', '/proc', '--dev', '/dev', '--tmpfs', '/tmp']
            for system in ('/usr', '/bin', '/lib', '/lib64', '/etc/fonts', '/etc/ld.so.cache', '/etc/libreoffice'):
                if Path(system).exists():
                    wrapper.extend(['--ro-bind', system, system])
            command = [*wrapper, '--bind', str(directory), str(directory), '--chdir', str(directory), *command]
        else:
            raise GoogleSourceError('This format conversion requires network-isolated processes using sandbox-exec or bubblewrap.')
        try:
            result = subprocess.run(command, cwd=directory, env={'PATH': os.defpath, 'LANG': 'C.UTF-8', 'TMPDIR': str(directory), 'SAL_USE_VCLPLUGIN': 'svp' if sys.platform == 'linux' else 'gen'}, capture_output=True, timeout=120, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise GoogleSourceError('Isolated document conversion failed or timed out.') from exc
        output = directory / ('source.' + target.split(':', 1)[0])
        if result.returncode or not output.is_file() or (not output.stat().st_size):
            raise GoogleSourceError('LibreOffice did not produce a complete converted file.')
        if output.stat().st_size > 200 * 1024 * 1024:
            raise GoogleSourceError('The converted file exceeds the 200 MB processing limit.')
        return output.read_bytes()

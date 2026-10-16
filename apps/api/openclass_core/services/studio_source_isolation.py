"""Native shell inspection needs an explicit isolated runner from its host."""
from dataclasses import dataclass
from openclass_core.services.studio_ports import host_service

@dataclass(frozen=True)
class IsolatedSourceCommand:
    argv: tuple[str, ...]
    isolation: str

def isolated_source_command(command, *, workspace, source_path):
    try:
        isolated = host_service('isolated_source_command', tuple(command), workspace=workspace, source_path=source_path)
    except RuntimeError as error:
        raise RuntimeError('Native source shell inspection requires an isolated runner. Ordinary reading and bounded source tools remain available.') from error
    if not isinstance(isolated, IsolatedSourceCommand) or isolated.isolation not in {'sandbox-exec', 'bubblewrap', 'rootless-container'} or (not isolated.argv):
        raise RuntimeError('The source host did not provide a verified isolated command; host execution is disabled.')
    return list(isolated.argv)

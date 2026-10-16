import pytest
from openclass_core.services.studio_ports import bind_studio_services
from openclass_core.services.studio_source_isolation import isolated_source_command


def test_missing_or_invalid_isolation_never_falls_back_to_host_execution(tmp_path):
    with bind_studio_services({}):
        with pytest.raises(RuntimeError,match='requires an isolated runner'):
            isolated_source_command(['pi'],workspace=tmp_path,source_path=tmp_path/'source.pdf')
    with bind_studio_services({'isolated_source_command':lambda *_,**__:['pi']}):
        with pytest.raises(RuntimeError,match='host execution is disabled'):
            isolated_source_command(['pi'],workspace=tmp_path,source_path=tmp_path/'source.pdf')


def test_bounded_source_mode_disables_builtin_tools_and_ambient_extensions(tmp_path):
    from openclass_core.services.pi_source_runtime import PiSourceTextClient
    command=PiSourceTextClient('owner',binary='/test/pi',runtime_root=tmp_path)._command(
        provider='deepseek',model='model',reasoning_effort=None,system_prompt='task',inspection_scope='document',allow_pdf_page_image=False)
    assert {'--no-builtin-tools','--no-extensions','--no-skills','--no-context-files'}<=set(command)
    assert '--extension' in command

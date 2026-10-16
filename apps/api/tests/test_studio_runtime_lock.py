import pytest
from openclass_core.services.studio_runtime_lock import studio_writer_lock


def test_direct_backend_start_cannot_bypass_the_profile_writer_lock(tmp_path):
    with studio_writer_lock(tmp_path):
        with pytest.raises(RuntimeError,match='already owns'):
            with studio_writer_lock(tmp_path):pass
    with studio_writer_lock(tmp_path):pass

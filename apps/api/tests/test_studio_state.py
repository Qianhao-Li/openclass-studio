import sqlite3
import pytest
from fastapi import HTTPException
from openclass_core.services.studio_state import StudioStateStore, StudioPreferences


def test_preferences_are_local_and_revision_checked(tmp_path):
    store = StudioStateStore(tmp_path)
    before = store.preferences("local_one")
    saved = store.save_preferences("local_one", StudioPreferences(preferredTextModel="provider:model:personal_api"), 0)
    assert saved["revision"] == 1
    assert StudioStateStore(tmp_path).preferences("local_one") == saved
    with pytest.raises(HTTPException) as conflict:
        store.save_preferences("local_one", StudioPreferences(), before["revision"])
    assert conflict.value.status_code == 409
    assert store.preferences("local_two")["revision"] == 0
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT name FROM sqlite_master WHERE name='users'").fetchone() is None
    assert store.key_file.stat().st_mode & 0o077 == 0


def test_connections_use_profile_storage_without_platform_user(tmp_path):
    store = StudioStateStore(tmp_path)
    connections = store.connections()
    assert connections.list_connections("local_one") == []
    row = connections.save_connection("local_one", "codex", "https://chatgpt.com", "subject", "Personal model",
        {"access_token": "test-private-value"}, {"models": {"granted": True, "enabled": True}}, {"mode": "models"})
    assert "test-private-value" not in store.path.read_bytes().decode("latin1")
    assert connections.get_connection("local_two", row["id"]) is None


def test_source_search_preference_has_one_local_authority(tmp_path):
    from openclass_core.services.studio_ports import bind_studio_services
    from openclass_core.services.source_retrieval_preferences import SourceRetrievalPreferences, SourcePreferenceUpdate, auto_source_retrieval_enabled, _preferences
    store = StudioStateStore(tmp_path)
    with bind_studio_services({"source_preferences_store": lambda _path: store, "source_registered": lambda *_args: False}):
        _preferences.cache_clear()
        preferences = SourceRetrievalPreferences(store)
        assert not auto_source_retrieval_enabled("local_one", store.path)
        preferences.update("local_one", SourcePreferenceUpdate(enabled=True, expected_revision=0))
        assert auto_source_retrieval_enabled("local_one", store.path)
        assert not auto_source_retrieval_enabled("local_two", store.path)
        _preferences.cache_clear()

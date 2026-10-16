from fastapi import FastAPI
from fastapi.testclient import TestClient
from openclass_core.routers import studio_model_accounts
from openclass_core.studio_contracts import WorkspacePrincipal


def test_model_login_is_scoped_to_local_profile_and_cannot_become_platform_login(monkeypatch):
    captured = []
    def start(owner, **kwargs):
        captured.append((owner, kwargs))
        return {"login_id":"model-attempt", "verification_url":"https://auth.openai.com/device", "user_code":"test-code"}
    monkeypatch.setattr(studio_model_accounts, "start_codex_device_login", start)
    app = FastAPI()
    app.state.studio_identity = lambda _request: WorkspacePrincipal(id="local_one", kind="local", capabilities=frozenset({"models.manage"}))
    app.include_router(studio_model_accounts.router)
    client = TestClient(app)
    result = client.post("/api/codex/connections/login/device?purpose=platform")
    assert result.status_code == 200
    assert captured == [("local_one", {"purpose":"provider", "connection_id":None, "new_connection":True})]
    assert client.post("/api/codex/platform-login/device").status_code == 404

"""Exercise the real course routers in a separate, account-free local process."""
import json
import os
from pathlib import Path
import subprocess
import sys


def test_local_runtime_isolated_from_platform(tmp_path):
    script = '''
import json, os, sqlite3
from pathlib import Path
from openclass_core.services.studio_runtime_config import StudioRuntimeConfig
from openclass_core.studio_main import create_app
from fastapi.testclient import TestClient
root=Path(os.environ["STUDIO_TEST_ROOT"])
app=create_app(StudioRuntimeConfig(root, "x"*48))
with TestClient(app, base_url="http://127.0.0.1:8510", client=("127.0.0.1",55000)) as browser:
    assert browser.get("/api/workspace").status_code==401
    browser.headers["Origin"]="http://127.0.0.1:3510"
    opened=browser.post("/api/workbench/v1/session",headers={"X-OpenClass-Launch-Token":"x"*48})
    assert opened.status_code==200,opened.text
    created=browser.post("/api/lessons/generate",json={"start_blank":True})
    assert created.status_code==200,created.text
    course=created.json(); assert course["storage_kind"]=="ridoc"
    current=browser.get("/api/course-package");assert current.status_code==200,current.text
    assert current.json()["id"]==course["id"]
    catalog=browser.get("/api/ai-models"); assert catalog.status_code==200,catalog.text
    assert all(row["access_method"]!="platform_credits" for row in catalog.json()["text"])
    prefs=browser.get("/api/workbench/v1/preferences"); assert prefs.status_code==200,prefs.text
    assert browser.post("/api/auth/guest").status_code==404
    assert browser.get("/api/billing/wallet").status_code==404
    assert browser.get("/api/repositories").status_code==404
    connection=sqlite3.connect(root/"runtime.sqlite3")
    for table in ("users", "auth_guest_sessions"):
        if connection.execute("select 1 from sqlite_master where name=?",(table,)).fetchone():
            assert connection.execute("select count(*) from "+table).fetchone()[0]==0
    print(json.dumps({"id":course["id"],"ridoc_files":len(list(root.rglob("*.ridoc")))}))
'''
    env = {key:value for key,value in os.environ.items() if not key.startswith(("OPENCLASS_", "AI_USAGE_"))}
    env.update({"STUDIO_TEST_ROOT": str(tmp_path), "PYTHONPATH": str(Path(__file__).resolve().parents[1])})
    result = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr + result.stdout
    assert json.loads(result.stdout.strip().splitlines()[-1])["ridoc_files"] == 1

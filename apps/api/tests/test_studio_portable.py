"""A course with images, equations, branches and M/C survives a new machine."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

CREATE_SCRIPT=r'''
import base64,os
from pathlib import Path
from fastapi.testclient import TestClient
from openclass_core.services.studio_runtime_config import StudioRuntimeConfig
from openclass_core.studio_main import create_app
app=create_app(StudioRuntimeConfig(Path(os.environ['PORTABLE_ROOT']),'x'*48))
from openclass_core.services.workspace_state import get_ridoc_workspace_store
from openclass_core.services.rich_document import build_document,rebuild_document_from_content_json
from openclass_core.services.ridoc_turns import RidocTurnOutcome,commit_ridoc_turn_outcome
from openclass_core.services.ridoc_v3 import content_hash
owner=app.state.local_studio_identity.principal.id
png=base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=')
digest=content_hash(png);asset_id='basset_0123456789ab_'+digest[:32]
with TestClient(app,base_url='http://127.0.0.1:8510',client=('127.0.0.1',50000)) as browser:
    browser.headers['Origin']='http://127.0.0.1:3510'
    assert browser.post('/api/workbench/v1/session',headers={'X-OpenClass-Launch-Token':'x'*48}).status_code==200
    response=browser.post('/api/lessons/generate',json={'start_blank':True});assert response.status_code==200,response.text
    package=response.json();lesson_id=package['active_lesson_id']
    store=get_ridoc_workspace_store();lesson=store.load_package(owner,package_id=package['id']).lessons[0]
    document=build_document(title='Portable course fixture',document_id=lesson.board_document.id,
        content_text='# Portable course fixture\n\n$x^2 + 1$\n')
    content=document.content_json
    content['content'].append({'type':'image','attrs':{'src':'/api/board-assets/'+asset_id+'/content','assetId':asset_id,'alt':'Portable image'}})
    document=rebuild_document_from_content_json(document,content)
    record=commit_ridoc_turn_outcome(store,owner_user_id=owner,package_id=package['id'],lesson_id=lesson_id,
        expected_revision=package['ridoc_revision'],outcome=RidocTurnOutcome(transaction_id='portable-complete-turn',
        user_message='Complete fixture question',assistant_message='Complete fixture answer',document=document,
        resource_refs=[digest],asset_payloads={digest:png}))
    store.create_branch(owner_user_id=owner,package_id=package['id'],lesson_id=lesson_id,expected_revision=record.revision,name='study')
    exported=browser.get('/api/lessons/'+lesson_id+'/document/export-ridoc');assert exported.status_code==200,exported.text
    Path(os.environ['PORTABLE_FILE']).write_bytes(exported.content)
'''

OPEN_SCRIPT=r'''
import os
from pathlib import Path
from fastapi.testclient import TestClient
from openclass_core.services.studio_runtime_config import StudioRuntimeConfig
from openclass_core.studio_main import create_app
app=create_app(StudioRuntimeConfig(Path(os.environ['PORTABLE_ROOT']),'x'*48))
from openclass_core.services.workspace_state import get_ridoc_workspace_store
from openclass_core.services.rich_document import build_document,rebuild_document_from_content_json
owner=app.state.local_studio_identity.principal.id
with TestClient(app,base_url='http://127.0.0.1:8510',client=('127.0.0.1',50000)) as browser:
    browser.headers['Origin']='http://127.0.0.1:3510'
    assert browser.post('/api/workbench/v1/session',headers={'X-OpenClass-Launch-Token':'x'*48}).status_code==200
    imported=browser.post('/api/workspace/import-ridoc',files={'file':('portable.ridoc',Path(os.environ['PORTABLE_FILE']).read_bytes())})
    assert imported.status_code==200,imported.text
    package=imported.json();lesson_id=package['active_lesson_id'];store=get_ridoc_workspace_store()
    archive=store.load_archive(owner,package_id=package['id'])
    assert set(archive.package.lessons[lesson_id].branches)=={'main','study'}
    assert len(archive.assets)==1
    digest,png=next(iter(archive.assets.items()));asset_id='basset_0123456789ab_'+digest[:32]
    image=browser.get('/api/board-assets/'+asset_id+'/content');assert image.status_code==200,image.text
    assert image.content==png
    history=browser.get('/api/lessons/'+lesson_id+'/history/nodes?scope=all')
    assert history.status_code==200,history.text
    assert 'Complete fixture question' in history.text and 'Complete fixture answer' in history.text
    lesson=store.load_package(owner,package_id=package['id']).lessons[0]
    assert 'x^2 + 1' in str(lesson.board_document.model_dump())
    content=lesson.board_document.content_json
    content['content'].append({'type':'paragraph','content':[{'type':'text','text':'Continued editing on this machine.'}]})
    updated=store.commit_document(owner_user_id=owner,package_id=package['id'],lesson_id=lesson_id,expected_revision=archive.revision.value,
        transaction_id='portable-continued-edit',document=rebuild_document_from_content_json(lesson.board_document,content))
    assert updated.revision!=archive.revision.value
    assert 'Continued editing on this machine.' in store.load_package(owner,package_id=package['id']).lessons[0].board_document.content_text
'''


def run_script(script,root,export):
    env={k:v for k,v in os.environ.items() if not k.startswith(('OPENCLASS_','AI_USAGE_'))}
    env.update(PORTABLE_ROOT=str(root),PORTABLE_FILE=str(export),PYTHONPATH=str(Path(__file__).resolve().parents[1]))
    result=subprocess.run([sys.executable,'-c',script],env=env,capture_output=True,text=True,timeout=60)
    assert result.returncode==0,result.stdout+result.stderr


def test_complete_portable_course_needs_no_original_profile_account_or_sources(tmp_path):
    export=tmp_path/'portable.ridoc'
    run_script(CREATE_SCRIPT,tmp_path/'machine-a',export)
    shutil.rmtree(tmp_path/'machine-a')
    run_script(OPEN_SCRIPT,tmp_path/'machine-b',export)


if __name__=='__main__':
    run_script(CREATE_SCRIPT,Path(sys.argv[1]),Path(sys.argv[2]))

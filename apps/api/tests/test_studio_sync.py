import hashlib
import httpx
import pytest
from fastapi import HTTPException
from openclass_core.models import CoursePackage
from openclass_core.services.lesson_factory import create_empty_lesson
from openclass_core.services.rich_document import build_document
from openclass_core.services.ridoc_v3 import new_ridoc_v3
from openclass_core.services.ridoc_workspace_store import RidocWorkspaceStore
from openclass_core.services.studio_state import StudioStateStore
from openclass_core.services.studio_platform_client import PlatformClient
from openclass_core.services import studio_sync


def setup(tmp_path,monkeypatch):
    monkeypatch.setenv('OPENCLASS_STUDIO_PLATFORM_ORIGIN','https://platform.example')
    source=RidocWorkspaceStore(tmp_path/'source',tmp_path/'source.sqlite3')
    lesson=create_empty_lesson('Portable history')
    package=CoursePackage(title='Portable history',summary='',lessons=[lesson],package_kind='standalone')
    record=source.import_archive('source',new_ridoc_v3(package))
    source.commit_document(owner_user_id='source',package_id=package.id,lesson_id=lesson.id,expected_revision=record.revision,
        transaction_id='source-edit',document=build_document(title=lesson.title,document_id=lesson.board_document.id,content_text='# Saved learning result'))
    archive=source.load_archive('source',package_id=package.id)
    raw=source.package_path('source',package.id).read_bytes()
    local=RidocWorkspaceStore(tmp_path/'local',tmp_path/'local.sqlite3')
    calls=[]
    def download(request):
        calls.append(str(request.url))
        return httpx.Response(200,content=raw,headers={'X-RIDOC-Revision':archive.revision.value,
            'X-File-SHA256':hashlib.sha256(raw).hexdigest(),'X-Repository-Can-Write':'true'})
    client=PlatformClient(StudioStateStore(tmp_path/'profile'),'profile',transport=httpx.MockTransport(download))
    client.put('connection',{'origin':client.origin,'access_token':'model-independent-token','account_id':'account','token_id':'t','expires_at':9999999999,'scopes':['repositories.read','repositories.write']})
    monkeypatch.setattr(studio_sync,'local_platform_client',lambda _:client)
    monkeypatch.setattr(studio_sync,'get_ridoc_workspace_store',lambda:local)
    return local,client,package,archive,raw,calls


def test_pull_retries_complete_one_verified_import_without_accounts_or_originals(tmp_path,monkeypatch):
    local,client,package,archive,raw,calls=setup(tmp_path,monkeypatch)
    request=studio_sync.PullCourse(operation_id='pull_operation_123',revision=archive.revision.value)
    first=studio_sync.pull('profile','remote-course',request)
    assert first.id==package.id
    assert local.load_archive('profile',package_id=package.id).revision.value==archive.revision.value
    assert studio_sync.pull('profile','remote-course',request).id==first.id
    assert len(calls)==1
    assert len(local.list_packages('profile'))==1
    with pytest.raises(HTTPException):studio_sync.pull('profile','different-course',request)


def test_pull_recovers_after_the_package_is_saved_but_sync_receipt_write_fails(tmp_path,monkeypatch):
    local,client,package,archive,raw,calls=setup(tmp_path,monkeypatch)
    original=client.put
    def fail_once(name,value):
        if name.startswith('sync:'):raise OSError('simulated state interruption')
        original(name,value)
    monkeypatch.setattr(client,'put',fail_once)
    request=studio_sync.PullCourse(operation_id='pull_operation_123',revision=archive.revision.value)
    with pytest.raises(OSError):studio_sync.pull('profile','remote-course',request)
    assert local.load_archive('profile',package_id=package.id).revision.value==archive.revision.value
    monkeypatch.setattr(client,'put',original)
    assert studio_sync.pull('profile','remote-course',request).id==package.id
    assert len(calls)==1
    assert len(local.list_packages('profile'))==1


def test_push_retries_keep_the_original_remote_baseline_and_never_add_history(tmp_path,monkeypatch):
    from email.parser import BytesParser
    from email.policy import default
    import json
    local,client,package,archive,raw,calls=setup(tmp_path,monkeypatch)
    local.import_archive('profile',archive)
    requests=[]
    def upload(request):
        message=BytesParser(policy=default).parsebytes(b'Content-Type: '+request.headers['content-type'].encode()+b'\r\n\r\n'+request.content)
        metadata=json.loads(next(part.get_payload(decode=True) for part in message.iter_parts() if part.get_param('name',header='content-disposition')=='metadata'))
        requests.append(metadata)
        return httpx.Response(200,json={'operation_id':metadata['operation_id'],'repository_id':'remote',
            'ridoc_revision':metadata['ridoc_revision'],'uploaded_sha256':metadata['file_sha256'],'file_sha256':metadata['file_sha256'],'version_id':'v1'})
    client.transport=httpx.MockTransport(upload)
    request=studio_sync.PushCourse(operation_id='push_operation_123',expected_ridoc_revision=archive.revision.value)
    before=local.package_path('profile',package.id).read_bytes()
    one=studio_sync.push('profile',package.id,request)
    assert studio_sync.push('profile',package.id,request)==one
    assert requests[0]==requests[1]
    assert local.package_path('profile',package.id).read_bytes()==before

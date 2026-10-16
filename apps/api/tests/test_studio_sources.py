import os
from pathlib import Path
import subprocess
import sys


def test_local_original_and_ordinary_text_remain_available_without_cloud(tmp_path):
    script='''
import hashlib,os
from pathlib import Path
from unittest.mock import patch
from openclass_core.services.studio_runtime_config import StudioRuntimeConfig
from openclass_core.studio_main import create_app
app=create_app(StudioRuntimeConfig(Path(os.environ['STUDIO_TEST_ROOT']),'x'*48))
from openclass_core.services.source_ingestion_service import source_ingestion_service
from openclass_core.models import CoursePackage
from openclass_core.services.source_basic_cache import prepare_basic_content,load_basic_content
service=source_ingestion_service.google_service
owner=app.state.local_studio_identity.principal.id
raw=b'# Heading\\nExact original text with meaningful spacing.\\n'
with patch('httpx.Client.request',side_effect=AssertionError('Offline ordinary reading must not call a cloud service')):
    source=service.queue_file(owner_user_id=owner,package_id='local-course',file_name='source.md',content=raw,mime_type='text/markdown')
    assert source.metadata['source_backend']=='local' and 'google_original' not in source.metadata
    assert source.metadata['local_original']['sha256']==hashlib.sha256(raw).hexdigest()
    source,data=prepare_basic_content(service,source)
    assert 'Exact original text' in str(data)
    assert service.download(owner_user_id=owner,package_id='local-course',source_id=source.id)[1]==raw
    stored=service.storage.path(source.metadata['local_original']['bucket'],source.metadata['local_original']['name'])
    stored.write_bytes(b'corrupt')
    try:service.download(owner_user_id=owner,package_id='local-course',source_id=source.id)
    except Exception:pass
    else:raise AssertionError('A corrupt original must not be returned')
print('local-source-offline-ok')
'''
    env={k:v for k,v in os.environ.items() if not k.startswith(('OPENCLASS_','AI_USAGE_'))}
    env.update(STUDIO_TEST_ROOT=str(tmp_path),PYTHONPATH=str(Path(__file__).resolve().parents[1]))
    result=subprocess.run([sys.executable,'-c',script],env=env,capture_output=True,text=True,timeout=60)
    assert result.returncode==0,result.stdout+result.stderr
    assert 'local-source-offline-ok' in result.stdout

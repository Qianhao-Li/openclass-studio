"""Refresh short-lived external OIDC assertions for Google Workload Identity Federation.

The issuer key belongs to OpenClass. No Google service-account key is downloaded.
Only the configured subject/audience can impersonate the scoped source account.
"""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
import jwt
from cryptography.hazmat.primitives import serialization

def refresh_subject_token() -> None:
    key_file = os.getenv('OPENCLASS_GOOGLE_FEDERATION_KEY_FILE', '')
    if not key_file:
        return
    credentials_file = os.getenv('GOOGLE_APPLICATION_CREDENTIALS', '')
    config = json.loads(Path(credentials_file).read_text())
    if config.get('type') != 'external_account' or not config.get('audience', '').startswith('//iam.googleapis.com/projects/'):
        raise ValueError('Invalid Google federation credential configuration')
    target = Path(config['credential_source']['file'])
    issuer = os.environ['OPENCLASS_GOOGLE_FEDERATION_ISSUER']
    audience = os.environ['OPENCLASS_GOOGLE_FEDERATION_AUDIENCE']
    raw = Path(key_file).read_bytes()
    key = serialization.load_pem_private_key(raw, password=None)
    public = key.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    now = int(time.time())
    token = jwt.encode({'iss': issuer, 'sub': 'openclass-source-runtime', 'aud': audience, 'iat': now, 'exp': now + 300}, raw, algorithm='RS256', headers={'kid': hashlib.sha256(public).hexdigest()[:24]})
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix='.source-identity-', dir=target.parent)
    try:
        with os.fdopen(descriptor, 'w') as stream:
            stream.write(token)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)

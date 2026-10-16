"""Pure-file, stdlib-only inspection of an already owner-bound legacy credential pair.

Callers must derive both paths from the same verified OpenClass owner. Importing
this module and its inspection functions never initializes storage or contacts a provider.
"""
from __future__ import annotations
import base64
import hashlib
import json
from pathlib import Path
PI_FINGERPRINT_FILE = '.openclass-codex-auth.sha256'

class CodexLegacyCredentialError(ValueError):

    def __init__(self, reason):
        self.reason = reason
        super().__init__('Legacy credential evidence is inconsistent; both copies must be preserved.')

def credential_contents_checksum(auth) -> str:
    return hashlib.sha256(json.dumps(auth, sort_keys=True, separators=(',', ':')).encode()).hexdigest()

def _token_identity(access):
    try:
        payload = access.split('.')[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + '=' * (-len(payload) % 4)))
        subject = claims.get('sub')
        if not isinstance(subject, str) or not subject:
            raise ValueError()
        return subject
    except (ValueError, TypeError, IndexError, AttributeError):
        raise CodexLegacyCredentialError('identity_unavailable') from None

def _coordinated_managed_auth(managed, refreshed):
    tokens = managed.get('tokens', {})
    if not isinstance(refreshed, dict) or refreshed.get('type') != 'oauth' or refreshed.get('accountId') != tokens.get('account_id'):
        raise CodexLegacyCredentialError('account_mismatch')
    if not all((isinstance(refreshed.get(key), str) and refreshed[key] for key in ('access', 'refresh'))):
        raise CodexLegacyCredentialError('incomplete_pi_credential')
    if refreshed['access'] == tokens.get('access_token') and refreshed['refresh'] == tokens.get('refresh_token'):
        return managed
    if _token_identity(tokens.get('access_token')) != _token_identity(refreshed['access']):
        raise CodexLegacyCredentialError('identity_mismatch')
    return {**managed, 'tokens': {**tokens, 'access_token': refreshed['access'], 'refresh_token': refreshed['refresh']}}

def _legacy_auth_candidate(managed_auth_path: Path, pi_agent_dir: Path):
    source, pi_home = (Path(managed_auth_path), Path(pi_agent_dir))
    pi_path, fingerprint_path = (pi_home / 'auth.json', pi_home / PI_FINGERPRINT_FILE)
    if any((path.is_symlink() for path in (source.parent.parent, source.parent, source, pi_home.parent, pi_home, pi_path, fingerprint_path))):
        raise CodexLegacyCredentialError('linked_credential_path')
    try:
        source_bytes = source.read_bytes()
        managed = json.loads(source_bytes)
        tokens = managed.get('tokens') if isinstance(managed, dict) else None
        if not isinstance(tokens, dict) or not all((isinstance(tokens.get(key), str) and tokens[key] for key in ('access_token', 'refresh_token', 'account_id'))):
            raise CodexLegacyCredentialError('managed_credential_invalid')
    except CodexLegacyCredentialError:
        raise
    except (OSError, ValueError, TypeError):
        raise CodexLegacyCredentialError('managed_credential_unavailable') from None
    proof = {'status': 'managed', 'reason': 'pi_absent', 'pi_refresh_preserved': False, 'retain_legacy_pi': False}
    if not pi_path.exists():
        return (managed, {**proof, 'auth_sha256': credential_contents_checksum(managed)})
    try:
        pi_bytes = pi_path.read_bytes()
        pi_auth = json.loads(pi_bytes)
        if not isinstance(pi_auth, dict):
            raise CodexLegacyCredentialError('pi_credential_invalid')
        refreshed = pi_auth.get('openai-codex')
        if refreshed is None:
            return (managed, {**proof, 'auth_sha256': credential_contents_checksum(managed)})
        if not isinstance(refreshed, dict) or refreshed.get('type') != 'oauth' or (not all((isinstance(refreshed.get(key), str) and refreshed[key] for key in ('access', 'refresh', 'accountId')))):
            raise CodexLegacyCredentialError('incomplete_pi_credential')
        fingerprint = fingerprint_path.read_text().strip() if fingerprint_path.exists() else ''
        if fingerprint != hashlib.sha256(source_bytes).hexdigest():
            candidate = managed
            proof.update(reason='managed_auth_changed' if fingerprint else 'fingerprint_absent', retain_legacy_pi=True)
        else:
            candidate = _coordinated_managed_auth(managed, refreshed)
            changed = candidate != managed
            proof.update(status='promote_pi' if changed else 'managed', reason='matching_source_fingerprint' if changed else 'credentials_match', pi_refresh_preserved=changed)
        if source.read_bytes() != source_bytes or pi_path.read_bytes() != pi_bytes:
            raise CodexLegacyCredentialError('credential_changed_during_inspection')
        return (candidate, {**proof, 'auth_sha256': credential_contents_checksum(candidate)})
    except CodexLegacyCredentialError:
        raise
    except (OSError, ValueError, TypeError):
        raise CodexLegacyCredentialError('credential_evidence_unavailable') from None

def legacy_codex_migration_proof(managed_auth_path: Path, pi_agent_dir: Path) -> dict:
    """Read-only expected checksum after coordination; never return credential values."""
    return _legacy_auth_candidate(managed_auth_path, pi_agent_dir)[1]

def inspect_legacy_codex_credentials(managed_auth_path: Path, pi_agent_dir: Path) -> dict:
    """Return managed / promote_pi / blocked, with safe reasons and no token material."""
    try:
        proof = legacy_codex_migration_proof(managed_auth_path, pi_agent_dir)
        return {key: proof[key] for key in ('status', 'reason', 'pi_refresh_preserved', 'retain_legacy_pi')}
    except CodexLegacyCredentialError as exc:
        return {'status': 'blocked', 'reason': exc.reason}

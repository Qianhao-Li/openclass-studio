"""One revocation boundary for account settings and compatibility endpoints."""
from __future__ import annotations
from openclass_core.services.external_connections import ExternalConnectionError

class ExternalConnectionLifecycle:

    def __init__(self, store, *, authorization=None, evidence=None):
        self.store = store
        self.authorization = authorization
        self.evidence = evidence

    def _record(self, owner, identifier):
        row = self.store.get_connection(owner, identifier)
        if row is None or row['status'] == 'disconnected':
            raise ExternalConnectionError('Connected account not found.', 404)
        return row

    def _cleanup(self, owner, identifier):
        try:
            if self.evidence is None:
                from openclass_core.services.external_turn_evidence import ExternalTurnEvidenceService
                self.evidence = ExternalTurnEvidenceService(connection_store=self.store)
            removed = self.evidence.cleanup_connection_cache(owner, identifier)
            return {'status': 'succeeded', 'removed': removed}
        except Exception:
            return {'status': 'retry_pending', 'removed': 0}

    def update(self, owner, identifier, expected_revision, *, enabled_capabilities=None, capability_scopes=None, resource_scope=None, status=None):
        row = self._record(owner, identifier)
        capabilities = None
        if enabled_capabilities is not None:
            selected = set(enabled_capabilities)
            if len(selected) != len(enabled_capabilities) or not selected.issubset(row['capabilities']):
                raise ExternalConnectionError('Select capabilities that this account actually granted.', 422)
            if row['provider'] == 'google' and 'calendar' in selected:
                raise ExternalConnectionError('Calendar access is not enabled in this release.', 422)
            capabilities = {key: {**value, 'enabled': key in selected} if isinstance(value, dict) else bool(value) and key in selected for key, value in row['capabilities'].items()}
        changed = self.store.update_connection(owner, identifier, expected_revision, status=status, capabilities=capabilities, resource_scope=resource_scope, capability_scopes=capability_scopes)
        return {**changed, 'cache_cleanup': self._cleanup(owner, identifier)}

    def disconnect(self, owner, identifier, expected_revision):
        row = self._record(owner, identifier)
        if row['provider'] == 'codex':
            from openclass_core.services.codex_connections import disconnect_codex_connection
            changed = disconnect_codex_connection(owner, identifier, expected_revision, store=self.store)
            return {**changed, 'upstream_revocation': 'local_only', 'cache_cleanup': self._cleanup(owner, identifier)}
        try:
            credentials = self.store.credentials_for_reauthorization(owner, identifier, row['provider'])
        except ExternalConnectionError:
            credentials = None
        changed = self.store.disconnect(owner, identifier, expected_revision)
        cleanup = self._cleanup(owner, identifier)
        upstream = 'could_not_attempt'
        if credentials is not None:
            try:
                if self.authorization is None:
                    from openclass_core.services.external_account_authorization import ExternalAccountAuthorization
                    self.authorization = ExternalAccountAuthorization(self.store)
                upstream = self.authorization.revoke_upstream(row, credentials)
            except Exception:
                upstream = 'failed'
        return {**changed, 'upstream_revocation': upstream, 'cache_cleanup': cleanup}

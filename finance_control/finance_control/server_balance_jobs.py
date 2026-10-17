"""Bounded in-memory jobs so a synchronous bank read never blocks HTTPServer."""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime, timezone
import threading
import time
from uuid import uuid4

from .administration import AdministrationError, _id, _revision
from .server_balance_gateway import _validated_output


_REQUEST_FIELDS = {'id', 'revision', 'confirmed', 'tan_method', 'tan_medium'}
_MAX_FINISHED = 8
_FINISHED_TTL = 15 * 60
_ERROR_CODES = frozenset({
    'bank_read_unavailable', 'bank_read_timeout', 'server_banking_unsupported',
    'bank_product_unavailable', 'invalid_bank_auth_selection', 'bank_read_busy',
    'invalid_request', 'vault_unavailable', 'authorization_required', 'auth_rejected',
    'bank_failure', 'invalid_bank_result', 'too_many_accounts',
    'duplicate_account', 'unsupported_platform', 'unknown_connection',
    'forbidden', 'stale_revision', 'unauthorized', 'invalid_bank',
})


class ServerBalanceJobs:
    """Keep only bounded status/results in RAM; the gateway owns bank validation."""

    def __init__(self, gateway, mappings=None):
        self.gateway = gateway
        self.mappings = mappings
        self._lock = threading.Lock()
        self._jobs = {}
        self._active = None

    def _prune(self):
        now = time.monotonic()
        for job_id, job in list(self._jobs.items()):
            if job['finished_at'] is not None and now - job['finished_at'] >= _FINISHED_TTL:
                del self._jobs[job_id]
        finished = sorted((job for job in self._jobs.values()
                           if job['finished_at'] is not None),
                          key=lambda job: job['finished_at'])
        for job in finished[:-_MAX_FINISHED]:
            del self._jobs[job['job_id']]

    def _run(self, job_id, actor, request):
        try:
            raw = self.gateway.read(actor, request)
            # The gateway validates its worker output; recheck at the RAM boundary
            # so a changed gateway cannot accidentally expose arbitrary fields.
            if type(raw) is not dict:
                raise AdministrationError('bank_read_unavailable', 503)
            result = _validated_output(json.dumps(raw, ensure_ascii=False,
                                                  separators=(',', ':')).encode('utf-8'))
            if result['status'] == 'ok':
                status, value = 'complete', {'result': result,
                                           'retrieved_at': datetime.now(timezone.utc).isoformat()}
            else:
                status, value = 'error', {'code': result['code']}
                if 'diagnostic' in result:
                    value['diagnostic'] = result['diagnostic']
        except AdministrationError as error:
            status, value = 'error', {'code': error.code if error.code in _ERROR_CODES
                                     else 'bank_read_unavailable'}
        except Exception:
            status, value = 'error', {'code': 'bank_read_unavailable'}
        with self._lock:
            job = self._jobs.get(job_id)
            if job is not None:
                job.update(status=status, finished_at=time.monotonic(), **value)
            if self._active == job_id:
                self._active = None
            self._prune()

    def start(self, actor, data):
        if type(data) is not dict or set(data) != _REQUEST_FIELDS:
            raise AdministrationError('invalid_action')
        if data['confirmed'] is not True:
            raise AdministrationError('confirmation_required')
        connection_id, revision = _id(data['id']), _revision(data['revision'])
        try:
            owner_id, bank_id, _bank_code = self.gateway._connection(
                actor, connection_id, revision)
            _id(owner_id)
        except AdministrationError:
            raise
        except Exception:
            raise AdministrationError('bank_read_unavailable', 503) from None
        with self._lock:
            self._prune()
            if self._active is not None:
                raise AdministrationError('bank_read_busy', 409)
            job_id = uuid4().hex
            self._jobs[job_id] = {'job_id': job_id, 'owner_id': owner_id,
                                  'connection_id': connection_id, 'revision': revision,
                                  'bank_id': bank_id,
                                  'status': 'running', 'finished_at': None}
            self._active = job_id
            try:
                thread = threading.Thread(target=self._run,
                                          args=(job_id, dict(actor), dict(data)), daemon=True)
                thread.start()
            except Exception:
                del self._jobs[job_id]
                self._active = None
                raise AdministrationError('bank_read_unavailable', 503) from None
            return {'job_id': job_id, 'status': 'running'}

    def state(self, actor, data):
        if type(data) is not dict or set(data) != {'job_id'}:
            raise AdministrationError('invalid_action')
        job_id = _id(data['job_id'])
        with self._lock:
            self._prune()
            job = self._jobs.get(job_id)
            if job is None or type(actor) is not dict or actor.get('id') != job['owner_id']:
                raise AdministrationError('unknown_bank_job', 404)
            connection_id, revision = job['connection_id'], job['revision']
        try:
            owner_id, _bank_id, _bank_code = self.gateway._connection(
                actor, connection_id, revision)
            if owner_id != job['owner_id']:
                raise AdministrationError('unknown_bank_job', 404)
        except AdministrationError:
            raise
        except Exception:
            raise AdministrationError('bank_read_unavailable', 503) from None
        with self._lock:
            self._prune()
            job = self._jobs.get(job_id)
            if job is None:
                raise AdministrationError('unknown_bank_job', 404)
            response = {'job_id': job_id, 'status': job['status']}
            if self.mappings is not None:
                response['mapping_available'] = True
            if job['status'] == 'complete':
                response['result'] = job['result']
            elif job['status'] == 'error':
                response['code'] = job['code']
                if 'diagnostic' in job:
                    response['diagnostic'] = job['diagnostic']
            return response

    def confirm(self, actor, data):
        """Hand only server-held, completed results to explicit balance adoption."""
        if type(data) is not dict or set(data) != {'job_id', 'confirmed', 'mappings'}:
            raise AdministrationError('balance_mapping_invalid')
        if data['confirmed'] is not True:
            raise AdministrationError('confirmation_required')
        job_id = _id(data['job_id'])
        if (type(data['mappings']) is not list or not data['mappings']
                or len(data['mappings']) > 20
                or any(type(item) is not dict
                       or set(item) != {'account_fingerprint', 'account_id'}
                       for item in data['mappings'])):
            raise AdministrationError('balance_mapping_invalid')
        if self.mappings is None:
            raise AdministrationError('bank_read_unavailable', 503)
        with self._lock:
            self._prune()
            job = self._jobs.get(job_id)
            if job is None or type(actor) is not dict or actor.get('id') != job['owner_id']:
                raise AdministrationError('unknown_bank_job', 404)
            if job['status'] != 'complete':
                raise AdministrationError('bank_job_not_complete', 409)
            context = {key: job[key] for key in ('job_id', 'owner_id', 'connection_id',
                                                'revision', 'bank_id', 'retrieved_at')}
            context['accounts'] = deepcopy(job['result']['accounts'])
        try:
            owner_id, bank_id, _bank_code = self.gateway._connection(
                actor, context['connection_id'], context['revision'])
        except AdministrationError:
            raise
        except Exception:
            raise AdministrationError('bank_read_unavailable', 503) from None
        if owner_id != context['owner_id'] or bank_id != context['bank_id']:
            raise AdministrationError('stale_revision', 409)
        # Recheck TTL after authorization; neither client amounts nor client
        # identity fields ever enter the trusted service context.
        with self._lock:
            self._prune()
            if self._jobs.get(job_id) is not job:
                raise AdministrationError('unknown_bank_job', 404)
        return self.mappings.commit(actor, deepcopy(data), context)

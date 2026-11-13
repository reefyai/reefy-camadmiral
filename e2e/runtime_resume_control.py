"""Faults exist only in this mounted synthetic E2E control module."""
import json
import os
import time
from pathlib import Path

from fastapi import Header, HTTPException
from fastapi.responses import JSONResponse
from camadmiral import app as control
from camadmiral.frigate import FrigateApiError, FrigateClient
from camadmiral.frigate_import import ImportStore, _refresh_capture

app = control.app

class ProcessLost(BaseException):
    pass

@app.post('/__e2e/runtime-resume', include_in_schema=False)
def exercise(options: dict, x_camadmiral_action: str | None = Header(default=None)):
    if x_camadmiral_action != 'synthetic-runtime-resume':
        raise HTTPException(status_code=400)
    result = {'restart_requests': 0, 'runtime_calls': []}
    repository = control._repository(required=True)
    with control.FRIGATE_LOCK, control.MEDIA_LOCK:
        target = control.load_frigate_targets(repository)[0]
        store = ImportStore(repository)
        links = store.links(target.target_id)
        row = links['managed_camera']
        class FaultClient(FrigateClient):
            def set_runtime_camera_enabled(self, key, enabled):
                result['runtime_calls'].append([key, enabled])
                super().set_runtime_camera_enabled(key, enabled)
                if key == 'managed_camera' and not enabled:
                    result['pause_accepted_ack_lost'] = True
                    raise FrigateApiError('target_unavailable')

            def restart(self):
                before = self.service_started_at()
                result['restart_requests'] += 1
                super().restart()
                deadline = time.monotonic() + 180
                while time.monotonic() < deadline:
                    try:
                        if self.service_started_at() > before + 2:
                            break
                    except FrigateApiError:
                        pass
                    time.sleep(.5)
                else:
                    raise FrigateApiError('target_unavailable')
                # Retain the real runtime override across versions. Some server
                # versions already persist it; reapply only when the server
                # discarded it. Neither saved YAML nor readiness budgets change.
                result['off_persisted_natively'] = not self.config()['cameras']['managed_camera']['enabled']
                if os.environ.get('FRIGATE_TEST_VERSION') == '0.18.2' and not result['off_persisted_natively']:
                    raise AssertionError('Real Frigate did not retain its runtime OFF override')
                if not result['off_persisted_natively']:
                    FrigateClient.set_runtime_camera_enabled(self, 'managed_camera', False)
                result['off_after_restart'] = not self.config()['cameras']['managed_camera']['enabled']
                if options.get('interrupt'):
                    result['interrupted'] = True
                    result.update(status='interrupted',capture_resume_pending=True,target_restart_pending=True)
                    Path('/var/lib/camadmiral/runtime-resume-evidence.json').write_text(json.dumps(result))
                    os._exit(77)  # Disposable E2E control process only.

        client = FaultClient(target)
        before = client.raw_config()
        try:
            _refresh_capture(repository, target, client, links, row)
            result['status'] = 'recovered'
        except ProcessLost:
            result['status'] = 'interrupted'
        except FrigateApiError as exc:
            result.update(status='error', code=exc.code)
        result['raw_config_preserved'] = client.raw_config() == before
        result['enabled'] = client.config()['cameras']['managed_camera']['enabled']
        result['fps'] = client.stats()['managed_camera']['camera_fps']
        saved = store.links(target.target_id)['managed_camera']
        result['capture_resume_pending'] = bool(saved.get('capture_resume_pending'))
        result['target_restart_pending'] = bool(saved.get('target_restart'))
        Path('/var/lib/camadmiral/runtime-resume-evidence.json').write_text(json.dumps(result))
        return JSONResponse(result)

@app.get('/__e2e/runtime-resume-state', include_in_schema=False)
def observe():
    repository = control._repository(required=True)
    target = control.load_frigate_targets(repository)[0]
    row = ImportStore(repository).links(target.target_id)['managed_camera']
    return {'capture_resume_pending': bool(row.get('capture_resume_pending')),
            'target_restart_pending': bool(row.get('target_restart'))}

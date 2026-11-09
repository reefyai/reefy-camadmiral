"""Private grouped-import journal and one-restart cutover orchestration."""
from __future__ import annotations

import copy
import time
import urllib.parse

from . import frigate_import as imp
from .frigate import FrigateApiError, FrigateClient

MEDIA_VERIFY_SECONDS = 90


def _runtime_source(value):
    # Frigate accepts both a scalar source and a one-item source list.
    return value if isinstance(value, str) else value[0]


class BatchCutover:
    def __init__(self, repository, target, job, reconcile_media, client, inventory=None):
        self.repository, self.target, self.job = repository, target, job
        self.reconcile_media, self.client, self.inventory = reconcile_media, client, inventory
        self.store = imp.ImportStore(repository)

    @property
    def batch(self):
        return self.job['batch']

    @property
    def rows(self):
        keys = set(self.batch['keys'])
        return [row for row in self.job['cameras'] if row['key'] in keys]

    def checkpoint(self, phase=None):
        if phase is not None:
            self.batch['phase'] = phase
        self.store.save_job(self.target.target_id, self.job)

    def remember(self, row, phase=None):
        if phase:
            row.update(status='running', phase=phase)
        row['batch_job_id'] = self.job['id']
        self.store.save_link(self.target.target_id, row['key'], row)
        self.checkpoint()

    def failure(self, row, exc):
        row.update(status='error', reason=exc.code if isinstance(exc, FrigateApiError) else 'import_failed', phase=None)
        self.remember(row)

    def read(self, operation):
        # Recovery can begin while the previously accepted restart is offline.
        deadline = time.monotonic() + imp.READER_RESTART_VERIFY_TIMEOUT_SECONDS
        while True:
            try:
                return operation()
            except FrigateApiError as exc:
                if (not imp._temporary_api_error(exc)
                        and not (exc.code == 'request_rejected' and exc.upstream_status == 500)) or time.monotonic() >= deadline:
                    raise
                time.sleep(1)

    def restart(self, kind):
        pending = self.batch.get('restart')
        if not pending or pending['kind'] != kind:
            pending = {'kind': kind, 'started_before': self.read(self.client.service_started_at),
                       'raw_hash': imp.digest(self.read(self.client.raw_config)), 'sent': False}
            self.batch['restart'] = pending
            self.checkpoint()
        if imp.digest(self.read(self.client.raw_config)) != pending['raw_hash']:
            raise FrigateApiError('rollback_configuration_conflict')
        if not pending['sent']:
            pending['sent'] = True
            self.checkpoint()
            imp._request_restart(self.client)
        deadline = time.monotonic() + imp.READER_RESTART_VERIFY_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            try:
                if self.client.service_started_at() > pending['started_before'] + 1:
                    self.client.config()
                    return
            except FrigateApiError as exc:
                if not imp._temporary_api_error(exc) and not (exc.code == 'request_rejected' and exc.upstream_status == 500):
                    raise
            time.sleep(1)
        # An intent persisted immediately before process loss may not have been
        # dispatched. Only a full old-epoch readiness budget permits a retry.
        pending['sent'] = False
        self.checkpoint()
        raise FrigateApiError('target_unavailable')

    def saved(self, rows):
        current = self.read(self.client.raw_config)
        configured = current.get('go2rtc', {}).get('streams', {})
        return all(current.get('cameras', {}).get(row['key']) == row['managed_camera']
                   and all(configured.get(k) == v for k, v in row['managed_streams'].items()) for row in rows)

    def save_config(self, payload, rows):
        deadline = time.monotonic() + 180
        while True:
            try:
                self.client.set_config(payload, requires_restart=True)
                return
            except FrigateApiError as exc:
                if not imp._temporary_api_error(exc) or time.monotonic() >= deadline:
                    raise
                try:
                    if self.saved(rows):
                        return
                    for row in rows:
                        imp._check_original(self.client, row)
                except FrigateApiError as read_error:
                    if not imp._temporary_api_error(read_error):
                        raise
                time.sleep(1)

    def wait_rows(self, rows, *, original=False):
        remaining = {row['key']: row for row in rows}
        deadline = time.monotonic() + MEDIA_VERIFY_SECONDS
        while remaining and time.monotonic() < deadline:
            for key, row in list(remaining.items()):
                try:
                    if original:
                        ready = (float(self.client.stats().get(key, {}).get('camera_fps') or 0) > 0
                                 and imp._running_inputs_match(self.client, key, row['original']['ffmpeg']['inputs']))
                    else:
                        ready = imp._reader_media_ready(self.client, row)
                    if ready:
                        del remaining[key]
                except FrigateApiError as exc:
                    if not imp._temporary_api_error(exc):
                        raise
            if remaining:
                time.sleep(1)
        return set(remaining)

    def disable_created(self, rows):
        changed = False
        for row in rows:
            if not row.get('created_camera'):
                continue
            if not row.get('camera_uuid') and row.get('candidate_id'):
                prior = self.repository.adoption_for_candidate(row['candidate_id'])
                if prior:
                    row['camera_uuid'] = prior['camera_uuid']
            if not row.get('camera_uuid'):
                continue
            with self.repository.connect() as connection:
                other = connection.execute('SELECT 1 FROM frigate_camera_selections WHERE camera_uuid=? AND target_id<>? LIMIT 1',
                                           (row['camera_uuid'], self.target.target_id)).fetchone()
            if other:
                raise FrigateApiError('restore_media_in_use')
            self.repository.set_camera_enabled(row['camera_uuid'], False)
            changed = True
        if changed:
            self.reconcile_media()

    def restore_rows(self, rows, *, reason='interrupted_import'):
        current = self.read(self.client.raw_config)
        safe, failed = [], set()
        for row in rows:
            try:
                imp._restore_guard(row, current)
                safe.append(row)
            except FrigateApiError:
                failed.add(row['key'])
                row.update(status='error', reason='rollback_required', phase=None, configuration_status='restore_required')
                self.remember(row)
        self.disable_created(safe)
        payload = {'cameras': {}, 'go2rtc': {'streams': {}}}
        configured = current.get('go2rtc', {}).get('streams', {})
        needs_save = False
        for row in safe:
            originals = {**row.get('released_streams', {}), **row['original_streams']}
            added = {name: '' for name in row.get('aliases', {}).values() if name in configured and name not in originals}
            if (current['cameras'].get(row['key']) != row['original'] or added
                    or any(configured.get(name) != value for name, value in originals.items())):
                needs_save = True
                payload['cameras'][row['key']] = imp._camera_write(row, row['original'], current['cameras'].get(row['key']))
                payload['go2rtc']['streams'].update({**added, **originals})
        if needs_save:
            self.client.set_config(payload, requires_restart=True)
            self.restart('restore')
        else:
            restart_required = False
            for row in safe:
                try:
                    for alias, value in {**row.get('released_streams', {}), **row['original_streams']}.items():
                        self.client.set_runtime_stream(alias, _runtime_source(value))
                    if not self.client.config().get('cameras', {}).get(row['key'], {}).get('enabled', True):
                        self.client.set_runtime_camera_enabled(row['key'], True)
                except FrigateApiError:
                    restart_required = True
            # A crash can occur after the restoration save and before its
            # restart intent. Saved originals with managed running inputs still
            # require one shared restart before their journal can be cleared.
            if restart_required or any(not imp._running_inputs_match(self.client, row['key'],
                    row['original']['ffmpeg']['inputs']) for row in safe):
                self.restart('restore')
        failed |= self.wait_rows(safe, original=True)
        for row in safe:
            if row['key'] in failed:
                row.update(status='error', reason='rollback_required', phase=None, configuration_status='restore_required')
                self.remember(row)
                continue
            imp._finish_restore(self.repository, self.target, row['key'], row, self.client, self.store)
            row.update(status='error', reason=row.get('reason') or reason, phase=None, configuration_status='restored')
        self.checkpoint()
        return failed

    def release(self):
        # Persist intent before any runtime action. Saved Frigate YAML stays
        # unchanged on the normal path, so its next start remains recoverable.
        raw = self.client.raw_config()
        imp._guard_restart_runtime_controls(raw, self.client.config(), {})
        for row in self.rows:
            imp._check_original(self.client, row)
            aliases = imp._media_aliases(row['original'], raw.get('go2rtc', {}).get('streams', {})) | set(row['original_streams'])
            for key, other in raw['cameras'].items():
                if key != row['key'] and aliases & imp._media_aliases(other, raw.get('go2rtc', {}).get('streams', {})):
                    raise FrigateApiError('shared_handoff_source')
            row.update(handoff=True, state='paused', paused_camera=copy.deepcopy(row['original']),
                       released_streams={name: raw['go2rtc']['streams'][name] for name in aliases},
                       paused_streams={name: raw['go2rtc']['streams'][name] for name in aliases},
                       target_api_url=self.target.api_url, configuration_status='restore_required')
            self.remember(row, 'releasing_original')
        try:
            for row in self.rows:
                self.client.set_runtime_camera_enabled(row['key'], False)
                for alias, value in sorted(row['released_streams'].items()):
                    if alias in self.client.runtime_streams():
                        imp._delete_runtime_alias(self.client, alias, _runtime_source(value))
        except FrigateApiError:
            # A target lacking reliable runtime controls still releases the
            # entire group with one shared initial restart.
            payload = {'cameras': {}, 'go2rtc': {'streams': {}}}
            for row in self.rows:
                imp._check_original(self.client, row)
                row['paused_camera'] = {**copy.deepcopy(row['original']), 'enabled': False}
                row['paused_streams'] = {name: ['rtsp://127.0.0.1:1/camadmiral-handoff'] for name in row['released_streams']}
                payload['cameras'][row['key']] = row['paused_camera']
                payload['go2rtc']['streams'].update(row['paused_streams'])
                self.remember(row)
            self.client.set_config(payload, requires_restart=True)
            self.batch['persistent_release'] = True
            self.checkpoint()
            self.restart('release')

    def stage(self, row):
        imp._check_original(self.client, row)
        if not row.get('camera_uuid'):
            row['candidate_id'] = 'frigate-import:' + self.target.target_id + ':' + row['key']
            prior = self.repository.adoption_for_candidate(row['candidate_id'])
            if prior:
                source = next(iter(row['sources'].values()))
                expected = {role: 'import-' + sid[:24] for role, sid in row['roles'].items()}
                if prior['role_tokens'] != expected or self.repository.credentials_for_candidate(row['candidate_id']) != (source['username'], source['password']):
                    raise FrigateApiError('camera_changed_refresh_preview')
                row['camera_uuid'] = prior['camera_uuid']
                row['stream_map'] = {sid: next(s['stream_uuid'] for s in prior['streams'] if s['profile_token'] == 'import-' + sid[:24]) for sid in row['sources']}
            else:
                # The active original feed supplies trusted credentials and URLs.
                # Metadata remains unknown until the post-release relay probe.
                profiles = [{'token': 'import-' + sid[:24], 'name': 'Imported stream', 'uri': source['uri'],
                             'width': 0, 'height': 0, 'fps': 0, 'encoding': None, 'bitrate_kbps': 0,
                             'source_kind': 'manual_rtsp'} for sid, source in row['sources'].items()]
                source = next(iter(row['sources'].values()))
                row['created_camera'] = True
                self.remember(row, 'adding_camera')
                adopted = self.repository.adopt({'candidate_uuid': row['candidate_id'], 'display_name': row['name'],
                    'ip': urllib.parse.urlsplit(source['uri']).hostname}, source['username'], source['password'], profiles,
                    {role: 'import-' + sid[:24] for role, sid in row['roles'].items()}, camera_origin='direct',
                    reject_source_keys={imp._rtsp_source_key(s['uri']) for s in row['sources'].values()}, initially_enabled=False)
                row['camera_uuid'] = adopted['camera_uuid']
                row['stream_map'] = {sid: next(s['stream_uuid'] for s in adopted['streams'] if s['profile_token'] == 'import-' + sid[:24]) for sid in row['sources']}
            row['created_camera'] = True
        if row.get('match_signature') and row['match_signature'] != imp.match_signature(self.repository, row['camera_uuid']):
            raise FrigateApiError('camera_changed_refresh_preview')
        row['aliases'] = {role: 'camadmiral_' + row['camera_uuid'].replace('-', '_') + '_import_' + role for role in ('record', 'detect')}
        self.remember(row, 'preparing_relay')

    def validate(self, row):
        camera = imp._camera(self.repository, row['camera_uuid'])
        host = imp._target_host(self.repository, self.target, self.inventory)
        streams = [s for s in camera['streams'] if s['stream_uuid'] in row['stream_map'].values()]
        for index, stream in enumerate(streams, 1):
            row.update(stream_index=index, stream_total=len(streams))
            self.remember(row, 'validating_relay')
            deadline = time.monotonic() + MEDIA_VERIFY_SECONDS
            while True:
                result = imp._probe_import(row, imp.relay_url(self.repository, host, stream), 'camadmiral', self.repository.rtsp_access_password())
                if result.status == 'ready':
                    break
                if result.status not in {'unavailable', 'timeout'} or time.monotonic() >= deadline:
                    raise FrigateApiError('relay_' + result.status)
                time.sleep(1)
            try:
                imp.snapshot_frame(stream['stream_key'], rtsp_password=self.repository.rtsp_access_password(), width=320)
            except imp.SnapshotError:
                raise FrigateApiError('relay_video_unavailable') from None
            self.repository.record_probe_results({stream['stream_uuid']: result})
            if row.get('created_camera'):
                with self.repository.connect() as connection:
                    connection.execute('UPDATE onvif_profiles SET width=?,height=?,encoding=?,fps=? WHERE camera_uuid=? AND profile_uuid=(SELECT profile_uuid FROM managed_streams WHERE stream_uuid=?)',
                                       (result.width or 0, result.height or 0, result.video_codec, result.fps or 0, row['camera_uuid'], stream['stream_uuid']))
                    connection.commit()
        row['managed_camera'], row['managed_streams'] = imp.build_managed(self.repository, row, host)
        # Clear the durable runtime OFF override created by our own pause.
        # Frigate 0.18 otherwise keeps implicitly enabled entries disabled.
        row['managed_camera'].setdefault('enabled', True)
        self.remember(row)

    def finish(self):
        accepted = [row for row in self.rows if row['key'] in self.batch.get('accepted', [])]
        for row in accepted:
            if not self.saved([row]):
                raise FrigateApiError('rollback_configuration_conflict')
        sources = imp.source_connection_revisions(self.repository.managed_stream_runtime_sources())
        for row in accepted:
            try:
                from .camera_identity import learn_import_identity
                learn_import_identity(self.repository, row['camera_uuid'], self.inventory)
            except Exception:
                pass
            row['identity_status'] = 'verified' if row['camera_uuid'] in self.repository.camera_identity_links() else 'unavailable'
            camera = imp._camera(self.repository, row['camera_uuid'])
            roles = imp._role_streams(camera)
            self.repository.select_frigate_camera(self.target.target_id, row['camera_uuid'])
            signature = imp.digest({'camera': row['managed_camera'], 'streams': row['managed_streams']})
            self.repository.record_frigate_attempt(self.target.target_id, row['camera_uuid'], row['key'], roles['record']['stream_uuid'], roles['detect']['stream_uuid'], signature)
            self.repository.complete_frigate_attempt(self.target.target_id, row['camera_uuid'], status='applied', applied_hash=signature)
            source = imp._camera_source_revisions(self.client, row, camera, sources)
            if source is not None:
                row['relay_source_revisions'] = source
            row.update(state='active', status='applied', reason=None, configuration_status=None, phase=None,
                       stream_index=None, stream_total=None, relay_runtime_revision=imp._relay_runtime_revision(self.repository))
            self.store.save_link(self.target.target_id, row['key'], row)
        self.job['status'] = 'completed'
        self.checkpoint('partial_restore' if self.batch.get('pending_restore') else 'complete')

    def verify(self):
        accepted = [row for row in self.rows if row['key'] in self.batch.get('accepted', [])]
        failed = self.wait_rows(accepted)
        if failed:
            self.batch['accepted'] = [key for key in self.batch['accepted'] if key not in failed]
            for row in accepted:
                if row['key'] in failed:
                    self.failure(row, FrigateApiError('capture_verification_failed'))
        restore = [row for row in self.rows if row['key'] not in self.batch['accepted']]
        self.checkpoint('rollback_failed')
        pending = self.restore_rows(restore) if restore else set()
        self.batch['pending_restore'] = sorted(pending)
        self.checkpoint('verified')
        self.finish()

    def abort(self, exc):
        self.checkpoint('rollback_all')
        pending = self.restore_rows(self.rows, reason=exc.code if isinstance(exc, FrigateApiError) else 'import_failed')
        self.batch['pending_restore'] = sorted(pending)
        self.job['status'] = 'error'
        self.checkpoint('partial_restore' if pending else 'complete')

    def run(self):
        self.release()
        for row in self.rows:
            try:
                self.stage(row)
            except Exception as exc:
                self.failure(row, exc)
        for row in self.rows:
            if row['status'] != 'error' and row.get('created_camera'):
                self.repository.set_camera_enabled(row['camera_uuid'], True)
        self.reconcile_media()
        for row in self.rows:
            if row['status'] == 'error':
                continue
            try:
                self.validate(row)
                imp._check_original(self.client, row)
                self.batch['accepted'].append(row['key'])
            except Exception as exc:
                self.failure(row, exc)
        accepted = [row for row in self.rows if row['key'] in self.batch['accepted']]
        self.disable_created([row for row in self.rows if row['key'] not in self.batch['accepted']])
        payload = {'cameras': {}, 'go2rtc': {'streams': {}}}
        for row in accepted:
            imp._check_original(self.client, row)
            row.update(state='applying', configuration_status='restore_required')
            self.remember(row, 'saving_frigate')
            payload['cameras'][row['key']] = imp._camera_write(row, row['managed_camera'], row['paused_camera'])
            payload['go2rtc']['streams'].update(row['managed_streams'])
        if not accepted:
            self.abort(FrigateApiError('import_failed'))
            return self.job
        self.checkpoint('saving')
        self.save_config(payload, accepted)
        self.checkpoint('cutover')
        for row in accepted:
            self.remember(row, 'waiting_for_frigate')
        self.restart('cutover')
        self.checkpoint('verify')
        self.verify()
        return self.job


def execute_batch(repository, target, job_id, selected, keep_existing, inventory, reconcile_media, client=None):
    client = client or FrigateClient(target)
    store = imp.ImportStore(repository)
    job = store.job(target.target_id, job_id)
    if job['api_url'] != target.api_url:
        raise FrigateApiError('target_changed_refresh_preview')
    keys = {row['key'] for row in job['cameras']}
    if not selected or len(set(selected)) != len(selected) or not set(selected) <= keys:
        raise FrigateApiError('invalid_selection')
    if job.get('batch', {}).get('phase') not in (None, 'complete'):
        recover_batches(repository, target, reconcile_media, client)
        job = store.job(target.target_id, job_id)
        if job.get('batch', {}).get('phase') != 'complete':
            raise FrigateApiError('rollback_required')
    imp.start_job(job, selected, keep_existing)
    eligible = []
    effective, raw = client.config(), client.raw_config()
    try:
        imp._guard_restart_runtime_controls(raw, effective, {})
        operator_paused = False
    except FrigateApiError:
        operator_paused = True
    links = store.links(target.target_id)
    for row in job['cameras']:
        if row['key'] not in selected or row.get('status') == 'applied':
            continue
        if row['category'] == 'managed' or links.get(row['key'], {}).get('state') == 'active':
            row['status'] = 'already_managed'
            continue
        if not row['enabled'] or (not row['can_import'] and not (row['key'] in keep_existing and row.get('can_keep_existing'))):
            row['status'] = 'pending'
            continue
        try:
            if operator_paused:
                raise FrigateApiError('import_source_conflict')
            if row['key'] in links:
                imp.restore(repository, target, row['key'], client, reconcile_media)
            for name in ('handoff', 'paused_camera', 'released_streams', 'paused_streams', 'batch_job_id'):
                row.pop(name, None)
            row.update(state='preparing', reason=None, configuration_status='unchanged', import_job_id=job_id)
            imp._check_original(client, row)
            imp._refresh_masked_import_sources(row, client)
            if row.get('camera_uuid') and any(link['camera_uuid'] == row['camera_uuid'] for key, link in links.items() if key != row['key']):
                raise FrigateApiError('camera_already_linked')
            eligible.append(row['key'])
        except Exception as exc:
            row.update(status='error', reason=exc.code if isinstance(exc, FrigateApiError) else 'import_failed', phase=None)
    if not eligible:
        job['status'] = 'completed'; store.save_job(target.target_id, job); return job
    job['batch'] = {'phase': 'staging', 'keys': eligible, 'accepted': []}
    batch = BatchCutover(repository, target, job, reconcile_media, client, inventory)
    # API readiness precedes first-frame readiness during Frigate startup.
    # Wait for the selected original feeds together without adding readers.
    unavailable = batch.wait_rows(batch.rows, original=True)
    if unavailable:
        for row in batch.rows:
            if row['key'] in unavailable:
                row.update(status='error', reason='source_unavailable', phase=None, configuration_status='unchanged')
        batch.batch['keys'] = [key for key in eligible if key not in unavailable]
    if not batch.rows:
        job['status'] = 'completed'
        batch.checkpoint('complete')
        return job
    batch.checkpoint()
    try:
        return batch.run()
    except Exception as exc:
        try:
            batch.abort(exc)
        except Exception:
            job['status'] = 'error'
            for row in batch.rows:
                row.update(status='error', reason='rollback_required', configuration_status='restore_required', phase=None)
            batch.checkpoint('rollback_all')
        return job


def recover_batches(repository, target, reconcile_media, client):
    store = imp.ImportStore(repository)
    with repository.connect() as connection:
        records = connection.execute('SELECT job_id,payload FROM frigate_import_jobs WHERE target_id=?', (target.target_id,)).fetchall()
    pending = set()
    for job_id, payload in records:
        job = store.decode(payload, job_id)
        phase = job.get('batch', {}).get('phase')
        if phase in (None, 'complete'):
            continue
        batch = BatchCutover(repository, target, job, reconcile_media, client)
        try:
            if (job['api_url'] != target.api_url
                    or any(row.get('target_api_url', target.api_url) != target.api_url for row in batch.rows)):
                raise FrigateApiError('rollback_target_changed')
            if phase == 'partial_restore':
                rows = [row for row in batch.rows if row['key'] in batch.batch.get('pending_restore', [])]
                failed = batch.restore_rows(rows)
                batch.batch['pending_restore'] = sorted(failed)
                batch.checkpoint('partial_restore' if failed else 'complete')
            elif phase == 'verified':
                batch.finish()
            elif phase in {'saving', 'cutover', 'verify', 'rollback_failed'} and batch.saved(
                    [row for row in batch.rows if row['key'] in batch.batch.get('accepted', [])]):
                if phase != 'rollback_failed':
                    batch.restart('cutover')
                batch.verify()
            else:
                batch.abort(FrigateApiError('interrupted_import'))
        except Exception:
            job['status'] = 'error'
            for row in batch.rows:
                if row.get('state') != 'active':
                    row.update(status='error', reason='rollback_required', configuration_status='restore_required', phase=None)
            batch.checkpoint()
        if batch.batch['phase'] != 'complete':
            pending.add(job_id)
    return pending

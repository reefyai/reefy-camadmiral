"""Explicit Frigate takeover. Credentials and rollback snapshots stay encrypted.

This is an internal UI workflow, not a public integration API. Frigate remains
responsible for recording, history, analytics, and operator camera settings.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import time
import urllib.parse
import uuid

from .crypto import decrypt_password, encrypt_password
from .frigate import FrigateApiError, FrigateClient, media_host_for_mode
from .media import SnapshotError, probe_source, snapshot_frame, source_connection_revisions
from .storage import _rtsp_source_key

MAX_CAMERAS = 256
MAX_SOURCES = 8
# A relay restart can reset RTP timing without changing any saved source URL.
RELAY_RUNTIME_REVISION = uuid.uuid4().hex
READER_RESTART_VERIFY_TIMEOUT_SECONDS = 180


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


class ImportStore:
    def __init__(self, repository):
        self.repository = repository

    def encode(self, value, context):
        return encrypt_password(json.dumps(value), context, self.repository.master_key)

    def decode(self, value, context):
        return json.loads(decrypt_password(value, context, self.repository.master_key))

    def save_job(self, target_id, job):
        with self.repository.connect() as c:
            c.execute('INSERT INTO frigate_import_jobs VALUES (?,?,?,?) ON CONFLICT(job_id) DO UPDATE SET payload=excluded.payload,created_at=excluded.created_at',
                      (job['id'], target_id, self.encode(job, job['id']), str(time.time())))
            old = c.execute('SELECT job_id,payload FROM frigate_import_jobs WHERE target_id=? ORDER BY CAST(created_at AS REAL) DESC LIMIT -1 OFFSET 20', (target_id,)).fetchall()
            for old_id, payload in old:
                previous = self.decode(payload, old_id)
                if previous.get('batch', {}).get('phase') not in (None, 'complete'):
                    continue
                c.execute('DELETE FROM frigate_import_jobs WHERE job_id=? AND target_id=?', (old_id, target_id))
            c.commit()

    def interrupt_jobs(self):
        with self.repository.connect() as c:
            rows = c.execute('SELECT job_id,target_id,payload FROM frigate_import_jobs').fetchall()
        for job_id, target_id, payload in rows:
            job = self.decode(payload, job_id)
            if job['status'] == 'running':
                job['status'] = 'error'
                selected = set(job.get('selected', [r['key'] for r in job['cameras']]))
                for row in job['cameras']:
                    if row['key'] in selected and row.get('status') not in {'applied', 'already_managed'}:
                        row.update(status='error', reason='interrupted_import')
                self.save_job(target_id, job)

    def job(self, target_id, job_id):
        with self.repository.connect() as c:
            row = c.execute('SELECT payload FROM frigate_import_jobs WHERE job_id=? AND target_id=?', (job_id, target_id)).fetchone()
        if row is None:
            raise FrigateApiError('import_not_found')
        return self.decode(row[0], job_id)

    def links(self, target_id):
        with self.repository.connect() as c:
            rows = c.execute('SELECT camera_key,payload FROM frigate_import_links WHERE target_id=?', (target_id,)).fetchall()
            pending = c.execute('SELECT camera_key,payload FROM frigate_import_handoffs WHERE target_id=?', (target_id,)).fetchall()
        return {r[0]: self.decode(r[1], target_id + ':' + r[0]) for r in [*pending, *rows]}

    def save_link(self, target_id, key, value):
        with self.repository.connect() as c:
            if not value.get('camera_uuid'):
                c.execute('INSERT INTO frigate_import_handoffs VALUES (?,?,?) ON CONFLICT(target_id,camera_key) DO UPDATE SET payload=excluded.payload',
                          (target_id, key, self.encode(value, target_id + ':' + key)))
                c.commit()
                return
            c.execute('INSERT INTO frigate_import_links VALUES (?,?,?,?) ON CONFLICT(target_id,camera_key) DO UPDATE SET payload=excluded.payload',
                      (target_id, key, value['camera_uuid'], self.encode(value, target_id + ':' + key)))
            c.execute('DELETE FROM frigate_import_handoffs WHERE target_id=? AND camera_key=?', (target_id, key))
            c.commit()

    def delete_link(self, target_id, key):
        with self.repository.connect() as c:
            c.execute('DELETE FROM frigate_import_links WHERE target_id=? AND camera_key=?', (target_id, key))
            c.execute('DELETE FROM frigate_import_handoffs WHERE target_id=? AND camera_key=?', (target_id, key))
            c.commit()


def resolve_source(value, streams, trail=()):
    if not isinstance(value, str) or len(trail) > MAX_SOURCES:
        raise FrigateApiError('unsupported_source')
    # Preserve FFmpeg conversion arguments on the Frigate side, never import
    # its command as a source that would loop back into Frigate.
    wrapper = ''
    if value.startswith('ffmpeg:'):
        value = value[7:]
        value, separator, options = value.partition('#')
        wrapper = '#' + options if separator else ''
    if '{' in value or '}' in value:
        raise FrigateApiError('unresolved_secret')
    try:
        u = urllib.parse.urlsplit(value)
        if u.hostname in {'localhost', '127.0.0.1', '::1'} and (u.port or 554) == 8554:
            alias = u.path.lstrip('/')
            if alias in trail:
                raise FrigateApiError('relay_cycle')
            sources = streams.get(alias)
            sources = sources if isinstance(sources, list) else [sources]
            if len(sources) != 1:
                raise FrigateApiError('multiple_source_fallbacks')
            resolved = resolve_source(sources[0], streams, (*trail, alias))
            if wrapper and resolved['wrapper']:
                raise FrigateApiError('unsupported_source')
            return {**resolved, 'wrapper': wrapper or resolved['wrapper']}
        if u.scheme not in {'rtsp', 'rtsps'} or not u.hostname or u.fragment:
            raise FrigateApiError('unsupported_source')
        if re.search(r'(?:password|passwd|pwd|token|secret|pass)=', u.path, re.I) or any(
            re.search(r'^(?:password|passwd|pwd|token|secret|pass|auth)$', k, re.I)
            for k, v in urllib.parse.parse_qsl(u.query)
        ):
            raise FrigateApiError('unsupported_secret_query')
        host = '[' + u.hostname + ']' if ':' in u.hostname else u.hostname
        authority = host + (':' + str(u.port) if u.port else '')
        uri = urllib.parse.urlunsplit(u._replace(netloc=authority))
        return {'uri': uri, 'username': urllib.parse.unquote(u.username or ''),
                'password': urllib.parse.unquote(u.password or ''), 'wrapper': wrapper}
    except ValueError as exc:
        raise FrigateApiError('invalid_source') from exc


def inspect_camera(key, saved, effective, streams):
    inputs = effective.get('ffmpeg', {}).get('inputs', [])
    sources = {}
    roles = {}
    for inp in inputs:
        source = resolve_source(inp['path'], streams)
        if source['wrapper']:
            raise FrigateApiError('transcoded_capture_source')
        source_id = digest(_rtsp_source_key(source['uri']))
        sources[source_id] = source
        for role in inp.get('roles', []):
            if role in {'record', 'detect'}:
                roles[role] = source_id
    if not roles:
        raise FrigateApiError('missing_stream_roles')
    # Some configurations only assign detect; the recording role can use it
    # without enabling recording, which remains entirely operator-owned.
    roles.setdefault('record', roles.get('detect'))
    roles.setdefault('detect', roles.get('record'))
    live = {}
    live_aliases = set(effective.get('live', {}).get('streams', {}).values())
    if not live_aliases and key in streams:
        live_aliases.add(key)
    for alias in sorted(live_aliases):
        if alias not in streams:
            continue
        srcs = streams[alias]
        srcs = srcs if isinstance(srcs, list) else [srcs]
        if len(srcs) != 1:
            raise FrigateApiError('multiple_source_fallbacks')
        source = resolve_source(srcs[0], streams)
        source_id = digest(_rtsp_source_key(source['uri']))
        sources.setdefault(source_id, {**source, 'wrapper': ''})
        live[alias] = {'source_id': source_id, 'wrapper': source['wrapper']}
    if len(sources) > MAX_SOURCES:
        raise FrigateApiError('too_many_sources')
    if len({(s['username'], s['password']) for s in sources.values()}) != 1:
        raise FrigateApiError('different_stream_credentials')
    if len({urllib.parse.urlsplit(s['uri']).hostname for s in sources.values()}) != 1:
        raise FrigateApiError('different_stream_hosts')
    return {'sources': sources, 'roles': roles, 'live': live}


def public_job(job):
    # Never return a saved configuration, URI, password, or exception text.
    return {'id': job['id'], 'status': job['status'], 'restart_required': True,
            'selected': job.get('selected', []), 'keep_existing': job.get('keep_existing', []),
            'cameras': [{k: r.get(k) for k in ('key', 'name', 'category', 'reason', 'camera_uuid',
                                              'status', 'enabled', 'can_import', 'can_keep_existing', 'can_restore',
                                              'phase', 'stream_index', 'stream_total', 'configuration_status', 'identity_status')}
                        for r in job['cameras']]}


def start_job(job, selected, keep_existing):
    job.update(status='running', selected=list(selected), keep_existing=list(keep_existing))
    for row in job['cameras']:
        if row['key'] in selected and row.get('status') != 'applied' and row['category'] != 'managed':
            eligible = row['enabled'] and (row['can_import'] or (row['key'] in keep_existing and row.get('can_keep_existing')))
            row.update(status='queued' if eligible else 'pending', phase=None, stream_index=None, stream_total=None)
            if eligible:
                row['reason'] = None
                row['configuration_status'] = None
                row['identity_status'] = None


def match_signature(repository, camera_uuid):
    camera = _camera(repository, camera_uuid)
    sources = repository.managed_stream_sources(include_disabled=True, camera_uuid=camera_uuid)
    return digest({'enabled': camera['enabled'], 'roles': {s['stream_uuid']: s['roles'] for s in camera['streams']}, 'sources': sources})


def _import_source_config(key, effective, paths, saved):
    # The normal configuration response masks embedded video credentials.
    inputs = paths.get('cameras', {}).get(key, {}).get('ffmpeg', {}).get('inputs')
    if inputs is None or inputs == []:
        # A camera saved with restart pending is absent from runtime paths.
        # Its admin-only raw YAML is trusted too; unresolved variables are
        # still rejected by source resolution, never read from masked metadata.
        inputs = saved.get('ffmpeg', {}).get('inputs')
    if not isinstance(inputs, list) or not inputs:
        raise FrigateApiError('unsupported_source')
    return {**effective, 'ffmpeg': {**effective.get('ffmpeg', {}), 'inputs': inputs}}


def _refresh_masked_import_sources(row, client):
    # Failed jobs survive upgrades and the browser reconnects to their old ID.
    # Re-read only known masked credentials after the original config guard.
    if row.get('camera_uuid') or not any(
        source.get('username') and source.get('password') and
        set(source['username']) <= {'*'} and set(source['password']) <= {'*'}
        for source in row.get('sources', {}).values()
    ):
        return
    paths = client.raw_paths()
    actual = client.config().get('cameras', {}).get(row['key'], {})
    resolved = inspect_camera(row['key'], row['original'],
                              _import_source_config(row['key'], actual, paths, row['original']),
                              paths.get('go2rtc', {}).get('streams', {}))
    if (resolved['roles'] != row['roles'] or resolved['live'] != row['live'] or
        set(resolved['sources']) != set(row['sources']) or any(
            any(source.get(field) != row['sources'][sid].get(field) for field in ('uri', 'wrapper'))
            for sid, source in resolved['sources'].items())):
        raise FrigateApiError('camera_changed_refresh_preview')
    row['sources'] = resolved['sources']


def preview(repository, target, client=None):
    client = client or FrigateClient(target)
    raw = client.raw_config()
    effective = client.config()
    paths = client.raw_paths()
    streams = paths.get('go2rtc', {}).get('streams', {})
    links = ImportStore(repository).links(target.target_id)
    sources = repository.managed_stream_sources(include_disabled=True)
    inventory = {c['camera_uuid']: c for c in repository.consumer_inventory()}
    bindings = {}
    with repository.connect() as c:
        for r in c.execute('SELECT camera_uuid,frigate_camera_key FROM frigate_bindings WHERE target_id=?', (target.target_id,)):
            bindings[r[1]] = r[0]
    cameras = raw.get('cameras', {})
    if len(cameras) > MAX_CAMERAS:
        raise FrigateApiError('too_many_cameras')
    job = {'id': str(uuid.uuid4()), 'status': 'preview', 'cameras': [], 'api_url': target.api_url}
    for key, saved in cameras.items():
        actual = effective.get('cameras', {}).get(key, saved)
        row = {'key': key, 'name': actual.get('friendly_name') or key, 'enabled': actual.get('enabled', True),
               'category': 'new', 'status': 'ready', 'can_import': True, 'reason': None,
               'original': saved, 'original_streams': {}, 'camera_uuid': None,
               'onvif': copy.deepcopy(actual.get('onvif', {}))}
        job['cameras'].append(row)
        if key in links or key in bindings:
            row.update(category='managed', can_import=False, can_restore=key in links, camera_uuid=links[key]['camera_uuid'] if key in links else bindings[key])
            if key in links and links[key].get('state') != 'active':
                row.update(category='conflict', reason='incomplete_import_use_retry', can_import=False)
            continue
        try:
            row.update(inspect_camera(key, saved, _import_source_config(key, actual, paths, saved), streams))
            row['original_streams'] = {alias: raw.get('go2rtc', {}).get('streams', {}).get(alias)
                                       for alias in row['live']}
            # A live alias shared with another camera cannot be reassigned
            # without also affecting an unselected camera.
            if any(alias in other.get('live', {}).get('streams', {}).values()
                   for name, other in effective.get('cameras', {}).items() if name != key
                   for alias in row['live']):
                raise FrigateApiError('shared_live_alias')
            matches = {}
            for sid, src in row['sources'].items():
                matches[sid] = [s for s in sources if _rtsp_source_key(s['uri']) == _rtsp_source_key(src['uri'])]
            candidates = {s['camera_uuid'] for m in matches.values() for s in m}
            if candidates:
                if len(candidates) != 1 or any(len(m) != 1 for m in matches.values()):
                    raise FrigateApiError('ambiguous_source_match')
                cid = candidates.pop()
                if cid in bindings.values() or any(l['camera_uuid'] == cid for l in links.values()):
                    raise FrigateApiError('camera_already_linked')
                row.update(category='existing', camera_uuid=cid, match_signature=match_signature(repository, cid),
                           stream_map={sid: m[0]['stream_uuid'] for sid, m in matches.items()})
                camera = inventory[cid]
                expected_roles = {role: row['stream_map'][sid] for role, sid in row['roles'].items()}
                current_roles = {role: s['stream_uuid'] for s in camera['streams'] for role in s['roles']}
                credential_conflict = any((s['username'], s['password']) != (row['sources'][sid]['username'], row['sources'][sid]['password'])
                                          for sid, m in matches.items() for s in m)
                if credential_conflict or expected_roles != current_roles or not camera['enabled']:
                    row.update(category='conflict', reason='existing_settings_differ', can_import=False,
                               can_keep_existing=bool(camera['enabled']))
            if not row['enabled']:
                row.update(status='pending', reason='disabled_camera', can_import=False)
        except (FrigateApiError, ValueError, KeyError) as exc:
            row.update(category='conflict', can_import=False,
                       reason=exc.code if isinstance(exc, FrigateApiError) else 'unsupported_source')
    ImportStore(repository).save_job(target.target_id, job)
    return job


def _camera(repository, cid):
    return next(c for c in repository.consumer_inventory() if c['camera_uuid'] == cid)


def _role_streams(camera):
    return {role: s for s in camera['streams'] for role in s['roles']}


def relay_url(repository, host, stream):
    authority = '[' + host + ']' if ':' in host else host
    password = urllib.parse.quote(repository.rtsp_access_password(), safe='')
    return f"rtsp://camadmiral:{password}@{authority}:18554/{stream['stream_key']}"


def _profile(src, result, sid):
    return {'token': 'import-' + sid[:24], 'name': 'Imported stream', 'uri': src['uri'],
            'width': result.width or 0, 'height': result.height or 0, 'fps': result.fps or 0,
            'encoding': result.video_codec, 'bitrate_kbps': 0, 'source_kind': 'manual_rtsp'}


def _target_host(repository, target, inventory):
    saved = repository.frigate_target(target.target_id)
    return media_host_for_mode(inventory, (saved or {}).get('address_mode') or 'lan')


def build_managed(repository, row, host):
    camera = _camera(repository, row['camera_uuid'])
    role_streams = _role_streams(camera)
    if not camera['enabled'] or set(role_streams) != {'record', 'detect'}:
        raise FrigateApiError('stream_roles_missing')
    streams_by_id = {s['stream_uuid']: s for s in camera['streams']}
    aliases = row['aliases']
    streams = {}
    for role in ('record', 'detect'):
        streams[aliases[role]] = [relay_url(repository, host, role_streams[role])]
    for alias, spec in row['live'].items():
        sid = spec['source_id']
        role = next((r for r, source_id in row['roles'].items() if source_id == sid), None)
        stream = role_streams[role] if role else streams_by_id.get(row['stream_map'][sid])
        if stream is None:
            raise FrigateApiError('live_stream_disabled')
        source = relay_url(repository, host, stream)
        streams[alias] = [('ffmpeg:' + source + spec['wrapper']) if spec['wrapper'] else source]
    originals = {**row.get('released_streams', {}), **row['original_streams']}
    for alias in row.get('released_streams', {}):
        if alias in streams:
            continue
        source = resolve_source('rtsp://127.0.0.1:8554/' + alias, originals)
        sid = digest(_rtsp_source_key(source['uri']))
        role = next((r for r, source_id in row['roles'].items() if source_id == sid), None)
        stream = role_streams[role] if role else streams_by_id.get(row['stream_map'].get(sid))
        if stream is None:
            raise FrigateApiError('stream_roles_missing')
        url = relay_url(repository, host, stream)
        streams[alias] = [('ffmpeg:' + url + source['wrapper']) if source['wrapper'] else url]
    managed = copy.deepcopy(row['original'])
    inputs = []
    # Keep all original non-source tuning. Split a combined Record/Detect
    # input so later role changes do not require changing capture topology.
    for inp in managed.get('ffmpeg', {}).get('inputs', []):
        assigned = inp.get('roles', [])
        for role in ('record', 'detect'):
            if role in assigned:
                changed = copy.deepcopy(inp)
                changed.update(path=f'rtsp://127.0.0.1:8554/{aliases[role]}', roles=[role])
                if 'input_args' not in changed:
                    changed['input_args'] = 'preset-rtsp-restream'
                if 'audio' in assigned and role == next(r for r in ('record', 'detect') if r in assigned):
                    changed['roles'].append('audio')
                inputs.append(changed)
        if not any(r in assigned for r in ('record', 'detect')):
            raise FrigateApiError('unsupported_input_roles')
    managed.setdefault('ffmpeg', {})['inputs'] = inputs
    return managed, streams


def _media_aliases(camera, streams):
    """All aliases an entry reads, including nested capture/live aliases."""
    found = set()

    def visit(value, trail=()):
        if not isinstance(value, str) or len(trail) > MAX_SOURCES:
            raise FrigateApiError('unsafe_handoff_source')
        if value.startswith('ffmpeg:'):
            value = value[7:].split('#', 1)[0]
        if value in streams:
            if value in trail:
                raise FrigateApiError('relay_cycle')
            found.add(value)
            sources = streams[value]
            for source in sources if isinstance(sources, list) else [sources]:
                visit(source, (*trail, value))
            return
        u = urllib.parse.urlsplit(value)
        if u.scheme in {'rtsp', 'rtsps'} and u.hostname in {'localhost', '127.0.0.1', '::1'} and (u.port or 554) == 8554:
            alias = u.path.lstrip('/')
            if alias in trail:
                raise FrigateApiError('relay_cycle')
            if alias in streams:
                found.add(alias)
                sources = streams[alias]
                for source in sources if isinstance(sources, list) else [sources]:
                    visit(source, (*trail, alias))

    for item in camera.get('ffmpeg', {}).get('inputs', []):
        visit(item.get('path', ''))
    for alias in camera.get('live', {}).get('streams', {}).values():
        if alias in streams:
            visit('rtsp://127.0.0.1:8554/' + alias)
    return found


def _check_original(client, row):
    raw = client.raw_config()
    paused = row.get('handoff') and row.get('state') in {'paused', 'applying'}
    expected = row['paused_camera'] if paused else row['original']
    if raw.get('cameras', {}).get(row['key']) != expected:
        raise FrigateApiError('configuration_changed_refresh_preview')
    if any(alias in other.get('live', {}).get('streams', {}).values() or name == alias
           for name, other in raw.get('cameras', {}).items() if name != row['key']
           for alias in row['live']):
        raise FrigateApiError('configuration_changed_refresh_preview')
    for alias, value in row['original_streams'].items():
        if paused and alias in row.get('released_streams', {}):
            value = row['paused_streams'][alias]
        if raw.get('go2rtc', {}).get('streams', {}).get(alias) != value:
            raise FrigateApiError('configuration_changed_refresh_preview')
    if paused and any(raw.get('go2rtc', {}).get('streams', {}).get(alias) != value for alias, value in row['paused_streams'].items()):
        raise FrigateApiError('configuration_changed_refresh_preview')


def _wait_capture(client, keys, expected_inputs=None):
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        try:
            stats = client.stats()
            if all(float(stats.get(k, {}).get('camera_fps') or 0) > 0 for k in keys):
                if expected_inputs is not None:
                    if not _running_inputs_match(client, keys[0], expected_inputs):
                        time.sleep(1)
                        continue
                return
        except FrigateApiError:
            pass
        time.sleep(1)
    raise FrigateApiError('capture_verification_failed')


# Restart readiness and media readiness are separate budgets. A busy recorder
# can take longer than the capture deadline just to stop its old workers.
RESTART_READY_TIMEOUT_SECONDS = 180
RESTORE_RETRY_TIMEOUT_SECONDS = 180


def _temporary_api_error(exc):
    return exc.code in {'target_unavailable', 'invalid_response'} or (
        exc.code == 'request_rejected' and (exc.upstream_status in {502, 503, 504}
        or exc.upstream_status == 500 and exc.stage in {'restore_read', 'restart_read'})
    )


def _running_inputs_match(client, key, expected_inputs):
    paths = client.raw_paths().get('cameras', {}).get(key, {}).get('ffmpeg', {}).get('inputs', [])
    def simplify(items):
        result = []
        for item in items:
            roles = set(item.get('roles') or [])
            # Frigate assigns Record and Detect to its sole input, even when
            # saved YAML declares only one role. Preserve Audio and unknown
            # roles so real changes still fail the ownership check.
            if len(items) == 1:
                roles.update(('record', 'detect'))
            result.append({'path': item['path'], 'roles': sorted(roles)})
        return result
    return simplify(paths) == simplify(expected_inputs)


def _request_restart(client):
    try:
        client.restart()
    except FrigateApiError as exc:
        # A dropped acknowledgement can still mean Frigate accepted the restart.
        if not _temporary_api_error(exc):
            raise


def _restart(client):
    try:
        before = client.service_started_at()
    except FrigateApiError as exc:
        raise exc.with_context(stage='restart_read') from exc
    _request_restart(client)
    deadline = time.monotonic() + RESTART_READY_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        try:
            # last_updated - integer uptime can drift by less than one second.
            # The old process may still serve healthy-looking capture statistics.
            if client.service_started_at() > before + 1:
                client.config()
                return
        except FrigateApiError as exc:
            # Startup can briefly return 500 while IPC workers initialize.
            if not _temporary_api_error(exc) and not (exc.code == 'request_rejected' and exc.upstream_status == 500):
                raise
        time.sleep(1)
    raise FrigateApiError('target_unavailable')


def _restart_and_wait_capture(client, key, expected_inputs):
    _restart(client)
    _wait_capture(client, [key], expected_inputs)


def _wait_record_reader(client, row):
    """Detection FPS alone cannot prove a separate Record input is receiving."""
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        try:
            if not client.config()['cameras'][row['key']].get('record', {}).get('enabled', True):
                return
            stream = client.runtime_streams().get(row['aliases']['record'], {})
            if stream.get('consumers') and any(int(p.get('bytes_recv') or 0) > 0 for p in stream.get('producers', [])):
                return
        except FrigateApiError:
            pass
        time.sleep(1)
    raise FrigateApiError('recording_verification_failed')


def _release_reader(repository, target, row, client, progress):
    # Do not disturb an offline entry or mistake authentication failure for
    # session contention. This path is called only for unavailable/timeouts.
    if not float(client.stats().get(row['key'], {}).get('camera_fps') or 0) > 0:
        raise FrigateApiError('source_unavailable')
    _check_original(client, row)
    raw = client.raw_config()
    streams = raw.get('go2rtc', {}).get('streams', {})
    aliases = _media_aliases(row['original'], streams) | set(row['original_streams'])
    for name, other in raw.get('cameras', {}).items():
        if name != row['key'] and aliases & _media_aliases(other, streams):
            raise FrigateApiError('shared_handoff_source')
    for alias, sources in streams.items():
        if alias not in aliases and aliases & _media_aliases(
            {'ffmpeg': {'inputs': [{'path': s} for s in (sources if isinstance(sources, list) else [sources])]}}, streams
        ):
            raise FrigateApiError('shared_handoff_source')
    paused = copy.deepcopy(row['original'])
    paused['enabled'] = False
    row.update(handoff=True, release_required=True, state='releasing', configuration_status='restore_required', paused_camera=paused,
               released_streams={alias: streams[alias] for alias in aliases},
               paused_streams={alias: ['rtsp://127.0.0.1:1/camadmiral-handoff'] for alias in aliases},
               target_api_url=target.api_url, aliases={})
    store = ImportStore(repository)
    store.save_link(target.target_id, row['key'], row)
    progress('releasing_original')
    _check_original(client, row)
    client.set_config({'cameras': {row['key']: paused}, 'go2rtc': {'streams': row['paused_streams']}}, requires_restart=True)
    _restart(client)
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        try:
            actual = client.config().get('cameras', {}).get(row['key'], {})
            if actual.get('enabled') is False and not float(client.stats().get(row['key'], {}).get('camera_fps') or 0):
                row['state'] = 'paused'
                store.save_link(target.target_id, row['key'], row)
                _check_original(client, row)
                return
        except FrigateApiError:
            pass
        time.sleep(1)
    raise FrigateApiError('handoff_release_failed')


def _camera_write(row, desired, current):
    # config/set merges mappings. Remove the temporary flag when the original
    # relied on Frigate's default instead of leaving enabled:false behind.
    # Frigate rejects deleting an absent key, including a repeated restore.
    if row.get('handoff') and 'enabled' not in desired and 'enabled' in current:
        return {**desired, 'enabled': ''}
    return desired


def _probe_import(row, uri, username='', password=''):
    # Allow a bounded grace period for a camera to free its just-closed RTSP
    # session. This never turns invalid media or rejected credentials into ready.
    attempts = 3 if row.get('handoff') else 1
    for attempt in range(attempts):
        result = probe_source(uri, username, password)
        if result.status not in {'unavailable', 'timeout'} or attempt == attempts - 1:
            return result
        time.sleep(1)


def restore(repository, target, key, client=None, reconcile_media=None):
    client = client or FrigateClient(target)
    deadline = time.monotonic() + RESTORE_RETRY_TIMEOUT_SECONDS
    while True:
        try:
            return _restore_once(repository, target, key, client, reconcile_media)
        except FrigateApiError as exc:
            if not _temporary_api_error(exc) or time.monotonic() >= deadline:
                raise
            # Re-read the journal and recheck ownership after every outage or
            # uncertain save. Never replay a stale snapshot over operator edits.
            time.sleep(1)


def _finish_restore(repository, target, key, row, client, store):
    if row.get('camera_uuid'):
        repository.deselect_frigate_camera(target.target_id, row['camera_uuid'])
        repository.remove_frigate_binding(target.target_id, row['camera_uuid'])
    for alias in row.get('aliases', {}).values():
        try:
            client.delete_runtime_stream(alias)
        except FrigateApiError:
            pass
    store.delete_link(target.target_id, key)


def _restore_guard(row, current):
    key = row['key']
    actual = current.get('cameras', {}).get(key)
    # Do not clobber edits made after takeover. An operator must resolve them.
    if actual not in (row['original'], row.get('managed_camera'), row.get('paused_camera')):
        raise FrigateApiError('rollback_configuration_conflict')
    owned_streams = row.get('managed_streams', {})
    configured = current.get('go2rtc', {}).get('streams', {})
    originals = {**row.get('released_streams', {}), **row['original_streams']}
    for alias, value in owned_streams.items():
        if row.get('handoff') and actual == row.get('paused_camera') and configured.get(alias) == row.get('paused_streams', {}).get(alias):
            continue
        if configured.get(alias) not in (value, originals.get(alias)):
            raise FrigateApiError('rollback_configuration_conflict')
    for alias, value in row.get('released_streams', {}).items():
        if configured.get(alias) not in (row.get('paused_streams', {}).get(alias), value, owned_streams.get(alias)):
            raise FrigateApiError('rollback_configuration_conflict')
    return actual, configured, originals


def _restore_once(repository, target, key, client, reconcile_media):
    store = ImportStore(repository)
    row = store.links(target.target_id).get(key)
    if row is None:
        raise FrigateApiError('import_not_found')
    if row.get('target_api_url', target.api_url) != target.api_url:
        raise FrigateApiError('rollback_target_changed')
    try:
        current = client.raw_config()
    except FrigateApiError as exc:
        raise exc.with_context(stage='restore_read') from exc
    actual, configured, originals = _restore_guard(row, current)
    # A newly created relay must relinquish its camera slot when returning the
    # original reader. Retain its data; never disable a reused/operator camera.
    if row.get('handoff') and row.get('created_camera'):
        if not row.get('camera_uuid') and row.get('candidate_id'):
            prior = repository.adoption_for_candidate(row['candidate_id'])
            if prior:
                row['camera_uuid'] = prior['camera_uuid']
        if row.get('camera_uuid'):
            with repository.connect() as c:
                other = c.execute('SELECT 1 FROM frigate_camera_selections WHERE camera_uuid=? AND target_id<>? LIMIT 1',
                                  (row['camera_uuid'], target.target_id)).fetchone()
            if other:
                raise FrigateApiError('restore_media_in_use')
            repository.set_camera_enabled(row['camera_uuid'], False)
            if reconcile_media:
                reconcile_media()
    original_saved = (actual == row['original']
                      and all(configured.get(alias) == value for alias, value in originals.items())
                      and not any(alias in configured for alias in row.get('aliases', {}).values()))
    running_original = False
    if original_saved:
        try:
            running_original = (client.config().get('cameras', {}).get(key, {}).get('enabled', True)
                                and float(client.stats().get(key, {}).get('camera_fps') or 0) > 0
                                and _running_inputs_match(client, key, row['original']['ffmpeg']['inputs']))
        except FrigateApiError as exc:
            raise exc.with_context(stage='restore_read') from exc
    if running_original:
        # A previous process can lose the reply after restoring healthy video.
        # Clear that journal without writing configuration or restarting again.
        _finish_restore(repository, target, key, row, client, store)
        return
    client.set_config({'cameras': {key: _camera_write(row, row['original'], actual)}, 'go2rtc': {'streams': {
        **{alias: '' for alias in row.get('aliases', {}).values() if alias in configured},
        **row.get('released_streams', {}), **row['original_streams']}}}, requires_restart=True)
    if row.get('camera_uuid'):
        repository.deselect_frigate_camera(target.target_id, row['camera_uuid'])
        repository.remove_frigate_binding(target.target_id, row['camera_uuid'])
    row['state'] = 'restored'
    store.save_link(target.target_id, key, row)
    _restart_and_wait_capture(client, key, row['original']['ffmpeg']['inputs'])
    _finish_restore(repository, target, key, row, client, store)


def execute(repository, target, job_id, selected, keep_existing, inventory, reconcile_media,
            client=None):
    if len(selected) > 1:
        from .frigate_batch import execute_batch
        return execute_batch(repository, target, job_id, selected, keep_existing, inventory, reconcile_media, client)
    client = client or FrigateClient(target)
    store = ImportStore(repository)
    job = store.job(target.target_id, job_id)
    if job['api_url'] != target.api_url:
        raise FrigateApiError('target_changed_refresh_preview')
    keys = {r['key'] for r in job['cameras']}
    if not selected or len(selected) != len(set(selected)) or not set(selected) <= keys:
        raise FrigateApiError('invalid_selection')
    start_job(job, selected, keep_existing)
    store.save_job(target.target_id, job)
    for row in job['cameras']:
        if row['key'] not in selected or row.get('status') == 'applied':
            continue
        def progress(phase, stream_index=None, stream_total=None):
            row.update(status='running', phase=phase, stream_index=stream_index, stream_total=stream_total)
            store.save_job(target.target_id, job)

        try:
            if row['category'] == 'managed':
                row['status'] = 'already_managed'
                continue
            if not row['enabled']:
                row['status'] = 'pending'
                continue
            accepted = row['key'] in keep_existing and row.get('can_keep_existing')
            if not row['can_import'] and not accepted:
                row['status'] = 'pending'
                continue
            row['reason'] = None
            progress('checking_configuration')
            # A journal survives a process crash between Frigate save, restart,
            # and acknowledgement. Restore that transaction before retrying.
            previous = store.links(target.target_id).get(row['key'])
            if previous:
                if previous.get('state') == 'active':
                    row.update(status='applied', camera_uuid=previous['camera_uuid'])
                    continue
                progress('restoring_original')
                restore(repository, target, row['key'], client, reconcile_media)
                row['configuration_status'] = 'restored'
            row['configuration_status'] = row.get('configuration_status') or 'unchanged'
            for field in ('handoff', 'paused_camera', 'released_streams', 'paused_streams'):
                row.pop(field, None)
            row['state'] = 'preparing'
            _check_original(client, row)
            _refresh_masked_import_sources(row, client)
            if row.get('camera_uuid') and any(link['camera_uuid'] == row['camera_uuid'] for key, link in store.links(target.target_id).items() if key != row['key']):
                raise FrigateApiError('camera_already_linked')
            candidate_id = 'frigate-import:' + target.target_id + ':' + row['key']
            row.update(candidate_id=candidate_id, import_job_id=job_id)
            if row.get('camera_uuid') is None:
                prior = repository.adoption_for_candidate(candidate_id)
                if prior is not None:
                    expected = {role: 'import-' + sid[:24] for role, sid in row['roles'].items()}
                    credentials = repository.credentials_for_candidate(candidate_id)
                    source = next(iter(row['sources'].values()))
                    if prior['role_tokens'] != expected or credentials != (source['username'], source['password']):
                        raise FrigateApiError('camera_changed_refresh_preview')
                    row['camera_uuid'] = prior['camera_uuid']
                    row['created_camera'] = True
                    row['stream_map'] = {sid: next(s['stream_uuid'] for s in prior['streams']
                                                 if s['profile_token'] == 'import-' + sid[:24]) for sid in row['sources']}
            if row.get('release_required'):
                _release_reader(repository, target, row, client, progress)
            if row.get('camera_uuid') is None:
                row['created_camera'] = True
                profiles = []
                for index, (sid, src) in enumerate(row['sources'].items(), 1):
                    progress('validating_source', index, len(row['sources']))
                    result = _probe_import(row, src['uri'], src['username'], src['password'])
                    if result.status in {'unavailable', 'timeout'} and not row.get('handoff'):
                        _release_reader(repository, target, row, client, progress)
                        progress('validating_source', index, len(row['sources']))
                        result = _probe_import(row, src['uri'], src['username'], src['password'])
                    if result.status != 'ready':
                        raise FrigateApiError('source_' + result.status)
                    profiles.append(_profile(src, result, sid))
                source = next(iter(row['sources'].values()))
                progress('adding_camera')
                adoption = repository.adopt({'candidate_uuid': candidate_id,
                                             'display_name': row['name'], 'ip': urllib.parse.urlsplit(source['uri']).hostname},
                                            source['username'], source['password'], profiles,
                                            {role: 'import-' + sid[:24] for role, sid in row['roles'].items()},
                                            camera_origin='direct',
                                            reject_source_keys={_rtsp_source_key(s['uri']) for s in row['sources'].values()})
                row['camera_uuid'] = adoption['camera_uuid']
                row['stream_map'] = {sid: next(s['stream_uuid'] for s in adoption['streams']
                                             if s['profile_token'] == 'import-' + sid[:24]) for sid in row['sources']}
                if row.get('handoff'):
                    store.save_link(target.target_id, row['key'], row)
                store.save_job(target.target_id, job)
            if row.get('match_signature') and row['match_signature'] != match_signature(repository, row['camera_uuid']):
                raise FrigateApiError('camera_changed_refresh_preview')
            progress('preparing_relay')
            if row.get('created_camera'):
                repository.set_camera_enabled(row['camera_uuid'], True)
            host = _target_host(repository, target, inventory)
            camera = _camera(repository, row['camera_uuid'])
            relevant_streams = [s for s in camera['streams'] if s['stream_uuid'] in row['stream_map'].values()]
            for attempt in range(2):
                reconcile_media()
                try:
                    for index, stream in enumerate(relevant_streams, 1):
                        progress('validating_relay', index, len(relevant_streams))
                        result = _probe_import(row, relay_url(repository, host, stream), 'camadmiral', repository.rtsp_access_password())
                        if result.status != 'ready':
                            raise FrigateApiError('relay_' + result.status)
                        try:
                            snapshot_frame(stream['stream_key'], rtsp_password=repository.rtsp_access_password(), width=320)
                        except SnapshotError:
                            raise FrigateApiError('relay_video_unavailable') from None
                    break
                except FrigateApiError as exc:
                    if attempt or row.get('handoff') or exc.code not in {'relay_unavailable', 'relay_timeout'}:
                        raise
                    _release_reader(repository, target, row, client, progress)
            row['aliases'] = {role: 'camadmiral_' + row['camera_uuid'].replace('-', '_') + '_import_' + role
                              for role in ('record', 'detect')}
            managed, streams = build_managed(repository, row, host)
            row.update(state='applying', configuration_status='restore_required', managed_camera=managed, managed_streams=streams)
            store.save_link(target.target_id, row['key'], row)
            # Recheck immediately before writing; Frigate has no CAS endpoint.
            _check_original(client, row)
            progress('saving_frigate')
            expected_camera = row['paused_camera'] if row.get('handoff') else row['original']
            client.set_config({'cameras': {row['key']: _camera_write(row, managed, expected_camera)}, 'go2rtc': {'streams': streams}}, requires_restart=True)
            progress('waiting_for_frigate')
            _restart_and_wait_capture(client, row['key'], managed['ffmpeg']['inputs'])
            if row.get('handoff'):
                _wait_record_reader(client, row)
            saved = client.raw_config()
            if saved.get('cameras', {}).get(row['key']) != managed:
                raise FrigateApiError('saved_configuration_mismatch')
            progress('learning_identity')
            try:
                from .camera_identity import learn_import_identity
                learn_import_identity(repository, row['camera_uuid'], inventory)
            except Exception:
                # Video import remains useful for cameras with no discoverable
                # stable identity. Their management UI offers explicit linking.
                pass
            row['identity_status'] = 'verified' if row['camera_uuid'] in repository.camera_identity_links() else 'unavailable'
            repository.select_frigate_camera(target.target_id, row['camera_uuid'])
            role_streams = _role_streams(camera)
            h = digest({'camera': managed, 'streams': streams})
            repository.record_frigate_attempt(target.target_id, row['camera_uuid'], row['key'],
                                              role_streams['record']['stream_uuid'], role_streams['detect']['stream_uuid'], h)
            repository.complete_frigate_attempt(target.target_id, row['camera_uuid'], status='applied', applied_hash=h)
            source_revision = _camera_source_revisions(client, row, camera,
                source_connection_revisions(repository.managed_stream_runtime_sources()))
            if source_revision is not None:
                row['relay_source_revisions'] = source_revision
            row.update(state='active', status='applied', reason=None, configuration_status=None, phase=None, stream_index=None, stream_total=None, relay_runtime_revision=_relay_runtime_revision(repository))
            store.save_link(target.target_id, row['key'], row)
        except Exception as exc:
            row.update(status='error', reason=exc.code if isinstance(exc, FrigateApiError) else 'import_failed')
            link = store.links(target.target_id).get(row['key'])
            if link and link.get('state') in {'releasing', 'paused', 'applying', 'restored'}:
                try:
                    progress('restoring_original')
                    restore(repository, target, row['key'], client, reconcile_media)
                    row.update(state='restored', configuration_status='restored')
                except Exception:
                    row['reason'] = 'rollback_required'
                    row['configuration_status'] = 'restore_required'
            row['status'] = 'error'
        finally:
            row.update(phase=None, stream_index=None, stream_total=None)
            store.save_job(target.target_id, job)
    job['status'] = 'completed'
    store.save_job(target.target_id, job)
    return job


def recover_handoffs(repository, reconcile_media, client_factory=FrigateClient):
    """Resume restoration after a crash while the old reader was released."""
    from .frigate import FrigateTarget
    store = ImportStore(repository)
    for saved in repository.frigate_targets():
        target = FrigateTarget(saved['target_id'], saved['name'], saved['api_url'])
        from .frigate_batch import recover_batches
        pending_batches = recover_batches(repository, target, reconcile_media, client_factory(target))
        for key, row in store.links(target.target_id).items():
            if row.get('batch_job_id') in pending_batches:
                continue
            if not row.get('handoff') or row.get('state') == 'active':
                continue
            reason = 'interrupted_import'
            try:
                restore(repository, target, key, client_factory(target), reconcile_media)
            except Exception:
                reason = 'rollback_required'
            if row.get('import_job_id'):
                try:
                    job = store.job(target.target_id, row['import_job_id'])
                except FrigateApiError:
                    continue
                for camera in job['cameras']:
                    if camera['key'] == key:
                        camera.update(status='error', reason=reason, phase=None,
                                      configuration_status='restored' if reason == 'interrupted_import' else 'restore_required')
                job['status'] = 'error'
                store.save_job(target.target_id, job)


def _missing_runtime_yaml(exc):
    return (exc.code == 'request_rejected' and exc.upstream_status == 400
            and exc.upstream_detail in {'yaml: path not exist',
                'Failed to delete stream: yaml: path not exist'})


def _delete_runtime_alias(client, alias, source):
    # go2rtc deletes the live entry before trying to patch its generated YAML.
    # A missing YAML path is harmless only when the live deletion is observed.
    for attempt in range(2):
        try:
            client.delete_runtime_stream(alias)
            return
        except FrigateApiError as exc:
            if not _missing_runtime_yaml(exc):
                raise
            if alias not in client.runtime_streams():
                return
            if attempt:
                raise
            client.set_runtime_stream(alias, source)


def _refresh_imported_readers(client, row):
    # A stable downstream URL can now supply different codec parameters.
    # Recreate only owned aliases so Frigate discards its cached upstream SDP.
    configured = client.raw_config().get("go2rtc", {}).get("streams", {})
    sources = row["managed_streams"]
    if any(configured.get(alias) != values or not isinstance(values, list)
           or len(values) != 1 or not isinstance(values[0], str)
           for alias, values in sources.items()):
        raise FrigateApiError("import_source_conflict")
    runtime = client.runtime_streams()
    error = None
    try:
        for alias in sorted(sources):
            if alias not in runtime:
                continue
            _delete_runtime_alias(client, alias, sources[alias][0])
    except FrigateApiError as exc:
        error = exc
    finally:
        # Restore every owned alias, including after an ambiguous operation.
        for alias in sorted(sources):
            try:
                client.set_runtime_stream(alias, sources[alias][0])
            except FrigateApiError as exc:
                error = error or exc
    if error is not None:
        raise error


def _known_offline_required_roles(camera):
    roles = _role_streams(camera)
    return any(roles.get(role, {}).get("health_status") == "offline" for role in ("record", "detect"))


def _saved_control(raw, camera, path, default):
    value = default
    for source in (raw, camera):
        for key in path:
            if not isinstance(source, dict) or key not in source:
                break
            source = source[key]
        else:
            value = source
    return value


def _guard_restart_runtime_controls(raw, live, links):
    # A full restart restores disk settings. Do not discard unrelated runtime
    # pauses or UI controls while recovering our own temporary capture pause.
    controls = {("enabled",): True, ("detect", "enabled"): False,
                ("motion", "enabled"): True, ("record", "enabled"): False,
                ("snapshots", "enabled"): False, ("audio", "enabled"): False,
                ("birdseye", "enabled"): True, ("notifications", "enabled"): False,
                ("review", "alerts", "enabled"): True,
                ("review", "detections", "enabled"): True,
                ("onvif", "autotracking", "enabled"): False,
                ("objects", "genai", "enabled"): False,
                ("review", "genai", "enabled"): False,
                ("audio_transcription", "live_enabled"): False}
    for key, camera in live.get("cameras", {}).items():
        for path, default in controls.items():
            value = _saved_control({}, camera, path, None)
            if value is None:
                continue
            if path == ("enabled",) and links.get(key, {}).get("capture_resume_pending"):
                continue
            expected = _saved_control(raw, raw.get("cameras", {}).get(key, {}), path, default)
            if value != expected:
                print("frigate: automatic restart blocked by runtime control override at " + ".".join(path), flush=True)
                raise FrigateApiError("import_source_conflict")


def _restart_imported_target(repository, target, client, links, trigger):
    store = ImportStore(repository)
    pending = trigger.get("target_restart")
    raw = client.raw_config()
    if pending is None:
        _guard_restart_runtime_controls(raw, client.config(), links)
        configured = raw.get("go2rtc", {}).get("streams", {})
        if raw.get("cameras", {}).get(trigger["key"], {}).get("ffmpeg", {}).get("inputs") != trigger["managed_camera"].get("ffmpeg", {}).get("inputs"):
            raise FrigateApiError("import_source_conflict")
        if any(configured.get(alias) != values for alias, values in trigger["managed_streams"].items()):
            raise FrigateApiError("import_source_conflict")
        offline_keys = {key for key, row in links.items()
                        if _known_offline_required_roles(_camera(repository, row["camera_uuid"]))}
        pending = {"started_before": client.service_started_at(), "raw_hash": digest(raw),
                   "active_keys": sorted({trigger["key"]} | {key for key, stats in client.stats().items()
                                       if key not in offline_keys and float(stats.get("camera_fps") or 0) > 0}),
                   "runtime_revision": _relay_runtime_revision(repository),
                   "address_revisions": {r["camera_uuid"]: repository.latest_camera_address_revision(r["camera_uuid"])
                                         for r in links.values() if r.get("state") == "active"}}
        # Save before dispatch: an uncertain reply must never cause a restart loop.
        trigger["target_restart"] = pending
        store.save_link(target.target_id, trigger["key"], trigger)
        _request_restart(client)
    if digest(raw) != pending["raw_hash"]:
        raise FrigateApiError("import_source_conflict")
    deadline = time.monotonic() + READER_RESTART_VERIFY_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        try:
            if client.service_started_at() > pending["started_before"] + 2:
                break
        except FrigateApiError:
            pass
        time.sleep(0.5)
    else:
        raise FrigateApiError("capture_verification_failed")
    _wait_capture(client, pending["active_keys"])
    if digest(client.raw_config()) != pending["raw_hash"]:
        raise FrigateApiError("import_source_conflict")
    selected = set(repository.selected_frigate_camera_uuids(target.target_id))
    configured = raw.get("go2rtc", {}).get("streams", {})
    verified = []
    for key, row in links.items():
        if row.get("state") != "active" or row["camera_uuid"] not in selected:
            continue
        camera = _camera(repository, row["camera_uuid"])
        if not camera["enabled"] or _known_offline_required_roles(camera):
            continue
        if any(configured.get(alias) != values for alias, values in row["managed_streams"].items()):
            continue
        if raw.get("cameras", {}).get(key, {}).get("enabled", True):
            _wait_record_reader(client, row)
        verified.append((key, row))
    # Verify the whole recoverable set before committing any marker. Keep the
    # triggering journal until its peers have been committed successfully.
    source_revisions = source_connection_revisions(repository.managed_stream_runtime_sources())
    for key, row in sorted(verified, key=lambda item: item[0] == trigger["key"]):
        source_revision = _camera_source_revisions(client, row, _camera(repository, row['camera_uuid']), source_revisions)
        if source_revision is not None:
            row['relay_source_revisions'] = source_revision
        row["relay_runtime_revision"] = pending["runtime_revision"]
        row["relay_address_revision"] = pending["address_revisions"].get(row["camera_uuid"])
        row.pop("capture_resume_pending", None)
        row.pop("target_restart", None)
        row.pop('source_recovery_pending', None)
        store.save_link(target.target_id, key, row)
    # The triggering row may have been deselected while the reply was uncertain.
    trigger.pop("target_restart", None)
    trigger.pop("capture_resume_pending", None)
    trigger.pop('source_recovery_pending', None)
    store.save_link(target.target_id, trigger["key"], trigger)


def _refresh_capture(repository, target, client, links, row):
    store = ImportStore(repository)
    row["capture_resume_pending"] = True
    store.save_link(target.target_id, row["key"], row)
    try:
        client.set_runtime_camera_enabled(row["key"], False)
    except FrigateApiError:
        try:
            _restart_imported_target(repository, target, client, links, row)
        except FrigateApiError:
            if not row.get("target_restart"):
                # A safety guard rejected global restart before dispatch.
                client.set_runtime_camera_enabled(row["key"], True)
                row.pop("capture_resume_pending", None)
                store.save_link(target.target_id, row["key"], row)
            raise
        return True
    error = None
    try:
        _refresh_imported_readers(client, row)
    except FrigateApiError as exc:
        error = exc
    try:
        client.set_runtime_camera_enabled(row["key"], True)
    except FrigateApiError:
        _restart_imported_target(repository, target, client, links, row)
        return True
    row.pop("capture_resume_pending", None)
    store.save_link(target.target_id, row["key"], row)
    if error is not None:
        if _missing_runtime_yaml(error):
            _restart_imported_target(repository, target, client, links, row)
            return True
        raise error
    return False


def _camera_source_revisions(client, row, camera, revisions):
    if not revisions:
        return None
    record_enabled = client.config().get('cameras', {}).get(row['key'], {}).get('record', {}).get('enabled', True)
    roles = {'record', 'detect'} if record_enabled else {'detect'}
    required = {stream['stream_uuid'] for stream in camera['streams'] if roles & set(stream['roles'])}
    if not required or not required <= revisions.keys():
        return None
    return {key: revisions[key] for key in required}


def _reader_media_ready(client, row):
    camera = client.config().get('cameras', {}).get(row['key'], {})
    if not camera.get('enabled', True) or not float(client.stats().get(row['key'], {}).get('camera_fps') or 0) > 0:
        return False
    if not _running_inputs_match(client, row['key'], row['managed_camera']['ffmpeg']['inputs']):
        return False
    if camera.get('record', {}).get('enabled', True):
        stream = client.runtime_streams().get(row['aliases']['record'], {})
        return bool(stream.get('consumers') and any(int(p.get('bytes_recv') or 0) > 0 for p in stream.get('producers', [])))
    return True


def _ack_source_recovery(repository, client, row, camera):
    # Our own pause may close the last upstream consumer and create another
    # connection. Acknowledge only after readers have resumed on that connection.
    revision = _camera_source_revisions(client, row, camera,
        source_connection_revisions(repository.managed_stream_runtime_sources()))
    if revision is not None:
        row['relay_source_revisions'] = revision
    row.pop('source_recovery_pending', None)


def _relay_runtime_revision(repository):
    # A control-process restart and a shared relay child restart each invalidate
    # every imported consumer, including cameras whose address stayed unchanged.
    return RELAY_RUNTIME_REVISION + ":" + (repository.latest_relay_runtime_revision() or "initial")


def sync_imported(repository, target, inventory=None, client=None, host_resolver=None):
    """Reconcile only managed source fields; never regenerate operator settings."""
    client = client or FrigateClient(target)
    runtime_revision = _relay_runtime_revision(repository)
    source_revisions = source_connection_revisions(repository.managed_stream_runtime_sources())
    store = ImportStore(repository)
    selected = set(repository.selected_frigate_camera_uuids(target.target_id))
    links = store.links(target.target_id)
    for key, row in links.items():
        if row.get('state') != 'active' or row['camera_uuid'] not in selected:
            continue
        try:
            camera = _camera(repository, row['camera_uuid'])
            if not camera['enabled']:
                continue
            saved_target = repository.frigate_target(target.target_id) or {}
            mode = saved_target.get('address_mode') or 'lan'
            host = host_resolver(mode) if host_resolver else _target_host(repository, target, inventory)
            _, desired_streams = build_managed(repository, row, host)
            raw = client.raw_config()
            if row.get("target_restart"):
                _restart_imported_target(repository, target, client, links, row)
                break
            if row.get("capture_resume_pending"):
                # Recover a crash or ambiguous reply after our own runtime pause.
                # Resume before any new refresh, even if source ownership changed.
                if raw.get("cameras", {}).get(key, {}).get("enabled", True):
                    try:
                        client.set_runtime_camera_enabled(key, True)
                    except FrigateApiError:
                        _restart_imported_target(repository, target, client, links, row)
                        break
                row.pop("capture_resume_pending", None)
                store.save_link(target.target_id, key, row)
                continue
            # Replacing FFmpeg inputs outside CamAdmiral needs operator review.
            if raw.get('cameras', {}).get(key, {}).get('ffmpeg', {}).get('inputs') != row['managed_camera'].get('ffmpeg', {}).get('inputs'):
                repository.set_frigate_selection_error(target.target_id, row['camera_uuid'], 'import_source_conflict')
                continue
            configured = raw.get('go2rtc', {}).get('streams', {})
            if any(configured.get(alias) not in (old, desired_streams.get(alias)) for alias, old in row['managed_streams'].items()):
                repository.set_frigate_selection_error(target.target_id, row['camera_uuid'], 'import_source_conflict')
                continue
            if desired_streams != row['managed_streams']:
                client.set_config({'go2rtc': {'streams': desired_streams}})
                for alias, values in desired_streams.items():
                    client.set_runtime_stream(alias, values[0])
                row['managed_streams'] = desired_streams
                store.save_link(target.target_id, key, row)
            revision = repository.latest_camera_address_revision(row["camera_uuid"])
            source_revision = _camera_source_revisions(client, row, camera, source_revisions)
            previous_sources = row.get('relay_source_revisions')
            source_changed = (source_revision is not None and previous_sources is not None
                              and any(previous_sources.get(stream_uuid) != value for stream_uuid, value in source_revision.items()))
            configuration_changed = ((revision is not None and revision != row.get("relay_address_revision"))
                                     or row.get("relay_runtime_revision") != runtime_revision)
            pending = row.get('source_recovery_pending')
            if pending and pending.get('epoch') != RELAY_RUNTIME_REVISION:
                row.pop('source_recovery_pending', None)
                pending = None
            if pending and not configuration_changed:
                if _reader_media_ready(client, row):
                    _ack_source_recovery(repository, client, row, camera)
                    store.save_link(target.target_id, key, row)
                    repository.set_frigate_selection_error(target.target_id, row['camera_uuid'], None)
                elif (time.monotonic() - pending['started'] >= 90
                      and not _known_offline_required_roles(camera)):
                    _restart_imported_target(repository, target, client, links, row)
                    break
                # Let healthy peers recover while this camera initializes.
                continue
            if configuration_changed or source_changed:
                if _known_offline_required_roles(camera):
                    # Known source outages are not evidence of stuck capture.
                    # Leave the revision pending until ordinary health recovers.
                    continue
                if not client.config().get("cameras", {}).get(key, {}).get("enabled", True):
                    continue
                # Runtime pause stops both record and detect FFmpeg consumers.
                # Replacing go2rtc aliases alone leaves active consumers orphaned.
                if _refresh_capture(repository, target, client, links, row):
                    break
                row['source_recovery_pending'] = {'epoch': RELAY_RUNTIME_REVISION, 'started': time.monotonic()}
                if _reader_media_ready(client, row):
                    _ack_source_recovery(repository, client, row, camera)
                source_revision = None
                row["relay_address_revision"] = revision
                row["relay_runtime_revision"] = runtime_revision
                store.save_link(target.target_id, key, row)
            if source_revision is not None and source_revision != row.get('relay_source_revisions'):
                row['relay_source_revisions'] = source_revision
                store.save_link(target.target_id, key, row)
            repository.set_frigate_selection_error(target.target_id, row['camera_uuid'], None)
        except FrigateApiError as exc:
            print("frigate: imported reader recovery deferred (" + exc.code + ")", flush=True)
            repository.set_frigate_selection_error(target.target_id, row["camera_uuid"], exc.code)
            if row.get("target_restart"):
                break
    return {row['camera_uuid'] for row in links.values()}

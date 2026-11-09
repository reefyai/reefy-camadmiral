"""Actions regression: literal-IP imports follow verified physical identities.

Two logical channels share one synthetic ONVIF device. Docker address changes
model DHCP after a power event; the application must discover and validate them.
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import time
import urllib.request
import urllib.error

ROOT = Path(__file__).resolve().parents[1]
STATE = Path('/state/frigate-identity.json')
DATABASE = '/var/lib/camadmiral/camadmiral.db'
KEYS = ('identity_camera', 'channel_camera')
ENDPOINT = 'urn:uuid:synthetic-onvif-camera'
FRIGATE_CONTROL_FAULT_SCRIPT = r'''from pathlib import Path
import subprocess
import sys
p=Path('/usr/local/nginx/conf/nginx.conf')
s=p.read_text()
needle='location /ws {'
assert s.count(needle)==1
fault=needle+'\n            return 503;'
if len(sys.argv)>1 and sys.argv[1]=='restore':
    assert s.count(fault)==1
    p.write_text(s.replace(fault, needle, 1))
else:
    assert fault not in s
    p.write_text(s.replace(needle, fault, 1))
subprocess.run(['/usr/local/nginx/sbin/nginx','-t'],check=True)
subprocess.run(['/usr/local/nginx/sbin/nginx','-s','reload'],check=True)'''


def recording_clip_interval(start, end):
    if not math.isfinite(start) or not math.isfinite(end) or end <= start:
        raise ValueError('A recording must have a finite positive duration')
    # Frigate's inclusive clip query also selects adjacent segments at exact
    # boundaries. Keep this request inside the selected recording, especially
    # across a source resolution change, while retaining real playback checks.
    margin = min(0.05, (end - start) / 4)
    return start + margin, end - margin


def recording_playback_interval(row, rows):
    # The clip API includes every overlapping recording, not only the selected
    # ID. Find an exclusive interior so older codec headers cannot contaminate
    # validation of a new source. Never accept a different camera or an old row.
    windows = [(row[3], row[4])]
    for other in rows:
        if other[0] == row[0] or other[1] != row[1]:
            continue
        remaining = []
        for start, end in windows:
            if other[4] <= start or other[3] >= end:
                remaining.append((start, end))
            else:
                if start < other[3]:
                    remaining.append((start, other[3]))
                if other[4] < end:
                    remaining.append((other[4], end))
        windows = remaining
    windows = [(start, end) for start, end in windows
               if math.isfinite(start) and math.isfinite(end) and end - start >= 2]
    if not windows:
        raise ValueError("Recording has no complete exclusive playback interval")
    return recording_clip_interval(*max(windows, key=lambda window: window[1] - window[0]))


def recording_playback_url(row, rows):
    start, end = recording_playback_interval(row, rows)
    return f'http://frigate:5000/api/{row[1]}/start/{start}/end/{end}/clip.mp4'


def recording_for_playback(rows, key, since=-math.inf):
    for row in reversed(rows):
        if row[1] != key or row[3] <= since:
            continue
        try:
            recording_playback_interval(row, rows)
        except ValueError:
            continue
        return row
    return None


def decoded_frame_hash(output):
    frames = [line for line in output.splitlines() if re.fullmatch(
        r'\d+,\s*-?\d+,\s*-?\d+,\s*\d+,\s*[1-9]\d*,\s*[0-9a-fA-F]{64}', line.strip())]
    assert len(frames) == 1, 'Playback must decode exactly one nonempty video frame'
    return frames[0].rsplit(',', 1)[1].strip()


def reader_recovery_ready(readers, camera_ids, address_revisions, previous_runtime=None):
    generations = set()
    for key, camera_uuid in camera_ids.items():
        row = readers.get(key, {})
        generation = row.get("relay_runtime_revision")
        if (row.get("camera_uuid") != camera_uuid or not generation or not address_revisions.get(camera_uuid)
                or row.get("capture_resume_pending") or row.get("target_restart")
                or row.get("relay_address_revision") != address_revisions.get(camera_uuid)):
            return False
        if previous_runtime is not None and generation == previous_runtime.get(key):
            return False
        generations.add(generation)
    return bool(camera_ids) and len(generations) == 1


def driver(stage):
    import yaml
    from scenarios import (consumer_directory, directory_signature, request_json,
                           wait_for, wait_for_health)

    def frigate(path):
        with urllib.request.urlopen('http://frigate:5000/api' + path, timeout=15) as response:
            return json.load(response)

    def sql(query, parameters=()):
        with sqlite3.connect('file:' + DATABASE + '?mode=ro', uri=True) as connection:
            connection.row_factory = sqlite3.Row
            return [dict(row) for row in connection.execute(query, parameters)]

    def links():
        # The baseline must fail a behavioral assertion, not a missing-table error.
        if not sql("SELECT name FROM sqlite_master WHERE name='camera_identity_links'"):
            return {}
        return {row['camera_uuid']: json.loads(row['payload']) for row in sql('SELECT * FROM camera_identity_links')}

    def reader_links(target_id):
        # Use the packaged image modules, including in unchanged-baseline runs;
        # an absolute /e2e script does not put the image working directory on sys.path.
        if '/opt/camadmiral' not in sys.path:
            sys.path.insert(0, '/opt/camadmiral')
        from camadmiral.crypto import decrypt_password, load_master_key
        master = load_master_key()
        result = {}
        for row in sql('SELECT camera_key,payload FROM frigate_import_links WHERE target_id=?', (target_id,)):
            saved = json.loads(decrypt_password(row['payload'], target_id + ':' + row['camera_key'], master))
            result[row['camera_key']] = {field: saved.get(field) for field in (
                'camera_uuid', 'relay_address_revision', 'relay_runtime_revision')}
            result[row['camera_key']].update(capture_resume_pending=bool(saved.get('capture_resume_pending')),
                                            target_restart=bool(saved.get('target_restart')))
        return result

    if stage in {'remove-links', 'remove-primary-anchor'}:
        state = json.loads(STATE.read_text())
        with sqlite3.connect(DATABASE) as connection:
            if stage == 'remove-links':
                connection.execute('DELETE FROM camera_identity_links')
            else:
                camera_uuid = state['camera_ids']['identity_camera']
                connection.execute('DELETE FROM camera_identity_links WHERE camera_uuid=?', (camera_uuid,))
                connection.execute('DELETE FROM camera_identity_periods WHERE camera_uuid=?', (camera_uuid,))
        return

    wait_for_health()
    wait_for('Frigate API ready', lambda: frigate('/stats'), timeout=180)
    if stage == 'prepare':
        target = request_json('/internal/frigate-targets', method='POST', expected=201,
            payload={'name': 'Synthetic identity target', 'api_url': 'http://frigate:5000'},
            headers={'X-CamAdmiral-Action': 'add-frigate-target'})
        target = request_json('/internal/frigate-targets')['targets'][0]
        STATE.write_text(json.dumps({'target': target['target_id'], 'original': yaml.safe_load(frigate('/config/raw'))}))
        return
    state = json.loads(STATE.read_text())
    base = '/internal/frigate-targets/' + state['target']
    if stage == 'imported':
        job = request_json(base + '/import/' + state['job'])
        assert job['status'] == 'completed' and all(row['status'] == 'applied' for row in job['cameras']), job
        state['camera_ids'] = {row['key']: row['camera_uuid'] for row in job['cameras']}
        assert set(state['camera_ids']) == set(KEYS)
        assert len(set(state['camera_ids'].values())) == 2, 'Physical channels were merged'
        state['signature'] = directory_signature(consumer_directory())
        assert sum(len(c['streams']) for c in state['signature'].values()) == 4
        state['credentials'] = sql('SELECT credential_uuid,username,hex(password_ciphertext) AS ciphertext FROM camera_credentials ORDER BY credential_uuid')
        readers = reader_links(state['target'])
        # Older unchanged runtime has no reader journal; retain its original
        # black-box media assertions rather than failing on a missing field.
        state['reader_recovery_tracking'] = all(readers.get(key, {}).get('relay_runtime_revision') for key in KEYS)
        state['reader_runtime_revisions'] = {key: readers.get(key, {}).get('relay_runtime_revision') for key in KEYS}
        state['managed_config'] = yaml.safe_load(frigate('/config/raw'))
        original = state['original']
        current = state['managed_config']
        assert set(current['cameras']) == set(original['cameras'])
        assert current['camera_groups'] == original['camera_groups']
        for key in KEYS:
            for field, value in original['cameras'][key].items():
                if field not in {'ffmpeg', 'live'}:
                    assert current['cameras'][key][field] == value, (key, field)
        initial = links()
        state['initial_identity_links'] = {key: {
            'candidate_uuid': value.get('candidate_uuid'), 'mac': value.get('mac'),
            'endpoint_reference': (value.get('onvif') or {}).get('endpoint_reference')}
            for key, value in initial.items()}
        state['initial_identity_learned'] = (len(initial) == 2 and
            all(link.get('onvif', {}).get('endpoint_reference') == ENDPOINT for link in initial.values()) and
            len({link['candidate_uuid'] for link in initial.values()}) == 1)
        STATE.write_text(json.dumps(state))
        return
    if stage == 'fallback-control-unavailable':
        def unavailable():
            try:
                urllib.request.urlopen('http://frigate:5000/ws', timeout=5).close()
            except urllib.error.HTTPError as exc:
                return exc.code == 503
            return False
        wait_for('per-camera control fault active', unavailable, timeout=15)
        service = frigate('/stats')['service']
        state['forced_fallback_started_before'] = float(service['last_updated']) - float(service['uptime'])
        state['forced_fallback_pending'] = True
        STATE.write_text(json.dumps(state))
        return
    if stage == 'fallback-control-restored':
        from websockets.sync.client import connect
        def available():
            try:
                with connect('ws://frigate:5000/ws', proxy=None, open_timeout=5, close_timeout=2):
                    return True
            except Exception:
                return False
        wait_for('per-camera runtime control restored', available, timeout=15)
        state['fallback_control_restored'] = True
        STATE.write_text(json.dumps(state))
        return
    if stage == 'unanchored':
        primary = state['camera_ids']['identity_camera']
        secondary = state['camera_ids']['channel_camera']
        wait_for('anchored channel follows the moved device', lambda: all(row['source_host'] == '172.28.0.18'
            for row in sql('SELECT source_host FROM onvif_profiles WHERE camera_uuid=?', (secondary,))), timeout=240)
        assert primary not in links(), 'Legacy source was associated by stale IP without operator review'
        assert {r['source_host'] for r in sql('SELECT source_host FROM onvif_profiles WHERE camera_uuid=?', (primary,))} == {'172.28.0.17'}
        review = request_json('/internal/cameras/' + primary + '/identity', method='POST',
            payload={'address': '172.28.0.18'}, headers={'X-CamAdmiral-Action': 'link-camera-identity'})
        assert review['status'] == 'review' and review['identity']['endpoint_reference'] == ENDPOINT
        assert primary not in links(), 'Identify must not bind before confirmation'
        changed = request_json('/internal/cameras/' + primary + '/identity', method='POST', expected=409,
            payload={'address': '172.28.0.18', 'confirm': True, 'endpoint_reference': 'urn:uuid:synthetic-wrong-device'},
            headers={'X-CamAdmiral-Action': 'link-camera-identity'})
        assert changed['status'] == 'identity_changed' and primary not in links()
        return
    if stage.startswith('verify-'):
        restarted = stage.startswith('verify-restarted-')
        address = stage.removeprefix('verify-restarted-' if restarted else 'verify-')
        def recovered():
            sources = sql('SELECT source_host FROM onvif_profiles')
            current = links()
            periods = sql('SELECT camera_uuid,ip_address,onvif_identity FROM camera_identity_periods WHERE ended_at IS NULL')
            health = sql('SELECT health_status,probe_status FROM managed_streams WHERE enabled=1')
            if state.get('reader_recovery_tracking'):
                revisions = {row['camera_uuid']: row['event_uuid'] for row in sql(
                    'SELECT camera_uuid,event_uuid FROM camera_address_events ORDER BY changed_at,rowid')}
                previous = state['reader_runtime_revisions'] if restarted else None
                if not reader_recovery_ready(reader_links(state['target']), state['camera_ids'], revisions, previous):
                    return False
            # Source hosts are written before relay validation, and may still
            # roll back. Wait for the validated identity commit for both owners.
            return (len(sources) == 4 and all(row['source_host'] == address for row in sources)
                    and len(current) == 2 and all(link.get('ip') == address for link in current.values())
                    and len(health) == 4 and all(row['health_status'] == 'healthy' and row['probe_status'] == 'ready' for row in health)
                    and {row['camera_uuid'] for row in periods if row['ip_address'] == address and row['onvif_identity'] == ENDPOINT}
                        == set(state['camera_ids'].values()))
        wait_for('both logical channels recover through physical identity', recovered, timeout=240)
        current_links = links()
        assert state['initial_identity_learned'], 'Literal-IP import did not learn stable physical identity'
        assert len(current_links) == 2 and all(link['onvif']['endpoint_reference'] == ENDPOINT for link in current_links.values())
        assert len({link['candidate_uuid'] for link in current_links.values()}) == 1
        assert directory_signature(consumer_directory()) == state['signature'], 'Camera/stream IDs, names, roles or downstream URLs changed'
        assert sql('SELECT credential_uuid,username,hex(password_ciphertext) AS ciphertext FROM camera_credentials ORDER BY credential_uuid') == state['credentials']
        assert yaml.safe_load(frigate('/config/raw')) == state['managed_config'], 'Address recovery rewrote Frigate configuration'
        for camera_uuid in state['camera_ids'].values():
            periods = sql('SELECT * FROM camera_identity_periods WHERE camera_uuid=? ORDER BY started_at DESC', (camera_uuid,))
            assert periods and periods[0]['ip_address'] == address and periods[0]['onvif_identity'] == ENDPOINT
        if state.get('reader_recovery_tracking'):
            if restarted and state.get('forced_fallback_pending'):
                service = frigate('/stats')['service']
                started = float(service['last_updated']) - float(service['uptime'])
                assert started > state['forced_fallback_started_before'] + 2, 'Unavailable per-camera control must trigger a real verified Frigate restart'
                state['forced_fallback_pending'] = False
                state['fallback_verified'] = True
            readers = reader_links(state['target'])
            state['reader_runtime_revisions'] = {key: readers[key]['relay_runtime_revision'] for key in KEYS}
            STATE.write_text(json.dumps(state))
        return
    if stage == 'foreign-rejected':
        # A different camera at the original IP must never take over a known identity.
        camera_uuid = state['camera_ids']['channel_camera']
        conflict = request_json('/internal/cameras/' + camera_uuid + '/identity', method='POST', expected=409,
            payload={'address': '172.28.0.13', 'confirm': True, 'endpoint_reference': 'urn:uuid:synthetic-unrelated-device'},
            headers={'X-CamAdmiral-Action': 'link-camera-identity'})
        assert conflict['status'] == 'identity_conflict'
        return
    if stage == 'evidence':
        print(json.dumps({'camera_ids': state['camera_ids'], 'signature': state['signature'],
            'initial_identity_learned': state['initial_identity_learned'],
            'fallback_verified': state.get('fallback_verified', False),
            'fallback_control_restored': state.get('fallback_control_restored', False),
            'health': sql('SELECT camera_uuid,stream_uuid,health_status,probe_status FROM managed_streams'),
            'address_events': sql('SELECT camera_uuid,event_uuid FROM camera_address_events ORDER BY changed_at,rowid'),
            'relay_generation': sql('SELECT revision FROM relay_runtime_state') if state.get('reader_recovery_tracking') else [],
            'sources': sql('SELECT camera_uuid,source_host,source_path,source_query FROM onvif_profiles'),
            'links': links(), 'reader_recovery': reader_links(state['target']), 'history': sql('SELECT camera_uuid,ip_address,mac_address,onvif_identity,ended_at FROM camera_identity_periods')}))
        return
    raise ValueError(stage)


def host():
    from command_output import run_logged
    from playwright.sync_api import sync_playwright, expect
    revision = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    os.environ['IDENTITY_TEST_IMAGE'] = 'camadmiral-frigate-identity:' + revision[:12]
    artifacts = ROOT / 'e2e-artifacts/frigate-identity'
    artifacts.mkdir(parents=True, exist_ok=True)
    compose = ['docker', 'compose', '-p', 'camadmiral-frigate-identity', '-f', str(ROOT / 'e2e/frigate-identity-compose.yaml')]
    result = {'status': 'failed', 'revision': revision, 'moves': [], 'playback_checks': []}

    def run(*args, check=True):
        return run_logged([*compose, *args], cwd=ROOT,
                          transcript=artifacts / 'transcript.log', check=check)

    def stage(name):
        print('Imported identity stage: ' + name, flush=True)
        return run('run', '--rm', '--no-deps', 'test-driver', name)

    def state():
        return json.loads(run('run', '--rm', '--no-deps', '--entrypoint', 'cat', 'test-driver', str(STATE)))

    def recordings():
        return json.loads(run('exec', '-T', 'frigate', 'python3', '-c', """import json,sqlite3
from pathlib import Path
with sqlite3.connect('file:/config/frigate.db?mode=ro',uri=True) as db:
 rows=db.execute('SELECT id,camera,path,start_time,end_time FROM recordings ORDER BY start_time').fetchall()
print(json.dumps([list(r) for r in rows if Path(r[2]).is_file() and Path(r[2]).stat().st_size>0]))"""))

    def fresh(since):
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            rows = recordings()
            if all(recording_for_playback(rows, key, since) is not None for key in KEYS):
                return rows
            time.sleep(3)
        raise AssertionError('Both imported channels must save complete new recording segments')

    def history(seed=False):
        # Reuse the production-table fixture contract from the established import lab.
        code = """import json,sqlite3,time
with sqlite3.connect('/config/frigate.db') as db:
 if SEED:
  stamp=time.time()-20
  event={'id':'synthetic-identity-event','camera':'identity_camera','label':'person','start_time':stamp,'end_time':stamp+10,'top_score':0.9,'score':0.9,'false_positive':0,'zones':'[]','thumbnail':'','has_clip':1,'has_snapshot':0,'region':'[]','box':'[]','area':100,'retain_indefinitely':1,'ratio':1,'plus_id':'','model_hash':'synthetic','detector_type':'cpu','model_type':'synthetic','data':'{}'}
  columns={r[1] for r in db.execute('pragma table_info(event)')}
  event={k:v for k,v in event.items() if k in columns}
  db.execute('INSERT INTO event ('+','.join(event)+') VALUES ('+','.join('?' for _ in event)+')',list(event.values()))
  db.execute('INSERT INTO reviewsegment(id,camera,start_time,end_time,severity,thumb_path,data) VALUES (?,?,?,?,?,?,?)',('synthetic-identity-review','identity_camera',stamp,stamp+10,'alert','/media/frigate/clips/synthetic-review.jpg','{}'))
  db.commit()
 print(json.dumps({'events':db.execute("SELECT * FROM event WHERE id='synthetic-identity-event'").fetchall(),'review':db.execute("SELECT * FROM reviewsegment WHERE id='synthetic-identity-review'").fetchall()}))"""
        return json.loads(run('exec', '-T', 'frigate', 'python3', '-c', 'SEED=' + repr(seed) + '\n' + code))

    def verify(address, since, restarted=False):
        stage(('verify-restarted-' if restarted else 'verify-') + address)
        # Only recordings started after the validated identity commit can
        # establish recovered capture, not old segments flushed during a move.
        since = max(since, time.time())
        after = fresh(since)
        assert {row[0] for row in before} <= {row[0] for row in after}, 'Historical recordings were lost'
        assert history() == history_before, 'Event/review rows changed'
        # Decode original history and a new file for each logical channel.
        for key in KEYS:
            original = recording_for_playback(before, key)
            latest = recording_for_playback(after, key, since)
            assert original is not None and latest is not None, 'Complete old and new recording segments are required'
            decoded = []
            for row in (original, latest):
                playback = recording_playback_url(row, before if row is original else after)
                output = run('run', '--rm', '--no-deps', '--entrypoint', 'ffmpeg', 'test-driver',
                    '-v', 'error', '-i', playback, '-map', '0:v:0', '-frames:v', '1', '-threads', '1',
                    '-f', 'framehash', '-hash', 'sha256', '-')
                decoded.append({'recording_id': row[0], 'frame_sha256': decoded_frame_hash(output)})
            playback = recording_playback_url(latest, after)
            observed = json.loads(run('run', '--rm', '--no-deps', '--entrypoint', 'ffprobe', 'test-driver',
                '-v', 'error', '-i', playback, '-select_streams', 'v:0', '-show_entries', 'stream=width,height', '-of', 'json'))
            # Retain the selected synthetic file independently of clip API
            # concatenation, so a codec failure can be traced to its boundary.
            container = run("ps", "-q", "frigate").strip()
            retained = artifacts / (key + "-selected.mp4")
            subprocess.run(["docker", "cp", container + ":" + latest[2], str(retained)], check=True)
            moved = address in {'172.28.0.15', '172.28.0.18'}
            width, height = ((960, 540) if moved else (1280, 720)) if key == 'identity_camera' else ((480, 270) if moved else (640, 360))
            result['playback_checks'].append({'camera': key, 'address': address,
                'recording_id': latest[0], 'interval': recording_playback_interval(latest, after),
                'decoded_frames': decoded,
                'expected_dimensions': [width, height], 'observed_streams': observed['streams']})
            assert observed['streams'] == [{'width': width, 'height': height}], 'New recording came from the wrong physical source'
        result['moves'].append({'address': address, 'fresh_recording_ids': [r[0] for r in after if r[3] > since],
            'history_preserved': True, 'old_and_new_video_decoded': True})

    try:
        run('down', '--volumes', '--remove-orphans')
        run('build', 'camadmiral')
        # Share synthetic scratch state while keeping the database's production
        # owner and 0600 permissions. The driver reads it as the application UID.
        run('run', '--rm', '--no-deps', '--user', '0:0', '--entrypoint', 'chmod', 'test-driver', '0777', '/state')
        run('up', '-d', 'camadmiral', 'frigate', 'camera-onvif')
        result['images'] = subprocess.check_output(['docker', 'image', 'inspect', os.environ['IDENTITY_TEST_IMAGE'],
            'ghcr.io/blakeblackshear/frigate:0.17.2', '--format', '{{.Id}} {{json .RepoDigests}}'], text=True).splitlines()
        stage('prepare')
        before = fresh(time.time())
        history_before = history(seed=True)
        result['history_before'] = history_before
        result['recordings_before'] = before
        with sync_playwright() as playwright:
            browser = playwright.webkit.launch()
            page = browser.new_page(http_credentials={'username': 'admin', 'password': 'synthetic-e2e-admin-password'})
            try:
                page.goto('http://' + run('port', 'camadmiral', '18080') + '/settings/integrations')
                page.get_by_role('button', name='Import from Frigate', exact=True).click()
                page.get_by_label('Import Synthetic imported identity', exact=True).check()
                page.get_by_label('Import Synthetic independent channel', exact=True).check()
                page.get_by_label('Allow Frigate restart').check()
                with page.expect_response(lambda response: response.request.method == 'POST' and '/import/' in response.url) as imported:
                    page.get_by_role('button', name='Import and manage selected', exact=True).click()
                job = imported.value.json()['id']
                run('run', '--rm', '--no-deps', '--entrypoint', 'python', 'test-driver', '-c',
                    "import json;from pathlib import Path;p=Path('/state/frigate-identity.json');s=json.loads(p.read_text());s['job']=" + repr(job) + ";p.write_text(json.dumps(s))")
                expect(page.get_by_label('Allow Frigate restart')).to_be_enabled(timeout=300000)
                page.screenshot(path=str(artifacts / 'imported.png'))
                stage('imported')
                result['import_completed'] = True
                result['initial_identity_learned'] = state()['initial_identity_learned']
                since = time.time()
                run('stop', 'camera-onvif')
                run('rm', '-f', 'camera-onvif')
                run('up', '-d', 'camera-moved')
                verify('172.28.0.15', since)
                page.goto('http://' + run('port', 'camadmiral', '18080'))
                page.locator('#camera-rows tr.camera-row').filter(has_text='Synthetic imported identity').get_by_role('button', name='Details', exact=True).click()
                expect(page.get_by_text('Identity history', exact=True)).to_be_visible()
                expect(page.locator('#app-modal')).to_contain_text('172.28.0.15', timeout=30000)
                expect(page.locator('#app-modal')).to_contain_text('172.28.0.13')
                page.screenshot(path=str(artifacts / 'imported-identity-history.png'))
                run('up', '-d', 'old-address-device')
                stage('foreign-rejected')
                # Deterministically fail only the runtime-control transport.
                # HTTP APIs and live media remain available for real recovery.
                run('exec', '-T', 'frigate', 'python3', '-c', FRIGATE_CONTROL_FAULT_SCRIPT)
                stage('fallback-control-unavailable')
                run('restart', 'camadmiral')
                verify('172.28.0.15', time.time(), restarted=True)
                # Later recovery must also work through normal per-camera
                # control, including peers reset by another camera's move.
                run('exec', '-T', 'frigate', 'python3', '-c', FRIGATE_CONTROL_FAULT_SCRIPT, 'restore')
                stage('fallback-control-restored')
                run('stop', 'camadmiral')
                stage('remove-links')
                run('start', 'camadmiral')
                verify('172.28.0.15', time.time(), restarted=True)
                since = time.time()
                run('stop', 'camera-moved')
                run('rm', '-f', 'camera-moved')
                run('up', '-d', 'camera-returned')
                verify('172.28.0.17', since)
                run('stop', 'camadmiral')
                stage('remove-primary-anchor')
                run('stop', 'camera-returned')
                run('rm', '-f', 'camera-returned')
                run('up', '-d', 'camera-confirmed')
                run('start', 'camadmiral')
                stage('unanchored')
                page.goto('http://' + run('port', 'camadmiral', '18080'))
                row = page.locator('#camera-rows tr.camera-row').filter(has_text='Synthetic imported identity')
                row.get_by_role('button', name='Details', exact=True).click()
                page.get_by_role('button', name='Link device identity', exact=True).click()
                page.get_by_label("Camera's current IP address", exact=True).fill('172.28.0.18')
                page.get_by_role('button', name='Identify camera', exact=True).click()
                expect(page.get_by_text('ONVIF identity: ' + ENDPOINT, exact=True)).to_be_visible(timeout=60000)
                with page.expect_response(lambda response: response.request.method == 'POST' and response.url.endswith('/identity')) as confirmed:
                    page.get_by_role('button', name='Confirm device identity', exact=True).click()
                assert confirmed.value.json()['status'] == 'linked'
                expect(page.get_by_text('Device identity linked. Video recovery is being validated.', exact=True)).to_be_visible()
                page.screenshot(path=str(artifacts / 'legacy-identity-confirmed.png'))
                verify('172.28.0.18', time.time())
                result['identity'] = json.loads(stage('evidence'))
                result['status'] = 'passed'
            except Exception:
                page.screenshot(path=str(artifacts / 'failure.png'))
                raise
            finally:
                browser.close()
    finally:
        if result.get('import_completed'):
            try:
                result['identity'] = json.loads(stage('evidence'))
            except Exception:
                result['identity_evidence_unavailable'] = True
        (artifacts / 'result.json').write_text(json.dumps(result, indent=2))
        run('logs', '--no-color', check=False)
        if not os.environ.get('CAMADMIRAL_E2E_KEEP'):
            run('down', '--volumes', '--remove-orphans', check=False)


if __name__ == '__main__':
    driver(sys.argv[1]) if len(sys.argv) > 1 else host()

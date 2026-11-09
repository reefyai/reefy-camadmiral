"""Durable import/takeover regression against real Frigate and synthetic media.

Run by the GitHub Actions release gate, never against an operator's device.
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
STATE = Path('/state/frigate-import.json')


def driver(stage):
    from scenarios import request_json, request, wait_for, wait_for_health, consumer_directory, full_sync_when_idle

    def frigate(path):
        with urllib.request.urlopen('http://frigate:5000/api' + path, timeout=10) as r:
            return json.load(r)

    wait_for_health()
    wait_for('Frigate API', lambda: frigate('/stats'), timeout=180)
    if stage == 'prepare':
        request_json('/internal/frigate-targets', method='POST', expected=201,
                     payload={'name': 'Synthetic import target', 'api_url': 'http://127.0.0.1:20017'},
                     headers={'X-CamAdmiral-Action': 'add-frigate-target'})
        target = request_json('/internal/frigate-targets')['targets'][0]
        existing = request_json('/internal/cameras/rtsp', method='POST', expected=201, timeout=60,
                                headers={'X-CamAdmiral-Action': 'create-direct-rtsp-camera'},
                                payload={'display_name': 'Different CamAdmiral loading name',
                                         'sources': [{'label': 'Sub', 'url': 'rtsp://camera-open:8554/sub'}]})
        original = frigate('/config')
        saved = frigate('/config/raw')
        STATE.write_text(json.dumps({'target_id': target['target_id'], 'saved': saved,
                                     'camera': original['cameras']['operator_camera'],
                                     'all_cameras': original['cameras'], 'groups': original['camera_groups'],
                                     'existing_uuid': existing['adoption']['camera_uuid']}))
        return
    state = json.loads(STATE.read_text())
    base = '/internal/frigate-targets/' + state['target_id']
    if stage.startswith('external-'):
        import yaml

        def save(config):
            payload = json.dumps({'requires_restart': 1, 'config_data': config}).encode()
            urllib.request.urlopen(urllib.request.Request('http://frigate:5000/api/config/set', data=payload,
                headers={'Content-Type': 'application/json'}, method='PUT'), timeout=15).read()

        def metrics():
            return json.load(urllib.request.urlopen('http://import-external:8080/stream-stats', timeout=5))

        def reader():
            return json.loads(Path('/state/external-reader-status.json').read_text())

        def live_reader():
            value = reader()
            return value if value['status'] == 'running' and value['frames'] > 5 and time.time() - value['last_frame_at'] < 5 else False

        if stage == 'external-prepare':
            camera = copy.deepcopy(yaml.safe_load(state['saved'])['cameras']['operator_camera'])
            camera.update(enabled=True, friendly_name='Synthetic shared source',
                          ffmpeg={'inputs': [
                              {'path': 'rtsp://127.0.0.1:8554/external_record', 'input_args': 'preset-rtsp-restream', 'roles': ['record']},
                              {'path': 'rtsp://127.0.0.1:8554/external_detect', 'input_args': 'preset-rtsp-restream', 'roles': ['detect']} ]},
                          live={'streams': {'Main': 'external_record'}})
            state['external_camera'] = camera
            state['external_inventory'] = [c['id'] for c in consumer_directory()['cameras']]
            save({'cameras': {'external_camera': camera}, 'go2rtc': {'streams': {
                'external_record': 'rtsp://import-external:8554/main',
                'external_detect': 'rtsp://import-external:8554/sub'}}})
        elif stage == 'external-pause':
            save({'cameras': {'external_camera': {'enabled': False}}})
        elif stage == 'external-reader-ready':
            assert frigate('/config')['cameras']['external_camera']['enabled'] is False
            wait_for('independent recorder decodes the physical Record source', live_reader, timeout=60)
            assert metrics()['sessions'].get('main') == 1
            save({'cameras': {'external_camera': {'enabled': True}}})
        elif stage == 'external-blocked':
            wait_for('separate Detect remains live with Record occupied', lambda: frigate('/stats')['cameras']['external_camera']['camera_fps'] > 0, timeout=120)
            value = live_reader()
            assert value, 'External recorder must still decode real frames'
            stats = metrics()
            assert stats['sessions'].get('main') == stats['sessions'].get('sub') == 1, stats
            refused = subprocess.run(['ffprobe', '-v', 'error', '-rtsp_transport', 'tcp',
                '-show_entries', 'stream=codec_type', '-of', 'json', 'rtsp://import-external:8554/main'],
                capture_output=True, timeout=15)
            assert refused.returncode != 0, 'An independent reader must exhaust the Record slot'
            state.update(external_saved=frigate('/config/raw'), external_blocked_since=time.time(),
                         external_reader_before=value, external_denials_before=metrics()['session_denials'])
            assert yaml.safe_load(state['external_saved'])['cameras']['external_camera'] == state['external_camera']
        elif stage == 'external-failed':
            job = request_json(base + '/import/' + state['external_job'])
            row = next(r for r in job['cameras'] if r['key'] == 'external_camera')
            assert job['status'] == 'completed' and row['status'] == 'error' and row['reason'] == 'source_unavailable', row
            assert yaml.safe_load(frigate('/config/raw')) == yaml.safe_load(state['external_saved']), 'Failed external-reader handoff changed the original configuration'
            assert sorted(c['id'] for c in consumer_directory()['cameras']) == sorted(state['external_inventory']), 'Unvalidated media was adopted'
            assert frigate('/stats')['cameras']['external_camera']['camera_fps'] > 0, 'Restored Detect must remain available'
            current = live_reader()
            assert current and current['frames'] > state['external_reader_before']['frames']
            for field in ('wrapper_id', 'session_id', 'consumer_pid'):
                assert current[field] == state['external_reader_before'][field], 'Import interrupted the independent recorder'
            stats = metrics()
            assert stats['sessions'].get('main') == 1 and stats['session_denials'] > state['external_denials_before']
            state['external_evidence'] = {'failed_reason': row['reason'], 'configuration_status': row.get('configuration_status'),
                'detect_fps': frigate('/stats')['cameras']['external_camera']['camera_fps'],
                'external_frames_before': state['external_reader_before']['frames'], 'external_frames_after': current['frames'],
                'record_slot_occupied': True, 'original_configuration_preserved': True, 'no_adoption': True,
                'session_denials': stats['session_denials']}
        elif stage == 'external-success':
            job = request_json(base + '/import/' + state['external_job'])
            row = next(r for r in job['cameras'] if r['key'] == 'external_camera')
            assert row['status'] == 'applied', row
            camera = next(c for c in consumer_directory()['cameras'] if c['id'] == row['camera_uuid'])
            assert len(consumer_directory()['cameras']) == len(state['external_inventory']) + 1
            assert len(camera['streams']) == 2, 'Separate Record/Detect profiles were merged'
            current = yaml.safe_load(frigate('/config/raw'))
            original = yaml.safe_load(state['external_saved'])
            for field, value in state['external_camera'].items():
                if field not in {'ffmpeg', 'live'}:
                    assert current['cameras']['external_camera'][field] == value, field
            assert set(current['cameras']) == set(original['cameras'])
            for key in original['cameras']:
                if key != 'external_camera':
                    assert current['cameras'][key] == original['cameras'][key], key
            assert current['camera_groups'] == original['camera_groups']
            assert row.get('configuration_status') is None
            state['external_evidence'].update(retry_applied=True, two_profiles_preserved=True)
        else:
            raise ValueError(stage)
        STATE.write_text(json.dumps(state))
        return
    if stage == 'session-budget-before':
        def full():
            value = json.load(urllib.request.urlopen('http://import-limited:8080/stream-stats'))
            return value['sessions'].get('main') == 1
        wait_for('legacy recorder occupies the only camera slot', full)
        probe = subprocess.run(['ffprobe', '-v', 'error', '-rtsp_transport', 'tcp',
                                '-show_entries', 'stream=codec_type', '-of', 'json',
                                'rtsp://import-limited:8554/main'], capture_output=True, timeout=15)
        assert probe.returncode != 0, 'Fixture must reject an extra camera reader'
        assert frigate('/stats')['cameras']['operator_camera']['camera_fps'] > 0
        print(json.dumps(json.load(urllib.request.urlopen('http://import-limited:8080/stream-stats'))), flush=True)
        return
    if stage in {'handoff-reject', 'handoff-interrupt-start'}:
        endpoint = 'fail-available' if stage == 'handoff-reject' else 'stall-available'
        urllib.request.urlopen('http://import-limited:8080/' + endpoint).read()
        fresh = request_json(base + '/import')
        state['handoff_job'] = fresh['id']
        STATE.write_text(json.dumps(state))
        request_json(base + '/import/' + fresh['id'], method='POST', expected=202,
                     headers={'X-CamAdmiral-Action': 'import-frigate-cameras'},
                     payload={'selected': ['operator_camera'], 'allow_restart': True})
        def metrics():
            return json.load(urllib.request.urlopen('http://import-limited:8080/stream-stats'))
        if stage == 'handoff-interrupt-start':
            wait_for('new reader reached released slot', lambda: metrics()['stalled_sessions'] > 0, timeout=180)
            assert frigate('/config')['cameras']['operator_camera']['enabled'] is False
            code, _, _ = request(base, method='DELETE', headers={'X-CamAdmiral-Action': 'remove-frigate-target'})
            assert code == 409, 'Removing a running integration lost its handoff journal'
            return
        wait_for('failed handoff completed', lambda: request_json(base + '/import/' + fresh['id'])['status'] == 'completed', timeout=240)
        job = request_json(base + '/import/' + fresh['id'])
        row = next(r for r in job['cameras'] if r['key'] == 'operator_camera')
        assert row['status'] == 'error' and row['reason'] == 'source_unavailable', row
        import yaml
        assert yaml.safe_load(frigate('/config/raw')) == yaml.safe_load(state['saved']), 'Failed handoff changed configuration'
        assert len(consumer_directory()['cameras']) == 1, 'Failed handoff adopted unvalidated media'
        assert metrics()['fail_available'] == 0, 'Import never released the saturated legacy reader'
        wait_for('original capture restored', lambda: frigate('/stats')['cameras']['operator_camera']['camera_fps'] > 0, timeout=120)
        return
    if stage == 'handoff-interrupt-verify':
        import yaml
        def restored():
            return (yaml.safe_load(frigate('/config/raw')) == yaml.safe_load(state['saved']) and
                    frigate('/config')['cameras']['operator_camera']['enabled'] and
                    frigate('/stats')['cameras']['operator_camera']['camera_fps'] > 0)
        wait_for('startup automatically restores released reader', restored, timeout=180)
        job = request_json(base + '/import/' + state['handoff_job'])
        assert job['status'] == 'error', job
        assert next(r for r in job['cameras'] if r['key'] == 'operator_camera')['reason'] == 'interrupted_import'
        assert len(consumer_directory()['cameras']) == 1, 'Interrupted validation adopted a camera'
        state['saved'] = frigate('/config/raw')
        STATE.write_text(json.dumps(state))
        return
    if stage.startswith('restart-fault-'):
        endpoint = 'status' if stage == 'restart-fault-verify' else 'arm'
        mode = stage.removeprefix('restart-fault-arm-') if stage != 'restart-fault-arm' else 'disconnect'
        req = urllib.request.Request('http://camadmiral:20017/__fault__/' + endpoint,
                                     data=json.dumps({'mode': mode}).encode() if endpoint == 'arm' else None,
                                     method='POST' if endpoint == 'arm' else 'GET')
        with urllib.request.urlopen(req, timeout=10) as response:
            fault = json.load(response)
        if stage == 'restart-fault-verify':
            assert fault['dropped_replies'] == 1, fault
            assert fault['unavailable_reads'] > 0, fault
            assert fault['armed'] is False, fault
            print(json.dumps(fault), flush=True)
        return
    if stage == 'preview':
        preview = request_json(base + '/import')
        assert preview['restart_required'] is True
        row = next(r for r in preview['cameras'] if r['key'] == 'operator_camera')
        assert row['category'] == 'new', row
        assert frigate('/config/raw') == state['saved'], 'Preview changed Frigate'
        assert len(consumer_directory()['cameras']) == 1, 'Preview adopted a camera'
        matched = next(r for r in preview['cameras'] if r['key'] == 'matched_camera')
        assert matched['category'] == 'conflict' and matched['can_keep_existing']
        ambiguous = next(r for r in preview['cameras'] if r['key'] == 'ambiguous_camera')
        assert ambiguous['reason'] == 'ambiguous_source_match' and not ambiguous['can_import']
        assert next(r for r in preview['cameras'] if r['key'] == 'disabled_camera')['can_import'] is False
        assert next(r for r in preview['cameras'] if r['key'] == 'unsupported_camera')['can_import'] is False
        text = json.dumps(preview)
        assert 'synthetic-camera-secret' not in text and 'synthetic-onvif-secret' not in text
        code, body, _ = request(base + '/import/' + preview['id'], method='POST',
                                payload={'selected': ['operator_camera'], 'allow_restart': False},
                                headers={'X-CamAdmiral-Action': 'import-frigate-cameras'})
        assert code == 409
        assert frigate('/config/raw') == state['saved']
        return
    if stage == 'verify':
        wait_for('imported capture', lambda: frigate('/stats').get('cameras', {}).get(
            'operator_camera', {}).get('camera_fps', 0) > 0, timeout=180)
        current = frigate('/config')['cameras']['operator_camera']
        before = state['camera']
        for field in ('friendly_name', 'detect', 'record', 'objects', 'zones', 'motion', 'enabled'):
            assert current.get(field) == before.get(field), 'Changed preserved field: ' + field
        assert len(consumer_directory()['cameras']) == 3
        directory = consumer_directory()['cameras']
        assert state['existing_uuid'] in {c['id'] for c in directory}
        assert frigate('/config')['camera_groups'] == state['groups']
        for key in ('disabled_camera', 'offline_camera', 'unsupported_camera', 'ambiguous_camera'):
            assert frigate('/config')['cameras'][key] == state['all_cameras'][key], key
        preview = request_json(base + '/import')
        row = next(r for r in preview['cameras'] if r['key'] == 'operator_camera')
        assert row['category'] == 'managed', row
        assert set(frigate('/config')['cameras']) == set(state['all_cameras'])
        raw = frigate('/config/raw_paths')
        assert all('127.0.0.1:8554/' in i['path'] for i in raw['cameras']['operator_camera']['ffmpeg']['inputs'])
        for alias in ('operator_stream', 'matched_stream', 'authenticated_stream'):
            assert ':18554/' in str(raw['go2rtc']['streams'][alias]), alias
        for key in ('operator_camera', 'matched_camera', 'authenticated_camera'):
            assert all('127.0.0.1:8554/' in i['path'] for i in raw['cameras'][key]['ffmpeg']['inputs']), key
        return
    if stage == 'roles':
        directory = consumer_directory()['cameras']
        camera = next(c for c in directory if c['name'] == 'Original authenticated')
        sub = next(s for s in camera['streams'] if s.get('video', {}).get('width', s.get('width')) == 640)
        request_json('/internal/cameras/' + camera['id'] + '/stream-settings', method='POST', timeout=60,
                     headers={'X-CamAdmiral-Action': 'set-camera-stream-settings'},
                     payload={'enabled': [s['id'] for s in camera['streams']],
                              'roles': {'record': sub['id'], 'detect': sub['id']}})
        def role_applied():
            streams = frigate('/config/raw_paths')['go2rtc']['streams']
            record = next(v for k, v in streams.items() if k.endswith('_import_record') and sub['id'] in str(v))
            return bool(record)
        wait_for('imported role synchronization', role_applied, timeout=120)
        assert frigate('/config')['cameras']['authenticated_camera']['friendly_name'] == 'Original authenticated'
        assert frigate('/config')['cameras']['operator_camera']['detect']['fps'] == 3
        return
    if stage == 'sync':
        directory = consumer_directory()['cameras']
        matched = next(c for c in directory if c['id'] == state['existing_uuid'])
        assert matched['name'] == 'Different CamAdmiral loading name'
        full_sync_when_idle(state['target_id'])
        assert frigate('/config')['cameras']['matched_camera']['friendly_name'] == 'Original loading'
        assert frigate('/config')['cameras']['operator_camera']['detect']['fps'] == 3
        # Repeated selection of the same completed journal cannot add a camera.
        prior = request_json(base + '/import/' + state['job_id'])
        workers_before = frigate('/stats')['cameras']['operator_camera']
        request_json(base + '/import/' + state['job_id'], method='POST', expected=202,
                     payload={'selected': ['operator_camera'], 'allow_restart': True},
                     headers={'X-CamAdmiral-Action': 'import-frigate-cameras'})
        wait_for('repeat import finished', lambda: request_json(base + '/import/' + state['job_id'])['status'] == 'completed')
        assert len(consumer_directory()['cameras']) == 3
        workers_after = frigate('/stats')['cameras']['operator_camera']
        assert workers_before['capture_pid'] == workers_after['capture_pid'], 'Repeat import replaced capture workers'
        return
    if stage in {'dns-before', 'dns-after'}:
        camera = next(c for c in consumer_directory()['cameras'] if c['name'] == 'Original authenticated')
        signature = [(s['id'], s['downstream']['url']) for s in camera['streams']]
        if stage == 'dns-before':
            state['dns_signature'] = signature
            state['dns_camera_uuid'] = camera['id']
            STATE.write_text(json.dumps(state))
            return
        assert camera['id'] == state['dns_camera_uuid']
        assert signature == [tuple(s) for s in state['dns_signature']]
        diagnostic = {'attempts': 0}
        def moved_media():
            diagnostic['attempts'] += 1
            paths = frigate('/config/raw_paths')['cameras']['authenticated_camera']['ffmpeg']['inputs']
            alias = next(i['path'].rsplit('/', 1)[1] for i in paths if 'record' in i['roles'])
            frame = subprocess.run(['ffmpeg', '-v', 'error', '-rtsp_transport', 'tcp',
                                    '-i', 'rtsp://frigate:8554/' + alias, '-frames:v', '1',
                                    '-threads', '1', '-f', 'image2pipe', '-vcodec', 'mjpeg', '-'],
                                   capture_output=True, timeout=30)
            diagnostic.update(returncode=frame.returncode, frame_bytes=len(frame.stdout), error=frame.stderr.decode(errors='replace')[-2000:])
            if frame.returncode or not frame.stdout.startswith(b'\xff\xd8'): return False
            meta = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'stream=width,height',
                                   '-of', 'json', '-i', 'pipe:0'], input=frame.stdout,
                                  capture_output=True, timeout=10)
            diagnostic['streams'] = json.loads(meta.stdout).get('streams')
            return diagnostic['streams'] == [{'width': 480, 'height': 270}]
        try:
            wait_for('imported Frigate camera decodes moved DNS source', moved_media, timeout=180)
        except Exception:
            for label, path in [('stats', '/stats'), ('paths', '/config/raw_paths'), ('runtime', '/go2rtc/streams')]:
                try: diagnostic[label] = frigate(path)
                except Exception: diagnostic[label] = {'unavailable': True}
            state['dns_failure'] = diagnostic
            STATE.write_text(json.dumps(state))
            raise
        assert frigate('/config')['cameras']['authenticated_camera']['friendly_name'] == 'Original authenticated'
        assert frigate('/config')['cameras']['authenticated_camera']['detect']['width'] == 640
        return
    if stage == 'interrupt-start':
        urllib.request.urlopen('http://import-stalled:8080/stall').read()
        camera = {'enabled': True, 'friendly_name': 'Synthetic interrupted camera',
                  'ffmpeg': {'inputs': [{'path': 'rtsp://import-stalled:8554/main', 'roles': ['record', 'detect']}]},
                  'detect': {'width': 640, 'height': 360}}
        payload = json.dumps({'requires_restart': 1, 'config_data': {'cameras': {'interrupted_camera': camera}}}).encode()
        urllib.request.urlopen(urllib.request.Request('http://frigate:5000/api/config/set', data=payload,
                               headers={'Content-Type': 'application/json'}, method='PUT')).read()
        # Raw config is the durable source even before capture workers start.
        fresh = request_json(base + '/import')
        row = next(r for r in fresh['cameras'] if r['key'] == 'interrupted_camera')
        assert row['can_import'], row
        state.update(interrupted_job=fresh['id'], interrupted_camera=camera)
        STATE.write_text(json.dumps(state))
        request_json(base + '/import/' + fresh['id'], method='POST', expected=202,
                     headers={'X-CamAdmiral-Action': 'import-frigate-cameras'},
                     payload={'selected': ['interrupted_camera'], 'allow_restart': True})
        wait_for('probe entered stalled source', lambda: json.load(urllib.request.urlopen(
            'http://import-stalled:8080/stream-stats'))['describes'].get('main', 0) > 0)
        assert request_json(base + '/import/' + fresh['id'])['status'] == 'running'
        return
    if stage == 'interrupt-verify':
        import yaml
        job = request_json(base + '/import/' + state['interrupted_job'])
        assert job['status'] == 'error', job['status']
        assert yaml.safe_load(frigate('/config/raw'))['cameras']['interrupted_camera'] == state['interrupted_camera']
        assert len(consumer_directory()['cameras']) == 3
        urllib.request.urlopen('http://import-stalled:8080/resume').read()
        request_json(base + '/import/' + job['id'], method='POST', expected=202,
                     headers={'X-CamAdmiral-Action': 'import-frigate-cameras'},
                     payload={'selected': ['interrupted_camera'], 'allow_restart': True})
        def finished():
            current = request_json(base + '/import/' + job['id'])
            if current['status'] != 'completed': return False
            row = next(r for r in current['cameras'] if r['key'] == 'interrupted_camera')
            assert row['status'] == 'applied', row
            return True
        wait_for('interrupted import retried', finished, timeout=180)
        assert len(consumer_directory()['cameras']) == 4
        return
    if stage == 'rollback':
        request_json(base + '/import-rollback', method='POST', timeout=120,
                     payload={'camera_key': 'operator_camera', 'allow_restart': True},
                     headers={'X-CamAdmiral-Action': 'rollback-frigate-import'})
        import yaml
        assert yaml.safe_load(frigate('/config/raw'))['cameras']['operator_camera'] == yaml.safe_load(state['saved'])['cameras']['operator_camera']
        assert frigate('/config')['cameras']['operator_camera']['friendly_name'] == 'Original entrance'
        assert len(consumer_directory()['cameras']) == 3, 'Rollback deleted adopted data'
        return
    raise ValueError(stage)


def host():
    from playwright.sync_api import sync_playwright, expect
    artifacts = ROOT / 'e2e-artifacts/frigate-import'
    artifacts.mkdir(parents=True, exist_ok=True)
    compose = ['docker', 'compose', '-p', 'camadmiral-frigate-import',
               '-f', str(ROOT / 'e2e/compose.yaml'),
               '-f', str(ROOT / 'e2e/recording-compose.yaml'),
               '-f', str(ROOT / 'e2e/frigate-import-compose.yaml')]
    result = {'status': 'failed', 'revision': subprocess.check_output(
        ['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()}

    def run(*args, check=True):
        p = subprocess.run([*compose, *args], cwd=ROOT, text=True,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        with (artifacts / 'transcript.log').open('a') as log:
            log.write(p.stdout)
        if check and p.returncode:
            print(p.stdout[-8000:], flush=True)
            p.check_returncode()
        return p.stdout.strip()

    def stage(name):
        print('Import regression stage: ' + name, flush=True)
        run('run', '--rm', '--entrypoint', 'python', 'test-driver', '/e2e/frigate_import.py', name)

    def source_generations():
        code = """import json,sys
from pathlib import Path
sys.path.insert(0,'/opt/camadmiral')
from camadmiral import media
from camadmiral.crypto import load_master_key
from camadmiral.storage import CameraRepository
from camadmiral.frigate_import import ImportStore
repository=CameraRepository(Path('/var/lib/camadmiral/camadmiral.db'),load_master_key())
helper=getattr(media,'source_connection_revisions',None)
live=helper(repository.managed_stream_runtime_sources()) if callable(helper) else {}
result={'supported':callable(helper),'saved':{},'live':{}}
for target in repository.frigate_targets():
 row=ImportStore(repository).links(target['target_id']).get('authenticated_camera')
 if row:
  saved=row.get('relay_source_revisions') or {}
  result.update(saved=saved,live={key:live[key] for key in saved if key in live})
print(json.dumps(result))
"""
        return json.loads(run('exec', '-T', 'camadmiral', 'python', '-c', code))

    def recordings(camera='operator_camera'):
        code = """import json,sqlite3
from pathlib import Path
with sqlite3.connect('file:/config/frigate.db?mode=ro',uri=True) as db:
 rows=db.execute("select id,path,start_time,end_time from recordings where camera=? order by start_time",(CAMERA,)).fetchall()
print(json.dumps([list(r) for r in rows if Path(r[1]).is_file() and Path(r[1]).stat().st_size>0]))
"""
        return json.loads(run('exec', '-T', 'frigate', 'python3', '-c', 'CAMERA='+repr(camera)+'\n'+code))

    try:
        run('down', '--volumes', '--remove-orphans')
        run('build', 'camadmiral')
        run('up', '-d', 'camadmiral', 'camera-open', 'camera-auth', 'import-stalled', 'import-limited', 'import-external', 'frigate', 'import-api-proxy')
        result['images'] = subprocess.check_output(['docker', 'image', 'inspect',
            'camadmiral-e2e:local', 'ghcr.io/blakeblackshear/frigate:0.17.2',
            '--format', '{{.Id}} {{json .RepoDigests}}'], text=True).strip().splitlines()
        stage('prepare')
        deadline = time.monotonic() + 180
        before = []
        while time.monotonic() < deadline:
            before = recordings()
            if len(before) >= 2:
                break
            time.sleep(3)
        assert len(before) >= 2, 'Fixture must save real recordings before migration'
        result['baseline_recordings'] = before
        stage('session-budget-before')
        seed = """import json,sqlite3,time,urllib.request
from pathlib import Path
stamp=time.time()-20
thumb='/media/frigate/clips/synthetic-import-review.jpg'
Path(thumb).parent.mkdir(parents=True,exist_ok=True)
Path(thumb).write_bytes(urllib.request.urlopen('http://127.0.0.1:5000/api/operator_camera/latest.jpg').read())
with sqlite3.connect('/config/frigate.db') as db:
 event={'id':'synthetic-import-event','camera':'operator_camera','label':'person','start_time':stamp,'end_time':stamp+10,'top_score':0.9,'score':0.9,'false_positive':0,'zones':'[]','thumbnail':'','has_clip':1,'has_snapshot':0,'region':'[]','box':'[]','area':100,'retain_indefinitely':1,'ratio':1,'plus_id':'','model_hash':'synthetic','detector_type':'cpu','model_type':'synthetic','data':'{}'}
 columns={r[1] for r in db.execute('pragma table_info(event)')}
 event={k:v for k,v in event.items() if k in columns}
 db.execute('insert into event ('+','.join(event)+') values ('+','.join('?' for _ in event)+')',list(event.values()))
 db.execute('insert into reviewsegment(id,camera,start_time,end_time,severity,thumb_path,data) values (?,?,?,?,?,?,?)',('synthetic-import-review','operator_camera',stamp,stamp+10,'alert',thumb,json.dumps({'detections':['synthetic-import-event'],'objects':['person'],'zones':[]})))
 db.commit()
print(json.dumps({'events':db.execute("select * from event where id='synthetic-import-event'").fetchall(),'review':db.execute("select * from reviewsegment where id='synthetic-import-review'").fetchall()}))
"""
        # Inspect fixture history independently after every migration/restart.
        history_code = """import json,sqlite3
with sqlite3.connect('file:/config/frigate.db?mode=ro',uri=True) as db:
 print(json.dumps({'events':db.execute("select * from event where id='synthetic-import-event'").fetchall(),'review':db.execute("select * from reviewsegment where id='synthetic-import-review'").fetchall()}))
"""
        history_before = json.loads(run('exec', '-T', 'frigate', 'python3', '-c', seed))
        assert history_before['events'] and history_before['review']
        result['history_before'] = history_before
        stage('handoff-reject')
        result['failed_handoff_restored'] = True
        stage('handoff-interrupt-start')
        run('restart', 'camadmiral')
        run('restart', 'import-api-proxy')
        stage('handoff-interrupt-verify')
        result['interrupted_handoff_restored_automatically'] = True
        stage('preview')
        stage('restart-fault-arm')
        base = 'http://' + run('port', 'camadmiral', '18080')
        with sync_playwright() as p:
            browser = p.webkit.launch()
            page = browser.new_page(http_credentials={'username': 'admin', 'password': 'synthetic-e2e-admin-password'})
            try:
                page.goto(base + '/settings/integrations')
                page.get_by_role('button', name='Import from Frigate', exact=True).click()
                expect(page.get_by_text('Original entrance', exact=False)).to_be_visible()
                first_camera = page.get_by_label('Import Original entrance', exact=True)
                assert page.get_by_role('button', name='Select all', exact=True).bounding_box()['y'] < first_camera.bounding_box()['y'], 'Select all must precede the camera list'
                assert 'synthetic-camera-secret' not in page.locator('#app-modal').inner_text()
                page.locator('#app-modal-close').click()
                page.get_by_role('button', name='Import from Frigate', exact=True).click()
                page.get_by_label('Keep CamAdmiral settings for Original loading', exact=True).check()
                page.get_by_role('button', name='Select all', exact=True).click()
                page.get_by_label('Allow Frigate restart').check()
                with page.expect_response(lambda r: r.request.method == 'POST' and '/import/' in r.url) as imported:
                    page.get_by_role('button', name='Import and manage selected', exact=True).click()
                import_id = imported.value.json()['id']
                selected_labels = ['Import Original entrance', 'Import Original loading',
                                   'Import Original authenticated', 'Import Original offline']
                for label in selected_labels:
                    expect(page.get_by_label(label, exact=True)).to_be_checked()
                    expect(page.get_by_label(label, exact=True)).to_be_disabled()
                expect(page.locator('.import-status .inline-spinner')).to_be_visible()
                expect(page.locator('.import-status .inline-spinner')).to_be_in_viewport()
                expect(page.get_by_role('progressbar', name='Camera import progress')).to_be_visible()
                expect(page.locator('#app-modal').get_by_role('status')).to_be_in_viewport()
                expect(page.get_by_label('Allow Frigate restart')).to_be_disabled()
                expect(page.get_by_role('button', name='Select all', exact=True)).to_be_disabled()
                expect(page.locator('#app-modal').get_by_role('status')).to_contain_text('Importing')
                expect(page.locator('#app-modal').get_by_role('status')).to_contain_text("Releasing Frigate's camera connection", timeout=120000)
                expect(page.locator('#app-modal').get_by_role('status')).to_contain_text('Waiting for Frigate to restart and video to resume', timeout=120000)
                page.locator('#app-modal-close').click()
                page.get_by_role('button', name='Import from Frigate', exact=True).click()
                for label in selected_labels:
                    expect(page.get_by_label(label, exact=True)).to_be_checked()
                    expect(page.get_by_label(label, exact=True)).to_be_disabled()
                expect(page.get_by_label('Keep CamAdmiral settings for Original loading', exact=True)).to_be_checked()
                expect(page.get_by_label('Keep CamAdmiral settings for Original loading', exact=True)).to_be_disabled()
                expect(page.locator('.import-status .inline-spinner')).to_be_visible()
                page.screenshot(path=str(artifacts / 'import-progress.png'))
                run('run', '--rm', '--entrypoint', 'python', 'test-driver', '-c',
                    "import json;from pathlib import Path;p=Path('/state/frigate-import.json');s=json.loads(p.read_text());s['job_id']=" + repr(import_id) + ";p.write_text(json.dumps(s))")
                expect(page.get_by_label('Allow Frigate restart')).to_be_enabled(timeout=300000)
                expect(page.locator('#app-modal').get_by_role('status')).to_contain_text('Import finished with errors: 3 cameras managed, 1 failed, 0 pending.')
                expect(page.get_by_text('Managed', exact=True)).to_have_count(3)
                expect(page.locator('.import-status .inline-spinner')).to_be_hidden()
                expect(page.get_by_role('progressbar', name='Camera import progress')).to_be_hidden()
                for label in selected_labels[:3]:
                    expect(page.get_by_label(label, exact=True)).to_be_checked()
                    expect(page.get_by_label(label, exact=True)).to_be_disabled()
                expect(page.get_by_label('Import Original offline', exact=True)).to_be_checked()
                expect(page.get_by_label('Import Original offline', exact=True)).to_be_enabled()
                page.get_by_label('Allow Frigate restart').check()
                retry_busy = {'injected': False}
                def busy_once(route):
                    if route.request.method == 'POST' and not retry_busy['injected']:
                        retry_busy['injected'] = True
                        route.fulfill(status=409, content_type='application/json', body=json.dumps({'status': 'sync_busy'}))
                    else:
                        route.continue_()
                page.route('**/internal/frigate-targets/*/import/*', busy_once)
                with page.expect_request(lambda r: r.method == 'POST' and '/import/' in r.url) as retried:
                    page.get_by_role('button', name='Retry selected', exact=True).click()
                assert retried.value.post_data_json['selected'] == ['offline_camera'], 'Retry must exclude completed cameras'
                expect(page.locator('#app-modal').get_by_role('status')).to_contain_text('Import finished with errors: 3 cameras managed, 1 failed, 0 pending.', timeout=120000)
                expect(page.locator('#app-modal').get_by_role('status')).to_be_in_viewport()
                assert retry_busy['injected'], 'Retry must exercise a real temporary-busy response'
                page.unroute('**/internal/frigate-targets/*/import/*', busy_once)
                result['busy_import_retry_preserved_selection'] = True
                result['ui_progress_and_partial_results'] = True
                page.screenshot(path=str(artifacts / 'import-results.png'))
            except Exception:
                page.screenshot(path=str(artifacts / 'failure.png'))
                raise
            finally:
                browser.close()
        stage('verify')
        stage('restart-fault-verify')
        result['lost_restart_reply_recovered'] = True
        stage('sync')
        stage('roles')
        stage('dns-before')
        source_before = source_generations()
        if source_before['supported']:
            deadline = time.monotonic() + 90
            while not (source_before['saved'] and source_before['saved'] == source_before['live']):
                if time.monotonic() >= deadline:
                    raise AssertionError('Source generations must be acknowledged before the move')
                time.sleep(1)
                source_before = source_generations()
        result['dns_source_generations_before'] = source_before
        run('stop', 'camera-auth')
        run('rm', '--force', 'camera-auth')
        run('up', '-d', 'camera-auth-import-moved')
        stage('dns-after')
        if source_before['supported']:
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                source_after = source_generations()
                if (source_after['saved'] and source_after['saved'] == source_after['live']
                        and source_after['saved'] != source_before['saved']):
                    break
                time.sleep(1)
            else:
                raise AssertionError('Recovered media did not acknowledge its new upstream connections')
            result['dns_source_generations_after'] = source_after
        result['address_recovery_decoded'] = True
        switched = time.time()
        deadline = time.monotonic() + 150
        after = []
        while time.monotonic() < deadline:
            after = recordings()
            if any(r[2] > switched for r in after):
                break
            time.sleep(3)
        assert any(r[2] > switched for r in after), 'No new recording after takeover'
        per_camera = {}
        for key in ['operator_camera', 'matched_camera', 'authenticated_camera']:
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                recent = [r for r in recordings(key) if r[2] > switched]
                if recent:
                    per_camera[key] = len(recent)
                    break
                time.sleep(3)
            assert key in per_camera, 'Selected camera has no fresh recording: ' + key
        result['selected_camera_fresh_recordings'] = per_camera
        result['saturated_session_handoff'] = True
        assert {r[0] for r in before} <= {r[0] for r in after}, 'Lost historical recordings'
        run('restart', 'camadmiral', 'frigate')
        run('restart', 'import-api-proxy')
        stage('verify')
        history_after = json.loads(run('exec', '-T', 'frigate', 'python3', '-c', history_code))
        assert history_after == history_before, 'Event or review history changed'
        # Read the historical clip through Frigate's playback API and decode a frame.
        playback = f"http://frigate:5000/api/operator_camera/start/{before[0][2]}/end/{before[0][3]}/clip.mp4"
        run('run', '--rm', '--entrypoint', 'ffmpeg', 'test-driver', '-v', 'error', '-i', playback,
            '-frames:v', '1', '-f', 'null', '-')
        result['historical_playback_decoded'] = True
        stage('restart-fault-arm-partial')
        stage('rollback')
        stage('restart-fault-verify')
        result['rollback_restart_reply_recovered'] = True
        stage('interrupt-start')
        run('restart', 'camadmiral')
        run('restart', 'import-api-proxy')
        stage('restart-fault-arm-empty')
        stage('interrupt-verify')
        stage('restart-fault-verify')
        result['empty_restart_reply_recovered'] = True
        result['interrupted_import_recovered'] = True

        # An independent recorder owns the Record slot throughout the failed
        # handoff. Detect remains live and cannot establish Record readiness.
        stage('external-prepare')
        run('restart', 'frigate')
        run('restart', 'import-api-proxy')
        deadline = time.monotonic() + 180
        external_before = []
        while time.monotonic() < deadline:
            external_before = recordings('external_camera')
            if len(external_before) >= 2:
                break
            time.sleep(3)
        assert len(external_before) >= 2, 'Separate-profile fixture must save real history before contention'
        external_history_code = history_code.replace('synthetic-import', 'synthetic-external')
        external_seed = seed.replace('operator_camera', 'external_camera').replace('synthetic-import', 'synthetic-external')
        external_history = json.loads(run('exec', '-T', 'frigate', 'python3', '-c', external_seed))
        result['external_baseline_recordings'] = external_before
        result['external_history_before'] = external_history
        stage('external-pause')
        run('restart', 'frigate')
        run('restart', 'import-api-proxy')
        run('up', '-d', 'external-recorder')
        stage('external-reader-ready')
        run('restart', 'frigate')
        run('restart', 'import-api-proxy')
        stage('external-blocked')

        def external_state():
            return json.loads(run('run', '--rm', '--entrypoint', 'python', 'test-driver', '-c',
                "import json;from pathlib import Path;s=json.loads(Path('/state/frigate-import.json').read_text());print(json.dumps({k:v for k,v in s.items() if k.startswith('external_')}))"))

        # Docker may assign a new ephemeral host port after the recovery restart.
        base = 'http://' + run('port', 'camadmiral', '18080')
        with sync_playwright() as p:
            browser = p.webkit.launch()
            page = browser.new_page(http_credentials={'username': 'admin', 'password': 'synthetic-e2e-admin-password'})
            try:
                page.goto(base + '/settings/integrations')
                page.get_by_role('button', name='Import from Frigate', exact=True).click()
                page.get_by_label('Import Synthetic shared source', exact=True).check()
                page.get_by_label('Allow Frigate restart').check()
                with page.expect_response(lambda r: r.request.method == 'POST' and '/import/' in r.url) as attempt:
                    page.get_by_role('button', name='Import and manage selected', exact=True).click()
                external_job = attempt.value.json()['id']
                run('run', '--rm', '--entrypoint', 'python', 'test-driver', '-c',
                    "import json;from pathlib import Path;p=Path('/state/frigate-import.json');s=json.loads(p.read_text());s['external_job']=" + repr(external_job) + ";p.write_text(json.dumps(s))")
                expect(page.get_by_label('Allow Frigate restart')).to_be_enabled(timeout=300000)
                stage('external-failed')
                evidence = external_state()
                result['external_reader'] = evidence['external_evidence']
                assert not any(r[2] > evidence['external_blocked_since'] for r in recordings('external_camera')), 'Detect FPS masked fresh Record media unexpectedly'
                assert {r[0] for r in external_before} <= {r[0] for r in recordings('external_camera')}, 'Failed handoff lost recordings'
                assert json.loads(run('exec', '-T', 'frigate', 'python3', '-c', external_history_code)) == external_history
                deadline = time.monotonic() + 120
                while time.monotonic() < deadline:
                    if any(r[2] > evidence['external_blocked_since'] for r in recordings('matched_camera')):
                        break
                    time.sleep(3)
                else:
                    raise AssertionError('Unrelated managed camera stopped recording after the failed handoff')
                result['external_reader']['unrelated_recording_preserved'] = True
                # This assertion reproduces the misleading result on the unchanged
                # application baseline, after the physical contention is proven.
                expect(page.get_by_text('Original Frigate configuration restored. Video availability still needs checking.', exact=True)).to_be_visible()
                expect(page.locator('#app-modal')).not_to_contain_text("Frigate's existing connection was not changed")
                assert result['external_reader']['configuration_status'] == 'restored'
                page.screenshot(path=str(artifacts / 'external-reader-restored.png'))

                run('stop', 'external-recorder')
                with page.expect_request(lambda r: r.method == 'POST' and '/import/' in r.url) as retry:
                    page.get_by_role('button', name='Retry selected', exact=True).click()
                assert retry.value.post_data_json['selected'] == ['external_camera']
                expect(page.get_by_label('Allow Frigate restart')).to_be_enabled(timeout=300000)
                stage('external-success')
                evidence = external_state()
                result['external_reader'].update(evidence['external_evidence'])
                page.screenshot(path=str(artifacts / 'external-reader-retry.png'))
            except Exception:
                page.screenshot(path=str(artifacts / 'external-reader-failure.png'))
                raise
            finally:
                browser.close()
        resumed = time.time()
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            external_after = recordings('external_camera')
            if any(r[2] > resumed for r in external_after):
                break
            time.sleep(3)
        assert any(r[2] > resumed for r in external_after), 'Retry did not save fresh Record media'
        assert {r[0] for r in external_before} <= {r[0] for r in external_after}, 'Retry lost historical recordings'
        assert json.loads(run('exec', '-T', 'frigate', 'python3', '-c', external_history_code)) == external_history
        playback = f"http://frigate:5000/api/external_camera/start/{external_before[0][2]}/end/{external_before[0][3]}/clip.mp4"
        run('run', '--rm', '--entrypoint', 'ffmpeg', 'test-driver', '-v', 'error', '-i', playback,
            '-frames:v', '1', '-f', 'null', '-')
        result['external_reader'].update(fresh_recording_after_retry=True, historical_playback_decoded=True,
                                         event_and_review_history_preserved=True)
        result.update(status='passed', final_recordings=after)
    finally:
        try:
            result['session_budget'] = json.loads(run('run', '--rm', '--entrypoint', 'python',
                'test-driver', '-c', "import json,urllib.request;print(json.dumps(json.load(urllib.request.urlopen('http://import-limited:8080/stream-stats'))))"))
        except Exception:
            result['session_budget'] = {'unavailable': True}
        (artifacts / 'result.json').write_text(json.dumps(result, indent=2))
        try:
            state_text = run('run', '--rm', '--entrypoint', 'python', 'test-driver', '-c',
                             "from pathlib import Path;print(Path('/state/frigate-import.json').read_text())")
            (artifacts / 'driver-state.json').write_text(state_text)
        except Exception:
            pass
        run('logs', '--no-color', check=False)
        if not os.environ.get('CAMADMIRAL_E2E_KEEP'):
            run('down', '--volumes', '--remove-orphans', check=False)


if __name__ == '__main__':
    driver(sys.argv[1]) if len(sys.argv) > 1 else host()

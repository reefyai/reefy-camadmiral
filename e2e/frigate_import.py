"""Durable import/takeover regression against real Frigate and synthetic media.

Run by the GitHub Actions release gate, never against an operator's device.
"""
from __future__ import annotations

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
    from scenarios import request_json, request, wait_for, wait_for_health, consumer_directory

    def frigate(path):
        with urllib.request.urlopen('http://frigate:5000/api' + path, timeout=10) as r:
            return json.load(r)

    wait_for_health()
    wait_for('Frigate API', lambda: frigate('/stats'), timeout=180)
    if stage == 'prepare':
        request_json('/internal/frigate-targets', method='POST', expected=201,
                     payload={'name': 'Synthetic import target', 'api_url': 'http://frigate:5000'},
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
        request_json(base + '/full-sync', method='POST', timeout=120,
                     headers={'X-CamAdmiral-Action': 'full-sync-frigate-target'})
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
        def moved_media():
            paths = frigate('/config/raw_paths')['cameras']['authenticated_camera']['ffmpeg']['inputs']
            alias = next(i['path'].rsplit('/', 1)[1] for i in paths if 'record' in i['roles'])
            frame = subprocess.run(['ffmpeg', '-v', 'error', '-rtsp_transport', 'tcp',
                                    '-i', 'rtsp://frigate:8554/' + alias, '-frames:v', '1',
                                    '-threads', '1', '-f', 'image2pipe', '-vcodec', 'mjpeg', '-'],
                                   capture_output=True, timeout=30)
            if frame.returncode or not frame.stdout.startswith(b'\xff\xd8'): return False
            meta = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'stream=width,height',
                                   '-of', 'json', '-i', 'pipe:0'], input=frame.stdout,
                                  capture_output=True, timeout=10)
            return json.loads(meta.stdout).get('streams') == [{'width': 480, 'height': 270}]
        wait_for('imported Frigate camera decodes moved DNS source', moved_media, timeout=180)
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

    def recordings():
        code = """import json,sqlite3
from pathlib import Path
with sqlite3.connect('file:/config/frigate.db?mode=ro',uri=True) as db:
 rows=db.execute("select id,path,start_time,end_time from recordings where camera='operator_camera' order by start_time").fetchall()
print(json.dumps([list(r) for r in rows if Path(r[1]).is_file() and Path(r[1]).stat().st_size>0]))
"""
        return json.loads(run('exec', '-T', 'frigate', 'python3', '-c', code))

    try:
        run('down', '--volumes', '--remove-orphans')
        run('build', 'camadmiral')
        run('up', '-d', 'camadmiral', 'camera-open', 'camera-auth', 'import-stalled', 'frigate')
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
        stage('preview')
        base = 'http://' + run('port', 'camadmiral', '18080')
        with sync_playwright() as p:
            browser = p.webkit.launch()
            page = browser.new_page(http_credentials={'username': 'admin', 'password': 'synthetic-e2e-admin-password'})
            try:
                page.goto(base + '/settings/integrations')
                page.get_by_role('button', name='Import from Frigate', exact=True).click()
                expect(page.get_by_text('Original entrance', exact=False)).to_be_visible()
                assert 'synthetic-camera-secret' not in page.locator('#app-modal').inner_text()
                page.locator('#app-modal-close').click()
                page.get_by_role('button', name='Import from Frigate', exact=True).click()
                page.get_by_label('Keep CamAdmiral settings for Original loading', exact=True).check()
                page.get_by_role('button', name='Select all', exact=True).click()
                page.get_by_label('Allow Frigate restart').check()
                with page.expect_response(lambda r: r.request.method == 'POST' and '/import/' in r.url) as imported:
                    page.get_by_role('button', name='Import and manage selected', exact=True).click()
                import_id = imported.value.json()['id']
                run('run', '--rm', '--entrypoint', 'python', 'test-driver', '-c',
                    "import json;from pathlib import Path;p=Path('/state/frigate-import.json');s=json.loads(p.read_text());s['job_id']=" + repr(import_id) + ";p.write_text(json.dumps(s))")
                expect(page.get_by_text('Import finished. Review each camera\'s result below.', exact=True)).to_be_visible(timeout=300000)
                expect(page.get_by_text('applied', exact=True)).to_have_count(3)
                page.screenshot(path=str(artifacts / 'import-results.png'))
            except Exception:
                page.screenshot(path=str(artifacts / 'failure.png'))
                raise
            finally:
                browser.close()
        stage('verify')
        stage('sync')
        stage('roles')
        stage('dns-before')
        run('stop', 'camera-auth')
        run('rm', '--force', 'camera-auth')
        run('up', '-d', 'camera-auth-import-moved')
        stage('dns-after')
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
        assert {r[0] for r in before} <= {r[0] for r in after}, 'Lost historical recordings'
        run('restart', 'camadmiral', 'frigate')
        stage('verify')
        history_after = json.loads(run('exec', '-T', 'frigate', 'python3', '-c', history_code))
        assert history_after == history_before, 'Event or review history changed'
        # Read the historical clip through Frigate's playback API and decode a frame.
        playback = f"http://frigate:5000/api/operator_camera/start/{before[0][2]}/end/{before[0][3]}/clip.mp4"
        run('run', '--rm', '--entrypoint', 'ffmpeg', 'test-driver', '-v', 'error', '-i', playback,
            '-frames:v', '1', '-f', 'null', '-')
        result['historical_playback_decoded'] = True
        stage('rollback')
        stage('interrupt-start')
        run('restart', 'camadmiral')
        stage('interrupt-verify')
        result['interrupted_import_recovered'] = True
        result.update(status='passed', final_recordings=after)
    finally:
        (artifacts / 'result.json').write_text(json.dumps(result, indent=2))
        run('logs', '--no-color', check=False)
        if not os.environ.get('CAMADMIRAL_E2E_KEEP'):
            run('down', '--volumes', '--remove-orphans', check=False)


if __name__ == '__main__':
    driver(sys.argv[1]) if len(sys.argv) > 1 else host()

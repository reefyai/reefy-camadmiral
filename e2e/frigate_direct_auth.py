"""Real direct-input authentication and preserved history on supported Frigate versions."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
BASELINE = '9d33ec8371b47495d8815d88ce7f8e410f727df0'
STATE = Path('/state/direct-auth-state.json')


def driver(stage):
    import yaml
    from scenarios import request_json, consumer_directory, directory_signature, wait_for, wait_for_health
    from frigate_identity import recording_for_playback, recording_playback_url

    def frigate(path):
        return json.load(urllib.request.urlopen('http://frigate:5000/api' + path, timeout=15))

    def save(state):
        STATE.write_text(json.dumps(state))

    def play(uri, name, dimensions):
        destination = Path('/state/' + name + '.mp4')
        destination.write_bytes(urllib.request.urlopen(uri, timeout=30).read())
        probe = json.loads(subprocess.check_output(['ffprobe', '-v', 'error', '-show_entries',
            'stream=codec_type,width,height', '-of', 'json', str(destination)], timeout=15))
        video = next(s for s in probe['streams'] if s['codec_type'] == 'video')
        assert (video['width'], video['height']) == tuple(dimensions), probe
        result = subprocess.run(['ffmpeg', '-v', 'error', '-threads', '1', '-i', str(destination),
            '-an', '-frames:v', '15', '-f', 'framemd5', '-'], capture_output=True, timeout=20)
        assert result.returncode == 0, 'Recorded playback decoder failed'
        frames = sum(bool(line.strip()) and not line.startswith(b'#') for line in result.stdout.splitlines())
        assert frames >= 15, 'Recorded playback must contain decoded video'
        return {'decoded_frames': frames, 'dimensions': dimensions, 'bytes': destination.stat().st_size}

    wait_for_health()
    wait_for('Frigate API', lambda: frigate('/stats'), timeout=180)
    if stage == 'prepare':
        for credential, expected in [('synthetic-camera-secret', True), ('synthetic-wrong-secret', False)]:
            checked = subprocess.run(['ffprobe', '-v', 'error', '-rtsp_transport', 'tcp',
                '-show_entries', 'stream=codec_type', '-of', 'json',
                'rtsp://operator:' + credential + '@camera-auth:8554/live'], capture_output=True, timeout=20)
            assert (checked.returncode == 0) == expected, 'Synthetic camera must enforce its password'
        request_json('/internal/frigate-targets', method='POST', expected=201,
            payload={'name': 'Synthetic direct-auth target', 'api_url': 'http://frigate:5000'},
            headers={'X-CamAdmiral-Action': 'add-frigate-target'})
        target = request_json('/internal/frigate-targets')['targets'][0]
        effective = frigate('/config'); paths = frigate('/config/raw_paths')
        effective_paths = effective['cameras']['direct_authenticated']['ffmpeg']['inputs']
        raw_paths = paths['cameras']['direct_authenticated']['ffmpeg']['inputs']
        assert all('synthetic' not in i['path'] for i in effective_paths), 'Fixture must expose real API masking'
        assert all('synthetic' in i['path'] for i in raw_paths), 'Admin paths must retain actual credentials'
        state = {'target_id': target['target_id'], 'saved': frigate('/config/raw'),
                 'evidence': {'authenticated_fixture_verified': True, 'api_masking_verified': True}}
        save(state)
    else:
        state = json.loads(STATE.read_text())
    base = '/internal/frigate-targets/' + state['target_id']

    def import_one(key):
        preview = request_json(base + '/import')
        assert 'synthetic-camera-secret' not in json.dumps(preview) and 'rtsp://' not in json.dumps(preview)
        row = next(r for r in preview['cameras'] if r['key'] == key)
        assert row['can_import'], row
        request_json(base + '/import/' + preview['id'], method='POST', expected=202,
            headers={'X-CamAdmiral-Action': 'import-frigate-cameras'},
            payload={'selected': [key], 'allow_restart': True})
        job = wait_for('direct-input import completion', lambda:
            (j if (j := request_json(base + '/import/' + preview['id']))['status'] == 'completed' else False), timeout=240)
        assert 'synthetic-camera-secret' not in json.dumps(job) and 'rtsp://' not in json.dumps(job)
        return preview['id'], next(r for r in job['cameras'] if r['key'] == key)

    if stage in {'prepare', 'negative'}:
        _, denied = import_one('denied_camera')
        assert denied['status'] == 'error' and denied['reason'] == 'source_auth_failed', denied
        assert denied['configuration_status'] == 'unchanged', denied
        assert frigate('/config/raw') == state['saved'], 'Wrong credentials changed saved configuration'
        assert not consumer_directory()['cameras'], 'Wrong credentials adopted an invalid source'
        state['evidence']['wrong_credentials_rejected_without_changes'] = True
        save(state)
    elif stage == 'legacy-failure':
        jid, row = import_one('direct_authenticated')
        assert row['status'] == 'error' and row['reason'] == 'source_auth_failed', row
        assert frigate('/config/raw') == state['saved'], 'Baseline failure changed configuration'
        assert not consumer_directory()['cameras'], 'Baseline failure adopted a camera'
        state['legacy_job_id'] = jid
        state['evidence']['baseline_failure'] = row
        save(state)
    elif stage == 'takeover':
        if state.get('legacy_job_id'):
            jid = state['legacy_job_id']
            request_json(base + '/import/' + jid, method='POST', expected=202,
                headers={'X-CamAdmiral-Action': 'import-frigate-cameras'},
                payload={'selected': ['direct_authenticated'], 'allow_restart': True})
            job = wait_for('persisted failed-job retry', lambda:
                (j if (j := request_json(base + '/import/' + jid))['status'] == 'completed' else False), timeout=240)
            row = next(r for r in job['cameras'] if r['key'] == 'direct_authenticated')
            state['evidence']['retried_baseline_job_without_new_preview'] = jid == state['legacy_job_id']
        else:
            jid, row = import_one('direct_authenticated')
        assert 'synthetic-camera-secret' not in json.dumps(row) and 'rtsp://' not in json.dumps(row)
        state['job_id'] = jid
        state['evidence']['import_result'] = row
        state['evidence']['failed_import_preserved_configuration'] = frigate('/config/raw') == state['saved']
        save(state)
        assert row['status'] == 'applied', 'DIRECT_AUTH_IMPORT_FAILED: ' + json.dumps(row)
        original = yaml.safe_load(state['saved']); current = yaml.safe_load(frigate('/config/raw'))
        assert set(current['cameras']) == set(original['cameras'])
        for key in ('untouched_camera', 'denied_camera'):
            assert current['cameras'][key] == original['cameras'][key], 'Unselected camera changed'
        assert current['camera_groups'] == original['camera_groups']
        old = copy.deepcopy(original['cameras']['direct_authenticated'])
        new = copy.deepcopy(current['cameras']['direct_authenticated'])
        old['ffmpeg'].pop('inputs'); new['ffmpeg'].pop('inputs'); new.pop('live', None)
        assert new == old, 'Non-source operator settings changed'
        camera = next(c for c in consumer_directory()['cameras'] if c['id'] == row['camera_uuid'])
        assert len(camera['streams']) == 2
        assert {r for s in camera['streams'] for r in s['roles']} == {'record', 'detect'}
        state['camera_id'] = camera['id']; state['evidence']['settings_and_roles_preserved'] = True
        state['after_import'] = time.time(); save(state)
    elif stage == 'playback':
        rows = json.loads(Path('/state/direct-auth-recordings.json').read_text())
        before = state['baseline_recording']
        fresh = recording_for_playback(rows, 'direct_authenticated', since=state['after_import'])
        assert fresh, 'No complete fresh recording after authenticated import'
        state['evidence']['old_playback'] = play(recording_playback_url(before, rows), 'direct-auth-old', [1280, 720])
        state['evidence']['fresh_playback'] = play(recording_playback_url(fresh, rows), 'direct-auth-fresh', [1280, 720])
        prior = directory_signature(consumer_directory())
        request_json(base + '/import/' + state['job_id'], method='POST', expected=202,
            payload={'selected': ['direct_authenticated'], 'allow_restart': True},
            headers={'X-CamAdmiral-Action': 'import-frigate-cameras'})
        wait_for('repeat import completion', lambda: request_json(base + '/import/' + state['job_id'])['status'] == 'completed')
        assert directory_signature(consumer_directory()) == prior, 'Repeated import changed stable identities or URLs'
        state['evidence']['repeat_import_preserved_identity'] = True
        save(state)
    elif stage == 'pending-runtime':
        pending = copy.deepcopy(yaml.safe_load(state['saved'])['cameras']['direct_authenticated'])
        pending['friendly_name'] = 'Synthetic pending camera'
        for inp in pending['ffmpeg']['inputs']:
            inp['path'] += '?case=synthetic-pending'
        payload = {'requires_restart': 1, 'config_data': {'cameras': {'pending_camera': pending}}}
        req = urllib.request.Request('http://frigate:5000/api/config/set', data=json.dumps(payload).encode(),
                                     headers={'Content-Type': 'application/json'}, method='PUT')
        assert json.load(urllib.request.urlopen(req, timeout=15))['success']
        assert 'pending_camera' not in frigate('/config/raw_paths')['cameras']
        assert yaml.safe_load(frigate('/config/raw'))['cameras']['pending_camera'] == pending
        preview = request_json(base + '/import')
        row = next(r for r in preview['cameras'] if r['key'] == 'pending_camera')
        assert row['can_import'] and row['category'] == 'new', row
        assert 'synthetic-camera-secret' not in json.dumps(preview) and 'rtsp://' not in json.dumps(preview)
        state['evidence']['saved_camera_before_runtime_restart_supported'] = True
        save(state)
    else:
        raise ValueError(stage)


def host():
    from frigate_identity import recording_for_playback
    version = os.environ.get('FRIGATE_TEST_VERSION', '0.17.2')
    assert version in {'0.17.2', '0.18.2'}, 'Unsupported test dependency'
    env = dict(os.environ, FRIGATE_TEST_IMAGE='ghcr.io/blakeblackshear/frigate:' + version)
    artifacts = ROOT / 'e2e-artifacts' / 'frigate-direct-auth' / version
    artifacts.mkdir(parents=True, exist_ok=True)
    compose = ['docker', 'compose', '-p', 'camadmiral-direct-auth-' + version.replace('.', '-'),
               '-f', str(ROOT / 'e2e/compose.yaml'), '-f', str(ROOT / 'e2e/frigate-direct-auth-compose.yaml')]
    result = {'status': 'failed', 'frigate_version': version, 'baseline_source': BASELINE}
    for field, ref in [('revision', 'HEAD'), ('application_tree', 'HEAD:camadmiral'),
                       ('baseline_application_tree', BASELINE + ':camadmiral')]:
        result[field] = subprocess.check_output(['git', 'rev-parse', ref], cwd=ROOT, text=True).strip()

    def run(*args, check=True):
        p = subprocess.run([*compose, *args], cwd=ROOT, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        with (artifacts / 'transcript.log').open('a') as log: log.write(p.stdout)
        if check and p.returncode:
            print(p.stdout[-6000:], flush=True); p.check_returncode()
        return p.stdout.strip()

    def stage(name):
        print('Direct-auth stage: ' + name, flush=True)
        run('run', '--rm', '--entrypoint', 'python', 'test-driver', '/e2e/frigate_direct_auth.py', name)

    def recordings():
        code = "import sqlite3,json;from pathlib import Path;db=sqlite3.connect('file:/config/frigate.db?mode=ro',uri=True);print(json.dumps([list(r) for r in db.execute('select id,camera,path,start_time,end_time from recordings order by start_time') if Path(r[2]).is_file() and r[4]-r[3]>=2]))"
        return json.loads(run('exec', '-T', 'frigate', 'python3', '-c', code))

    history_read = "import sqlite3,json;db=sqlite3.connect('file:/config/frigate.db?mode=ro',uri=True);print(json.dumps({'events':db.execute(\"select * from event where id='synthetic-direct-auth-event'\").fetchall(),'reviews':db.execute(\"select * from reviewsegment where id='synthetic-direct-auth-review'\").fetchall()}))"
    try:
        run('down', '--volumes', '--remove-orphans')
        run('build', 'camadmiral')
        if result['application_tree'] != result['baseline_application_tree']:
            with tempfile.TemporaryDirectory(prefix='direct-auth-baseline-') as temp:
                archive = subprocess.Popen(['git', 'archive', BASELINE], cwd=ROOT, stdout=subprocess.PIPE)
                subprocess.run(['tar', '-x', '-C', temp], stdin=archive.stdout, check=True)
                archive.stdout.close()
                assert archive.wait() == 0
                subprocess.run(['docker', 'build', '--build-arg', 'CHECKPOINT_REVISION=e2e',
                                '-t', 'camadmiral-direct-auth:baseline', temp], check=True)
            env['DIRECT_AUTH_APP_IMAGE'] = 'camadmiral-direct-auth:baseline'
        run('up', '-d', 'camadmiral', 'camera-open', 'camera-auth', 'frigate')
        result['images'] = json.loads(subprocess.check_output(['docker', 'image', 'inspect',
            'camadmiral-e2e:local', env['FRIGATE_TEST_IMAGE']], text=True))
        # Retain immutable image identity without unrelated image metadata.
        result['images'] = [{k: image.get(k) for k in ('Id', 'RepoDigests')} for image in result['images']]
        stage('prepare')
        deadline = time.monotonic() + 180
        before = []
        while time.monotonic() < deadline:
            before = recordings()
            if len([r for r in before if r[1] == 'direct_authenticated']) >= 2: break
            time.sleep(3)
        selected = recording_for_playback(before, 'direct_authenticated')
        assert selected, 'Fixture must record real authenticated video before import'
        seed = """import json,sqlite3,time,urllib.request
from pathlib import Path
thumb=Path('/media/frigate/clips/synthetic-direct-review.jpg');thumb.parent.mkdir(parents=True,exist_ok=True)
thumb.write_bytes(urllib.request.urlopen('http://127.0.0.1:5000/api/direct_authenticated/latest.jpg').read())
stamp=time.time()-20
with sqlite3.connect('/config/frigate.db') as db:
 event={'id':'synthetic-direct-auth-event','camera':'direct_authenticated','label':'person','start_time':stamp,'end_time':stamp+10,'top_score':0.9,'score':0.9,'false_positive':0,'zones':'[]','thumbnail':'','has_clip':1,'has_snapshot':0,'region':'[]','box':'[]','area':100,'retain_indefinitely':1,'ratio':1,'plus_id':'','model_hash':'synthetic','detector_type':'cpu','model_type':'synthetic','data':'{}'}
 columns={r[1] for r in db.execute('pragma table_info(event)')};event={k:v for k,v in event.items() if k in columns}
 db.execute('insert into event ('+','.join(event)+') values ('+','.join('?' for _ in event)+')',list(event.values()))
 db.execute('insert into reviewsegment(id,camera,start_time,end_time,severity,thumb_path,data) values (?,?,?,?,?,?,?)',('synthetic-direct-auth-review','direct_authenticated',stamp,stamp+10,'alert','/media/frigate/clips/synthetic-direct-review.jpg',json.dumps({'detections':['synthetic-direct-auth-event'],'objects':['person'],'zones':[]})))
 db.commit()
"""
        run('exec', '-T', 'frigate', 'python3', '-c', seed)
        history = json.loads(run('exec', '-T', 'frigate', 'python3', '-c', history_read))
        assert history['events'] and history['reviews']
        result['baseline_recordings'] = before; result['history_before'] = history
        update = "import json;from pathlib import Path;p=Path('/state/direct-auth-state.json');s=json.loads(p.read_text());s['baseline_recording']=" + repr(selected) + ";p.write_text(json.dumps(s))"
        run('run', '--rm', '--entrypoint', 'python', 'test-driver', '-c', update)
        if env.get('DIRECT_AUTH_APP_IMAGE'):
            stage('legacy-failure')
            assert json.loads(run('exec', '-T', 'frigate', 'python3', '-c', history_read)) == history
            result['baseline_failure_reproduced'] = True
            result['baseline_image'] = json.loads(subprocess.check_output(
                ['docker', 'image', 'inspect', 'camadmiral-direct-auth:baseline', '--format', '{{json .Id}}'], text=True))
            env['DIRECT_AUTH_APP_IMAGE'] = 'camadmiral-e2e:local'
            run('up', '-d', '--no-deps', '--force-recreate', 'camadmiral')
            stage('negative')
        stage('takeover')
        after_import = time.time()
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            after = recordings()
            fresh = recording_for_playback(after, 'direct_authenticated', since=after_import)
            if fresh: break
            time.sleep(3)
        else: raise AssertionError('No complete fresh recording after import')
        (artifacts / 'recordings.json').write_text(json.dumps(after, indent=2))
        # The driver volume is shared, while the application root remains read-only.
        run('run', '--rm', '--entrypoint', 'python', 'test-driver', '-c',
            "import json;from pathlib import Path;Path('/state/direct-auth-recordings.json').write_text(" + repr(json.dumps(after)) + ")")
        stage('playback')
        stage('pending-runtime')
        assert json.loads(run('exec', '-T', 'frigate', 'python3', '-c', history_read)) == history, 'Historical events or reviews changed'
        current = recordings()
        assert {r[0] for r in before} <= {r[0] for r in current}, 'Historical recordings disappeared'
        result['history_preserved'] = True
        run('create', 'test-driver')
        for name in ('direct-auth-old.mp4', 'direct-auth-fresh.mp4'):
            run('cp', 'test-driver:/state/' + name, str(artifacts / name))
        result['status'] = 'passed'
    finally:
        run('create', 'test-driver', check=False)
        run('cp', 'test-driver:' + str(STATE), str(artifacts / 'driver.json'), check=False)
        if (artifacts / 'driver.json').exists():
            result['driver'] = json.loads((artifacts / 'driver.json').read_text())
        (artifacts / 'result.json').write_text(json.dumps(result, indent=2))
        (artifacts / 'containers.log').write_text(run('logs', '--no-color', check=False))
        run('down', '--volumes', '--remove-orphans', check=False)


if __name__ == '__main__':
    if len(sys.argv) > 1: driver(sys.argv[1])
    else: host()

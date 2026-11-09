"""Slow restart and durable restoration against real Frigate and synthetic video."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
STATE = Path('/state/restart-recovery.json')


def driver(stage):
    import yaml
    from scenarios import request_json, wait_for, wait_for_health, consumer_directory
    from frigate_identity import recording_for_playback, recording_playback_url

    def frigate(path):
        return json.load(urllib.request.urlopen('http://frigate:5000/api' + path, timeout=15))

    def fault(options=None):
        req = urllib.request.Request('http://import-api-proxy:20017/__fault__/' + ('arm' if options is not None else 'status'),
            data=json.dumps(options).encode() if options is not None else None,
            method='POST' if options is not None else 'GET')
        return json.load(urllib.request.urlopen(req, timeout=10))

    def remember():
        STATE.write_text(json.dumps(state))

    def save_config(data):
        req = urllib.request.Request('http://frigate:5000/api/config/set',
            data=json.dumps({'requires_restart': 1, 'config_data': data}).encode(),
            headers={'Content-Type': 'application/json'}, method='PUT')
        assert json.load(urllib.request.urlopen(req, timeout=15))['success']

    def import_camera(key):
        preview = request_json(base + '/import')
        row = next(r for r in preview['cameras'] if r['key'] == key)
        assert row['can_import'], row
        request_json(base + '/import/' + preview['id'], method='POST', expected=202,
            headers={'X-CamAdmiral-Action': 'import-frigate-cameras'},
            payload={'selected': [key], 'allow_restart': True})
        job = wait_for('synthetic handoff completion', lambda:
            (j if (j := request_json(base + '/import/' + preview['id']))['status'] == 'completed' else False), timeout=480)
        row = next(r for r in job['cameras'] if r['key'] == key)
        state['results'][stage] = {'row': row, 'fault': fault()}; remember()
        return row

    def restore_camera(key, **kwargs):
        return request_json(base + '/import-rollback', method='POST',
            headers={'X-CamAdmiral-Action': 'rollback-frigate-import'},
            payload={'camera_key': key, 'allow_restart': True}, **kwargs)

    def original(key):
        saved = yaml.safe_load(state['saved'])
        actual = yaml.safe_load(frigate('/config/raw'))
        assert actual['cameras'][key] == saved['cameras'][key], 'Original camera settings changed'
        alias = saved['cameras'][key]['live']['streams']['Main']
        assert actual['go2rtc']['streams'][alias] == saved['go2rtc']['streams'][alias], 'Original source was not restored'
        return frigate('/stats')['cameras'].get(key, {}).get('camera_fps', 0) > 0

    wait_for_health()
    wait_for('real Frigate ready', lambda: frigate('/stats'), timeout=180)
    if stage == 'prepare':
        request_json('/internal/frigate-targets', method='POST', expected=201,
            headers={'X-CamAdmiral-Action': 'add-frigate-target'},
            payload={'name': 'Synthetic restart target', 'api_url': 'http://import-api-proxy:20017'})
        state = {'target_id': request_json('/internal/frigate-targets')['targets'][0]['target_id'],
                 'saved': frigate('/config/raw'), 'results': {}}
        remember()
        for key in ('slow_camera', 'failure_camera', 'interrupted_camera', 'untouched_camera'):
            wait_for('original synthetic capture', lambda: frigate('/stats')['cameras'].get(key, {}).get('camera_fps', 0) > 0, timeout=180)
        for host in ('import-limited', 'import-failure', 'import-interrupted'):
            checked = subprocess.run(['ffprobe', '-v', 'error', '-rtsp_transport', 'tcp',
                '-show_entries', 'stream=codec_type', '-of', 'json', 'rtsp://' + host + ':8554/main'], capture_output=True, timeout=20)
            assert checked.returncode != 0, 'Fixture must occupy the only physical camera slot'
        return
    state = json.loads(STATE.read_text())
    base = '/internal/frigate-targets/' + state['target_id']
    if stage == 'slow-import':
        fault({'mode': 'ack', 'restart_delays': [110]})
        row = import_camera('slow_camera')
        assert row['status'] == 'applied', 'SLOW_RESTART_HANDOFF_FAILED: ' + json.dumps(row)
        assert fault()['unavailable_reads'] > 0
        state['results'][stage]['completed_at'] = time.time(); remember()
    elif stage == 'slow-restore':
        # A real saved operator edit must remain protected even with recovery enabled.
        configured = yaml.safe_load(frigate('/config/raw'))
        old_fps = configured['cameras']['slow_camera']['detect']['fps']
        save_config({'cameras': {'slow_camera': {'detect': {'fps': old_fps + 1}}}})
        edited = frigate('/config/raw')
        denied = restore_camera('slow_camera', expected=409)
        assert denied['status'] == 'rollback_configuration_conflict', denied
        assert frigate('/config/raw') == edited, 'Recovery overwrote an operator edit'
        save_config({'cameras': {'slow_camera': {'detect': {'fps': old_fps}}}})
        fault({'mode': 'ack', 'restart_delays': [110]})
        restored = restore_camera('slow_camera', timeout=360)
        assert restored['status'] == 'restored', restored
        assert original('slow_camera')
        state['results'][stage] = {'operator_conflict_preserved': True, 'fault': fault(), 'completed_at': time.time()}; remember()
    elif stage == 'failed-restore':
        urllib.request.urlopen('http://import-failure:8080/fail-available', timeout=10).read()
        fault({'mode': 'ack', 'restart_delays': [], 'restore_outage_seconds': 12})
        row = import_camera('failure_camera')
        assert row['status'] == 'error' and row['reason'] == 'source_unavailable', row
        assert row['configuration_status'] == 'restored', row
        assert original('failure_camera')
        assert fault()['restore_outages'] == 1 and fault()['unavailable_reads'] > 1
        assert len(consumer_directory()['cameras']) == 1, 'Failed validation adopted a new camera'
        state['results'][stage]['completed_at'] = time.time(); remember()
    elif stage == 'interrupt-prepare':
        fault({'mode': 'ack', 'restart_delays': []})
        row = import_camera('interrupted_camera')
        assert row['status'] == 'applied', row
        fault({'mode': 'ack', 'restart_delays': [110]})
        try:
            restore_camera('interrupted_camera', timeout=2)
        except (TimeoutError, socket.timeout):
            pass
        else:
            raise AssertionError('Slow restoration unexpectedly completed before interruption')
        wait_for('restoration restart accepted', lambda: fault()['restart_requests'] == 1 and fault()['unavailable_reads'] > 0, timeout=30)
    elif stage == 'interrupt-verify':
        interrupted = fault()
        assert interrupted['api_unavailable'] and interrupted['restart_requests'] == 1, 'The fault proxy must survive the application interruption'
        wait_for('startup resumes original video', lambda: original('interrupted_camera'), timeout=240)
        def cleared():
            current = request_json(base + '/import')
            row = next(r for r in current['cameras'] if r['key'] == 'interrupted_camera')
            return not row.get('can_restore')
        wait_for('durable recovery journal cleared', cleared, timeout=240)
        assert fault()['restart_requests'] == 1, 'Recovery issued a redundant whole-instance restart'
        state['results'][stage] = {'fault': fault(), 'completed_at': time.time()}; remember()
    elif stage == 'playback':
        rows = json.loads(Path('/state/restart-recordings.json').read_text())
        before = json.loads(Path('/state/restart-before.json').read_text())
        evidence = {}
        for key in ('slow_camera', 'failure_camera', 'interrupted_camera', 'untouched_camera'):
            assert {r[0] for r in before if r[1] == key} <= {r[0] for r in rows if r[1] == key}, 'Historical recordings disappeared'
            for label, recording in [('old', recording_for_playback(before, key)),
                                     ('fresh', recording_for_playback(rows, key, since=state['playback_since']))]:
                assert recording, 'Missing complete ' + label + ' recording for ' + key
                destination = Path('/state/' + key + '-' + label + '.mp4')
                destination.write_bytes(urllib.request.urlopen(recording_playback_url(recording, rows), timeout=30).read())
                decoded = subprocess.run(['ffmpeg', '-v', 'error', '-threads', '1', '-i', str(destination),
                    '-an', '-frames:v', '15', '-f', 'framemd5', '-'], capture_output=True, timeout=30)
                count = sum(bool(l.strip()) and not l.startswith(b'#') for l in decoded.stdout.splitlines())
                assert decoded.returncode == 0 and count >= 15, 'Playback must decode actual video'
                evidence[key + '-' + label] = {'decoded_frames': count, 'bytes': destination.stat().st_size}
        assert yaml.safe_load(frigate('/config/raw')) == yaml.safe_load(state['saved']), 'Final saved operator configuration changed'
        state['results'][stage] = evidence; remember()
    else:
        raise ValueError(stage)


def host():
    from frigate_identity import recording_for_playback
    version = os.environ.get('FRIGATE_TEST_VERSION', '0.18.2')
    assert version in {'0.17.2', '0.18.2'}
    env = dict(os.environ, FRIGATE_TEST_IMAGE='ghcr.io/blakeblackshear/frigate:' + version)
    artifacts = ROOT / 'e2e-artifacts/frigate-restart-recovery' / version
    artifacts.mkdir(parents=True, exist_ok=True)
    compose = ['docker', 'compose', '-p', 'camadmiral-restart-' + version.replace('.', '-'),
        '-f', str(ROOT / 'e2e/compose.yaml'), '-f', str(ROOT / 'e2e/recording-compose.yaml'),
        '-f', str(ROOT / 'e2e/frigate-import-compose.yaml'), '-f', str(ROOT / 'e2e/frigate-restart-recovery-compose.yaml')]
    result = {'status': 'failed', 'frigate_version': version}
    for name, ref in [('revision', 'HEAD'), ('application_tree', 'HEAD:camadmiral')]:
        result[name] = subprocess.check_output(['git', 'rev-parse', ref], cwd=ROOT, text=True).strip()

    def run(*args, check=True):
        p = subprocess.run([*compose, *args], cwd=ROOT, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        with (artifacts / 'transcript.log').open('a') as log: log.write(p.stdout)
        if check and p.returncode:
            print(p.stdout[-6000:], flush=True); p.check_returncode()
        return p.stdout.strip()

    def stage(name):
        print('Restart regression stage: ' + name, flush=True)
        run('run', '--rm', '--entrypoint', 'python', 'test-driver', '/e2e/frigate_restart_recovery.py', name)

    def recordings():
        code = "import sqlite3,json;from pathlib import Path;db=sqlite3.connect('file:/config/frigate.db?mode=ro',uri=True);print(json.dumps([list(r) for r in db.execute('select id,camera,path,start_time,end_time from recordings order by start_time') if Path(r[2]).is_file() and r[4]-r[3]>=2]))"
        return json.loads(run('exec', '-T', 'frigate', 'python3', '-c', code))

    def shared_file(name, value):
        run('run', '--rm', '--entrypoint', 'python', 'test-driver', '-c',
            'from pathlib import Path;Path(' + repr('/state/' + name) + ').write_text(' + repr(json.dumps(value)) + ')')

    history_read = "import sqlite3,json;d=sqlite3.connect('file:/config/frigate.db?mode=ro',uri=True);print(json.dumps({'events':d.execute(\"select * from event where id='synthetic-restart-event'\").fetchall(),'reviews':d.execute(\"select * from reviewsegment where id='synthetic-restart-review'\").fetchall()}))"
    try:
        run('down', '--volumes', '--remove-orphans')
        run('build', 'camadmiral')
        run('up', '-d', 'camadmiral', 'camera-open', 'import-limited', 'import-failure', 'import-interrupted', 'frigate', 'import-api-proxy')
        images = json.loads(subprocess.check_output(['docker', 'image', 'inspect', 'camadmiral-e2e:local', env['FRIGATE_TEST_IMAGE']], text=True))
        result['images'] = [{k: im.get(k) for k in ('Id', 'RepoDigests')} for im in images]
        stage('prepare')
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            before = recordings()
            if all(recording_for_playback(before, key) for key in ('slow_camera', 'failure_camera', 'interrupted_camera', 'untouched_camera')): break
            time.sleep(3)
        else: raise AssertionError('All synthetic cameras must save actual video before migration')
        shared_file('restart-before.json', before)
        seed = """import json,sqlite3,time,urllib.request
from pathlib import Path
thumb=Path('/media/frigate/clips/synthetic-restart-review.jpg');thumb.parent.mkdir(parents=True,exist_ok=True)
thumb.write_bytes(urllib.request.urlopen('http://127.0.0.1:5000/api/slow_camera/latest.jpg').read())
stamp=time.time()-20
with sqlite3.connect('/config/frigate.db') as d:
 event={'id':'synthetic-restart-event','camera':'slow_camera','label':'person','start_time':stamp,'end_time':stamp+10,'top_score':0.9,'score':0.9,'false_positive':0,'zones':'[]','thumbnail':'','has_clip':1,'has_snapshot':0,'region':'[]','box':'[]','area':100,'retain_indefinitely':1,'ratio':1,'plus_id':'','model_hash':'synthetic','detector_type':'cpu','model_type':'synthetic','data':'{}'}
 columns={r[1] for r in d.execute('pragma table_info(event)')};event={k:v for k,v in event.items() if k in columns}
 d.execute('insert into event ('+','.join(event)+') values ('+','.join('?' for _ in event)+')',list(event.values()))
 d.execute('insert into reviewsegment(id,camera,start_time,end_time,severity,thumb_path,data) values (?,?,?,?,?,?,?)',('synthetic-restart-review','slow_camera',stamp,stamp+10,'alert',str(thumb),json.dumps({'detections':['synthetic-restart-event'],'objects':['person'],'zones':[]})))
 d.commit()
"""
        run('exec', '-T', 'frigate', 'python3', '-c', seed)
        history = json.loads(run('exec', '-T', 'frigate', 'python3', '-c', history_read))
        assert history['events'] and history['reviews']; result['history_before'] = history
        for name in ('slow-import', 'slow-restore', 'failed-restore', 'interrupt-prepare'):
            stage(name)
            assert json.loads(run('exec', '-T', 'frigate', 'python3', '-c', history_read)) == history, 'History changed during recovery'
        run('restart', 'camadmiral')
        stage('interrupt-verify')
        since = time.time()
        run('run', '--rm', '--entrypoint', 'python', 'test-driver', '-c',
            "import json;from pathlib import Path;p=Path('/state/restart-recovery.json');s=json.loads(p.read_text());s['playback_since']=" + repr(since) + ";p.write_text(json.dumps(s))")
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            rows = recordings()
            if all(recording_for_playback(rows, key, since=since) for key in ('slow_camera', 'failure_camera', 'interrupted_camera', 'untouched_camera')): break
            time.sleep(3)
        else: raise AssertionError('Every original camera must save fresh complete video after recovery')
        shared_file('restart-recordings.json', rows)
        stage('playback')
        assert json.loads(run('exec', '-T', 'frigate', 'python3', '-c', history_read)) == history
        result['history_preserved'] = True; result['status'] = 'passed'
    finally:
        run('create', 'test-driver', check=False)
        run('cp', 'test-driver:' + str(STATE), str(artifacts / 'driver.json'), check=False)
        if (artifacts / 'driver.json').exists(): result['driver'] = json.loads((artifacts / 'driver.json').read_text())
        (artifacts / 'result.json').write_text(json.dumps(result, indent=2))
        (artifacts / 'containers.log').write_text(run('logs', '--no-color', check=False))
        run('down', '--volumes', '--remove-orphans', check=False)


if __name__ == '__main__':
    if len(sys.argv) > 1: driver(sys.argv[1])
    else: host()

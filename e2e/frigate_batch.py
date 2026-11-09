"""Grouped takeover and generated-YAML recovery with real synthetic recordings."""
from __future__ import annotations
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
STATE = '/state/batch-import.json'


def driver(stage):
    import yaml
    from scenarios import request_json, wait_for, wait_for_health
    from frigate_restart_recovery import driver as prepare
    if stage == 'prepare':
        prepare('prepare')
        original = json.loads(Path('/state/restart-recovery.json').read_text())
        original['results'] = {}
        Path(STATE).write_text(json.dumps(original))
        return
    state = json.loads(Path(STATE).read_text())
    base = '/internal/frigate-targets/' + state['target_id']
    def remember(): Path(STATE).write_text(json.dumps(state))
    def frigate(path):
        return json.load(urllib.request.urlopen('http://frigate:5000/api' + path, timeout=15))
    def fault(options=None):
        if options is not None:
            options = {**options, 'runtime_controls': True}
        req = urllib.request.Request('http://import-api-proxy:20017/__fault__/' + ('arm' if options is not None else 'status'),
            data=json.dumps(options).encode() if options is not None else None,
            method='POST' if options is not None else 'GET')
        return json.load(urllib.request.urlopen(req, timeout=10))
    def start(keys, *, operator_edit=False):
        job = request_json(base + '/import')
        assert all(next(r for r in job['cameras'] if r['key'] == key)['can_import'] for key in keys)
        if operator_edit:
            payload = {'requires_restart': 1, 'config_data': {'cameras': {'failure_camera': {'detect': {'fps': 4}}}}}
            req = urllib.request.Request('http://frigate:5000/api/config/set', data=json.dumps(payload).encode(),
                headers={'Content-Type': 'application/json'}, method='PUT')
            assert json.load(urllib.request.urlopen(req, timeout=15))['success']
        request_json(base + '/import/' + job['id'], method='POST', expected=202,
            headers={'X-CamAdmiral-Action': 'import-frigate-cameras'},
            payload={'selected': keys, 'allow_restart': True})
        return job['id']
    def finished(job_id):
        return wait_for('grouped camera import completion', lambda:
            j if (j := request_json(base + '/import/' + job_id))['status'] == 'completed' else False, timeout=480)
    wait_for_health()
    if stage == 'batch-normal':
        fault({'mode': 'ack', 'restart_delays': []})
        job = finished(start(['slow_camera', 'interrupted_camera', 'failure_camera'], operator_edit=True))
        rows = {r['key']: r for r in job['cameras']}
        state['results'][stage] = {'rows': rows, 'fault': fault()}; remember()
        assert rows['slow_camera']['status'] == rows['interrupted_camera']['status'] == 'applied', rows
        assert rows['failure_camera']['status'] == 'error' and rows['failure_camera']['reason'] == 'configuration_changed_refresh_preview', rows
        raw = yaml.safe_load(frigate('/config/raw'))
        expected = yaml.safe_load(state['saved'])
        expected['cameras']['failure_camera']['detect']['fps'] = 4
        assert raw['cameras']['failure_camera'] == expected['cameras']['failure_camera'], 'Operator edit was overwritten'
        assert raw['cameras']['untouched_camera'] == expected['cameras']['untouched_camera'], 'Unselected camera changed'
        assert raw['camera_groups'] == expected['camera_groups'], 'Camera groups changed'
        assert fault()['restart_requests'] == 1, 'BATCH_RESTARTS_NOT_COMBINED: ' + json.dumps(fault())
    elif stage == 'batch-interrupt-start':
        fault({'mode': 'ack', 'restart_delays': [20]})
        state['interrupt_job'] = start(['failure_camera', 'untouched_camera'])
        wait_for('shared final restart accepted', lambda: fault()['restart_requests'] == 1 and fault()['api_unavailable'], timeout=180)
        job = request_json(base + '/import/' + state['interrupt_job'])
        state['interrupt_ids'] = {r['key']: r['camera_uuid'] for r in job['cameras'] if r['key'] in ['failure_camera', 'untouched_camera']}
        assert all(state['interrupt_ids'].values()), 'Camera identities must be journaled before restart'
        remember()
    elif stage == 'batch-interrupt-finish':
        job = finished(state['interrupt_job'])
        rows = {r['key']: r for r in job['cameras']}
        assert all(rows[k]['status'] == 'applied' and rows[k]['camera_uuid'] == value for k, value in state['interrupt_ids'].items()), rows
        assert fault()['restart_requests'] == 1, 'Recovery dispatched a second accepted restart'
        state['results'][stage] = {'rows': rows, 'fault': fault()}; remember()
    elif stage == 'recovery-import':
        job = finished(start(['slow_camera']))
        assert next(r for r in job['cameras'] if r['key'] == 'slow_camera')['status'] == 'applied', job
        state['recovery_saved'] = frigate('/config/raw'); remember()
    elif stage.startswith('recovery-arm-'):
        mode = stage.removeprefix('recovery-arm-')
        fault({'mode': 'ack', 'restart_delays': [], 'runtime_delete_fault': mode})
        state['recovery_before'] = frigate('/stats')['service']['uptime']; remember()
    elif stage.startswith('recovery-verify-'):
        expected_restarts = 1 if stage.endswith('before') else 0
        assert fault()['restart_requests'] == expected_restarts, fault()
        assert frigate('/config/raw') == state['recovery_saved'], 'Reader recovery changed saved configuration'
        state['results'][stage] = {'fault': fault()}; remember()
    elif stage == 'playback':
        from frigate_identity import recording_for_playback, recording_playback_url
        rows = json.loads(Path('/state/batch-recordings.json').read_text())
        before = json.loads(Path('/state/batch-before.json').read_text())
        decoded = []
        for key in ('slow_camera', 'failure_camera', 'interrupted_camera', 'untouched_camera'):
            for age, sample in [('historical', recording_for_playback(before, key)),
                                ('fresh', recording_for_playback(rows, key, since=state['fresh_since']))]:
                assert sample, 'A complete recording is missing: ' + key + ':' + age
                output = Path('/state/verified-playback.mp4')
                output.write_bytes(urllib.request.urlopen(recording_playback_url(sample, rows), timeout=30).read())
                p = subprocess.run(['ffmpeg', '-v', 'error', '-threads', '1', '-i', str(output),
                    '-an', '-frames:v', '15', '-f', 'framemd5', '-'], capture_output=True, timeout=30)
                frames = sum(bool(l.strip()) and not l.startswith(b'#') for l in p.stdout.splitlines())
                assert p.returncode == 0 and frames >= 15, 'Recording does not decode actual video'
                decoded.append({'camera': key, 'age': age, 'frames': frames})
        state['results']['playback'] = decoded; remember()
    else: raise ValueError(stage)


def host():
    from frigate_identity import recording_for_playback
    version = os.environ.get('FRIGATE_TEST_VERSION', '0.18.2')
    case = os.environ.get('FRIGATE_BATCH_CASE', 'batch')
    assert version in {'0.17.2', '0.18.2'} and case in {'batch', 'recovery'}
    env = dict(os.environ, FRIGATE_TEST_IMAGE='ghcr.io/blakeblackshear/frigate:' + version)
    artifacts = ROOT / 'e2e-artifacts/frigate-batch' / case / version
    artifacts.mkdir(parents=True, exist_ok=True)
    compose = ['docker', 'compose', '-p', 'camadmiral-batch-' + case + '-' + version.replace('.', '-'),
        '-f', str(ROOT / 'e2e/compose.yaml'), '-f', str(ROOT / 'e2e/recording-compose.yaml'),
        '-f', str(ROOT / 'e2e/frigate-import-compose.yaml'), '-f', str(ROOT / 'e2e/frigate-restart-recovery-compose.yaml')]
    result = {'status': 'failed', 'case': case, 'frigate_version': version}
    for key, ref in [('revision', 'HEAD'), ('application_tree', 'HEAD:camadmiral')]:
        result[key] = subprocess.check_output(['git', 'rev-parse', ref], cwd=ROOT, text=True).strip()
    def run(*args, check=True):
        p = subprocess.run([*compose, *args], cwd=ROOT, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        with (artifacts / 'transcript.log').open('a') as f: f.write(p.stdout)
        if check and p.returncode:
            print(p.stdout[-5000:], flush=True); p.check_returncode()
        return p.stdout.strip()
    def stage(name):
        print('Grouped import regression: ' + name, flush=True)
        run('run', '--rm', '--entrypoint', 'python', 'test-driver', '/e2e/frigate_batch.py', name)
    def recordings():
        code = "import sqlite3,json;from pathlib import Path;d=sqlite3.connect('file:/config/frigate.db?mode=ro',uri=True);print(json.dumps([list(r) for r in d.execute('select id,camera,path,start_time,end_time from recordings order by start_time') if Path(r[2]).is_file() and r[4]-r[3]>=2]))"
        return json.loads(run('exec', '-T', 'frigate', 'python3', '-c', code))
    def shared(name, value):
        run('run', '--rm', '--entrypoint', 'python', 'test-driver', '-c',
            'from pathlib import Path;Path(' + repr('/state/' + name) + ').write_text(' + repr(json.dumps(value)) + ')')
    def wait_recordings(since=0):
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            rows = recordings()
            if all(recording_for_playback(rows, k, since=since) for k in ('slow_camera', 'failure_camera', 'interrupted_camera', 'untouched_camera')): return rows
            time.sleep(3)
        raise AssertionError('Every original camera must save complete fresh video')
    def source_state():
        code = """import sys,json,sqlite3;sys.path.insert(0,'/opt/camadmiral')
from camadmiral.crypto import load_master_key,decrypt_password
from camadmiral.media import source_connection_revisions
d=sqlite3.connect('file:/var/lib/camadmiral/camadmiral.db?mode=ro',uri=True)
t,k,c,p=d.execute("select target_id,camera_key,camera_uuid,payload from frigate_import_links where camera_key='slow_camera'").fetchone()
r=json.loads(decrypt_password(p,t+':'+k,load_master_key()))
s=[{'stream_uuid':a,'stream_key':b} for a,b in d.execute('select stream_uuid,stream_key from managed_streams where camera_uuid=?',(c,))]
print(json.dumps({'saved':r.get('relay_source_revisions'),'live':source_connection_revisions(s),'pending':bool(r.get('capture_resume_pending') or r.get('source_recovery_pending'))}))"""
        return json.loads(run('exec', '-T', 'camadmiral', 'python', '-c', code))
    def reconnect_and_verify(mode):
        before = source_state(); result['source_before_' + mode] = before
        stage('recovery-arm-' + mode)
        run('restart', 'import-limited')
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            current = source_state()
            if current['live'] and current['saved'] == current['live'] and current['live'] != before['saved'] and not current['pending']: break
            time.sleep(3)
        else:
            result['source_after_' + mode] = current
            raise AssertionError('RUNTIME_ALIAS_RECOVERY_NOT_ACKNOWLEDGED: ' + json.dumps(current))
        result['source_after_' + mode] = current
        # The next ordinary reconciliation must not pause capture again.
        time.sleep(12)
        assert source_state() == current, 'Source recovery entered a refresh loop'
        stage('recovery-verify-' + mode)
    history_read = "import sqlite3,json;d=sqlite3.connect('file:/config/frigate.db?mode=ro',uri=True);print(json.dumps({'events':d.execute(\"select * from event where id='synthetic-batch-event'\").fetchall(),'reviews':d.execute(\"select * from reviewsegment where id='synthetic-batch-review'\").fetchall()}))"
    try:
        run('down', '--volumes', '--remove-orphans'); run('build', 'camadmiral')
        run('up', '-d', 'camadmiral', 'camera-open', 'import-limited', 'import-failure', 'import-interrupted', 'frigate', 'import-api-proxy')
        images = json.loads(subprocess.check_output(['docker', 'image', 'inspect', 'camadmiral-e2e:local', env['FRIGATE_TEST_IMAGE']], text=True))
        result['images'] = [{k: value.get(k) for k in ('Id', 'RepoDigests')} for value in images]
        stage('prepare'); before = wait_recordings(); shared('batch-before.json', before)
        seed = """import json,sqlite3,time,urllib.request
from pathlib import Path
p=Path('/media/frigate/clips/synthetic-batch-review.jpg');p.parent.mkdir(parents=True,exist_ok=True)
p.write_bytes(urllib.request.urlopen('http://127.0.0.1:5000/api/slow_camera/latest.jpg').read())
t=time.time()-20
with sqlite3.connect('/config/frigate.db') as d:
 e={'id':'synthetic-batch-event','camera':'slow_camera','label':'person','start_time':t,'end_time':t+10,'top_score':0.9,'score':0.9,'false_positive':0,'zones':'[]','thumbnail':'','has_clip':1,'has_snapshot':0,'region':'[]','box':'[]','area':100,'retain_indefinitely':1,'ratio':1,'plus_id':'','model_hash':'synthetic','detector_type':'cpu','model_type':'synthetic','data':'{}'}
 cols={r[1] for r in d.execute('pragma table_info(event)')};e={k:v for k,v in e.items() if k in cols}
 d.execute('insert into event ('+','.join(e)+') values ('+','.join('?' for _ in e)+')',list(e.values()))
 d.execute('insert into reviewsegment(id,camera,start_time,end_time,severity,thumb_path,data) values (?,?,?,?,?,?,?)',('synthetic-batch-review','slow_camera',t,t+10,'alert',str(p),json.dumps({'detections':['synthetic-batch-event'],'objects':['person'],'zones':[]})))
 d.commit()
"""
        run('exec', '-T', 'frigate', 'python3', '-c', seed)
        history = json.loads(run('exec', '-T', 'frigate', 'python3', '-c', history_read)); assert history['events'] and history['reviews']
        if case == 'batch':
            stage('batch-normal'); stage('batch-interrupt-start'); run('restart', 'camadmiral'); stage('batch-interrupt-finish')
        else:
            stage('recovery-import'); reconnect_and_verify('after'); reconnect_and_verify('before')
        since = time.time()
        run('run', '--rm', '--entrypoint', 'python', 'test-driver', '-c',
            "import json;from pathlib import Path;p=Path('" + STATE + "');s=json.loads(p.read_text());s['fresh_since']=" + repr(since) + ";p.write_text(json.dumps(s))")
        shared('batch-recordings.json', wait_recordings(since)); stage('playback')
        assert json.loads(run('exec', '-T', 'frigate', 'python3', '-c', history_read)) == history, 'Historical event/review metadata changed'
        result.update(status='passed', history_preserved=True)
    finally:
        run('create', 'test-driver', check=False); run('cp', 'test-driver:' + STATE, str(artifacts / 'driver.json'), check=False)
        if (artifacts / 'driver.json').exists(): result['driver'] = json.loads((artifacts / 'driver.json').read_text())
        (artifacts / 'result.json').write_text(json.dumps(result, indent=2))
        (artifacts / 'containers.log').write_text(run('logs', '--no-color', check=False))
        run('down', '--volumes', '--remove-orphans', check=False)


if __name__ == '__main__':
    if len(sys.argv) > 1: driver(sys.argv[1])
    else: host()

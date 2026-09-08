"""Real Frigate recording continuity across a CamAdmiral camera removal.

Run on a disposable Docker host: python3 e2e/recording_continuity.py
This deliberately asserts healthy behavior, so the affected Frigate build fails.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
STATE = Path('/state/recording-continuity.json')


def frigate(path):
    with urllib.request.urlopen('http://camadmiral:5000' + path, timeout=10) as response:
        return json.load(response)


def setup():
    from scenarios import (adopt_rtsp, camera_by_name, consumer_directory,
                           discovery_device, request_json, wait_for, wait_for_health)
    wait_for_health()
    wait_for('seeded camera', lambda: discovery_device('candidate-open'))
    adopt_rtsp('candidate-open', 'Synthetic removable camera', '', '', [
        {'label': 'Main', 'url': 'rtsp://172.30.0.10:8554/main'},
    ])
    camera = camera_by_name(consumer_directory(), 'Synthetic removable camera')
    wait_for('Frigate API', lambda: frigate('/api/stats'), timeout=180)
    request_json('/internal/frigate-targets', method='POST', expected=201,
                 payload={'name': 'Synthetic recording target', 'api_url': 'http://127.0.0.1:5000'},
                 headers={'X-CamAdmiral-Action': 'add-frigate-target'})
    target = request_json('/internal/frigate-targets')['targets'][0]
    route = f"/internal/frigate-targets/{target['target_id']}/cameras/{camera['id']}"
    selected = request_json(route, method='POST', timeout=120,
                            headers={'X-CamAdmiral-Action': 'sync-frigate-camera'})
    assert selected.get('selected') is True, selected
    key = 'camadmiral_' + re.sub(r'[^a-zA-Z0-9_]', '_', camera['id'])
    wait_for('both recording cameras in runtime',
             lambda: key in frigate('/api/config').get('cameras', {}), timeout=120)
    STATE.write_text(json.dumps({'route': route, 'removed_key': key}))
    print('SETUP: camera selected through CamAdmiral HTTP API', flush=True)


def remove():
    from scenarios import request_json
    state = json.loads(STATE.read_text())
    result = request_json(state['route'], method='DELETE', timeout=120,
                          headers={'X-CamAdmiral-Action': 'remove-frigate-camera'})
    assert result.get('selected') is False, result
    print('REMOVE: ' + json.dumps(result), flush=True)


def verify_retirement():
    from scenarios import request_json
    state = json.loads(STATE.read_text())
    key = state['removed_key']
    config = frigate('/api/config')['cameras'][key]
    assert config['enabled_in_config'] is False, config
    assert config['ui']['dashboard'] is False, config
    assert config['record']['enabled'] is False, config
    stats = frigate('/api/stats').get('cameras', {}).get(key, {})
    assert not any(stats.get(field) for field in ('pid', 'capture_pid', 'ffmpeg_pid', 'camera_fps', 'process_fps')), stats
    target_route = state['route'].split('/cameras/')[0]
    request_json(target_route + '/full-sync', method='POST', timeout=120,
                 headers={'X-CamAdmiral-Action': 'full-sync-frigate-target'})
    assert frigate('/api/config')['cameras'][key]['enabled'] is False
    print('PASS: removed camera hidden from live dashboard, no capture workers, retained after full sync', flush=True)


def reselect():
    from scenarios import request_json, wait_for
    state = json.loads(STATE.read_text())
    selected = request_json(state['route'], method='POST', timeout=120,
                            headers={'X-CamAdmiral-Action': 'sync-frigate-camera'})
    assert selected.get('selected') is True, selected
    key = state['removed_key']
    def active():
        config = frigate('/api/config')['cameras'][key]
        return (config['enabled_in_config'] and config['ui']['dashboard']
                and config['record']['enabled']
                and frigate('/api/stats').get('cameras', {}).get(key, {}).get('camera_fps', 0) > 0)
    wait_for('reselected camera capturing with recording restored', active, timeout=120)
    print('PASS: camera reselected with inherited recording and dashboard settings restored', flush=True)


def host():
    project = 'camadmiral-recording-e2e'
    artifacts = ROOT / 'e2e-artifacts' / 'recording-continuity'
    artifacts.mkdir(parents=True, exist_ok=True)
    compose = ['docker', 'compose', '-p', project,
               '-f', str(ROOT / 'e2e/compose.yaml'),
               '-f', str(ROOT / 'e2e/recording-compose.yaml')]

    def run(*args, capture=False):
        result = subprocess.run([*compose, *args], check=True, text=True,
                                stdout=subprocess.PIPE if capture else None)
        return result.stdout or ''

    def driver(action):
        run('run', '--rm', '--entrypoint', 'python', 'test-driver',
            '/e2e/recording_continuity.py', action)

    def inspect():
        # Read persistent output directly, independently of Frigate's live cache API.
        script = '''import json, sqlite3
from pathlib import Path
with sqlite3.connect('file:/config/frigate.db?mode=ro', uri=True) as db:
    rows = db.execute('SELECT camera, count(*), max(end_time) FROM recordings GROUP BY camera').fetchall()
    files = db.execute('SELECT path FROM recordings WHERE camera=? ORDER BY end_time DESC LIMIT 2', ('operator_camera',)).fetchall()
print(json.dumps({'recordings': rows, 'files_exist': bool(files) and all(Path(r[0]).is_file() and Path(r[0]).stat().st_size > 0 for r in files), 'cache': [p.name for p in Path('/tmp/cache').glob('*.mp4') if not p.name.startswith('preview_')]}))'''
        return json.loads(run('exec', '-T', 'frigate', 'python3', '-c', script, capture=True))

    def ready_baseline():
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            try:
                sample = inspect()
                if len(sample['recordings']) >= 2 and all(row[1] >= 2 for row in sample['recordings']) and sample['files_exist']:
                    return sample
            except (subprocess.CalledProcessError, ValueError):
                pass
            time.sleep(3)
        raise RuntimeError('SETUP FAILURE: both cameras must save real recordings before removal')

    failed = False
    try:
        run('build', 'camadmiral')
        run('up', '-d', 'camadmiral', 'camera-open', 'frigate', 'frigate-api-proxy')
        driver('setup')
        before = ready_baseline()
        print('BASELINE: ' + json.dumps(before), flush=True)
        driver('remove')
        run('restart', 'frigate')
        restarted_at = time.time()
        observations = []
        deadline = time.monotonic() + 120
        passed = False
        while time.monotonic() < deadline:
            time.sleep(5)
            sample = inspect()
            observations.append(sample)
            rows = [r for r in sample['recordings'] if r[0] == 'operator_camera']
            if rows and rows[0][2] > restarted_at + 20 and sample['files_exist']:
                passed = True
                break
        logs = run('logs', '--no-color', 'frigate', capture=True)
        stats = run('exec', '-T', 'frigate', 'python3', '-c',
                    "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:5000/api/stats').read().decode())", capture=True)
        evidence = {'baseline': before, 'observations': observations,
                    'stats': json.loads(stats), 'passed': passed,
                    'cache_error_count': logs.count('Error occurred when attempting to maintain recording cache')}
        (artifacts / 'evidence.json').write_text(json.dumps(evidence, indent=2))
        (artifacts / 'frigate.log').write_text(logs)
        print('FINAL: ' + json.dumps(observations[-1]), flush=True)
        print('Cache-maintenance errors: ' + str(evidence['cache_error_count']), flush=True)
        if not passed:
            raise AssertionError('CONTINUOUS RECORDING STOPPED: remaining camera saved no fresh recording after removal/restart')
        print('PASS: remaining camera continues saving recordings after removal/restart', flush=True)
        driver('verify-retirement')
        reselected_at = time.time()
        driver('reselect')
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            sample = inspect()
            if len(sample['recordings']) == 2 and all(r[2] > reselected_at + 10 for r in sample['recordings']):
                print('PASS: both cameras save new recordings after reselection', flush=True)
                break
            time.sleep(5)
        else:
            raise AssertionError('Reselected camera did not resume saved recordings')
    except Exception:
        failed = True
        raise
    finally:
        if failed and os.environ.get('CAMADMIRAL_E2E_KEEP') == '1':
            print('Keeping isolated lab: ' + project, flush=True)
        else:
            run('down', '--volumes', '--remove-orphans')


if __name__ == '__main__':
    if sys.argv[1:] == ['setup']:
        setup()
    elif sys.argv[1:] == ['remove']:
        remove()
    elif sys.argv[1:] == ['verify-retirement']:
        verify_retirement()
    elif sys.argv[1:] == ['reselect']:
        reselect()
    elif not sys.argv[1:]:
        host()
    else:
        raise SystemExit('usage: recording_continuity.py [setup|remove]')

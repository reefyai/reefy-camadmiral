"""Real delayed media and independent idle-profile health regression."""
import json
from pathlib import Path
import subprocess
import sys
import time
import urllib.request


def driver():
    from scenarios import availability, consumer_directory, discovery, request, request_json, wait_for, wait_for_health
    wait_for_health()
    wait_for('proxy', lambda: urllib.request.urlopen('http://stalled-camera:8080/', timeout=2).read())

    def control(path):
        return urllib.request.urlopen('http://stalled-camera:8080/' + path, timeout=5).read()

    def add(name, paths):
        request_json('/internal/cameras/rtsp', method='POST', expected=201,
                     payload={'display_name': name, 'sources': [
                         {'label': str(i), 'url': f'rtsp://stalled-camera:8554/{path}'}
                         for i, path in enumerate(paths)]},
                     headers={'X-CamAdmiral-Action': 'create-direct-rtsp-camera'})
        return next(c for c in consumer_directory()['cameras'] if c['name'] == name)

    camera = add('Synthetic dual profile', ['camera0', 'camera1'])
    other = add('Synthetic independent camera', ['camera2'])
    assert len({s['id'] for s in camera['streams'] if s['roles']}) == 2, 'Fixture needs separate bound profiles'

    def streams(camera):
        for device in discovery()['devices']:
            adoption = device.get('adoption') or {}
            if adoption.get('camera_uuid') == camera['id']:
                return [dict(s, roles=[role for role, uid in adoption['roles'].items()
                                      if uid == s['stream_uuid']]) for s in adoption['streams']]
        raise AssertionError('Adopted camera missing')

    def stable():
        now = {c['id']: c for c in consumer_directory()['cameras']}
        for original in (camera, other):
            assert [(s['id'], s['roles'], s['downstream']) for s in now[original['id']]['streams']] == [
                (s['id'], s['roles'], s['downstream']) for s in original['streams']]

    # Withhold all RTP for 18 seconds. The old five-second deadline fails.
    control('delay?seconds=18')
    start = time.monotonic()
    status, body, _ = request(f"/internal/cameras/{camera['id']}/snapshot.jpg", timeout=40)
    elapsed = time.monotonic() - start
    assert status == 200, ('delayed snapshot failed', status, elapsed)
    assert 15 < elapsed < 32, ('fixture did not exercise delayed media', elapsed)
    decoded = subprocess.run(['ffmpeg', '-v', 'error', '-threads', '1', '-i', 'pipe:0',
                              '-frames:v', '1', '-f', 'null', '-'], input=body,
                             capture_output=True, timeout=5)
    assert decoded.returncode == 0, 'Delayed JPEG did not decode'
    print(json.dumps({'delayed_frame_seconds': elapsed, 'jpeg_bytes': len(body)}), flush=True)
    control('resume')
    wait_for('both selected streams healthy', lambda: all(s['health_status'] == 'healthy' for s in streams(camera)))

    # No persistent viewers. Only the detect path stops; record must be sampled
    # independently, not inherit the detect failure or success.
    control('stall/camera1')
    observations = []
    other_before = streams(other)[0]['last_ready_at']

    def independent_failure():
        current = streams(camera)
        by_role = {role: s for s in current for role in s['roles']}
        point = {role: s['health_status'] for role, s in by_role.items()}
        observations.append(point)
        assert by_role['record']['health_status'] != 'offline', point
        assert all(s['health_status'] != 'offline' for s in streams(other)), 'Unrelated camera marked offline'
        return by_role['detect']['health_status'] == 'offline' and by_role['record']['health_status'] == 'healthy'

    wait_for('independent detect failure, healthy idle record', independent_failure, timeout=200, interval=2)
    assert streams(other)[0]['last_ready_at'] > other_before, 'Other camera health did not advance'
    assert availability(camera['id'])['buckets'][-1]['state'] != 'offline', 'One profile failure made the whole camera offline'
    print(json.dumps({'independent_failure': observations[-1]}), flush=True)
    stable()
    control('resume')
    wait_for('automatic recovery', lambda: all(s['health_status'] == 'healthy' for s in streams(camera)), timeout=100)
    stable()
    print('PASS: delayed frame decoded; profile failure isolated; automatic recovery and stable URLs', flush=True)


def host(driver_file='snapshot_health.py', artifact_name='snapshot-health'):
    root = Path(__file__).resolve().parents[1]
    artifacts = root / 'e2e-artifacts' / artifact_name
    artifacts.mkdir(parents=True, exist_ok=True)
    compose = ['docker', 'compose', '-p', 'camadmiral-' + artifact_name + '-e2e',
               '-f', str(root / 'e2e/compose.yaml'), '-f', str(root / 'e2e/stalled-snapshot-compose.yaml'),
               '-f', str(root / 'e2e/snapshot-health-compose.yaml')]

    def run(*args, check=True):
        p = subprocess.run([*compose, *args], text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        print(p.stdout, flush=True)
        with (artifacts / 'workload.log').open('a') as f:
            f.write(p.stdout)
        if check:
            p.check_returncode()
        return p.stdout

    try:
        run('build', 'camadmiral')
        run('up', '-d', 'camera-open', 'stalled-camera', 'camadmiral')
        run('run', '--rm', '--entrypoint', 'python', 'test-driver', '/e2e/' + driver_file, 'driver')
        run('exec', '-T', 'camadmiral', 'python', '/e2e/stalled_snapshot.py', 'measure')
    finally:
        (artifacts / 'container.log').write_text(run('logs', '--no-color', 'camadmiral', check=False))
        run('down', '--volumes', '--remove-orphans', check=False)


if __name__ == '__main__':
    driver() if sys.argv[1:] else host()

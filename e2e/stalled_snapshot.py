"""Exercise real timed-out snapshots and assert relay consumers are reclaimed."""
from concurrent.futures import ThreadPoolExecutor
import collections
import json
from pathlib import Path
import subprocess
import sys
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]


def driver(stage):
    from scenarios import consumer_directory, request, request_json, wait_for, wait_for_health
    state = Path('/state/stalled-camera.json')
    if stage == 'setup':
        wait_for_health()
        wait_for('fault proxy', lambda: urllib.request.urlopen('http://stalled-camera:8080/', timeout=2).read())
        request_json('/internal/cameras/rtsp', method='POST', expected=201,
                     payload={'display_name': 'Synthetic stalled camera', 'sources': [
                         {'label': 'Main', 'url': 'rtsp://stalled-camera:8554/main'}]},
                     headers={'X-CamAdmiral-Action': 'create-direct-rtsp-camera'})
        cameras = consumer_directory()['cameras']
        assert len(cameras) == 1
        state.write_text(json.dumps(cameras))
        stage = 'healthy'
    if stage in ('stall', 'resume'):
        print(urllib.request.urlopen(f'http://stalled-camera:8080/{stage}', timeout=5).read().decode())
        return
    camera = json.loads(state.read_text())[0]

    def snapshot(_=None):
        start = time.monotonic()
        status, body, _ = request(f"/internal/cameras/{camera['id']}/snapshot.jpg", timeout=15)
        elapsed = time.monotonic() - start
        if stage == 'burst':
            assert status == 503, (status, len(body))
            assert elapsed < 9, elapsed
        else:
            assert status == 200 and body.startswith(b'\xff\xd8\xff') and body.endswith(b'\xff\xd9'), status
        return {'status': status, 'elapsed': elapsed, 'bytes': len(body)}

    if stage == 'burst':
        with ThreadPoolExecutor(max_workers=4) as pool:
            print(json.dumps(list(pool.map(snapshot, range(4)))), flush=True)
    else:
        print(snapshot())
        current = consumer_directory()['cameras']
        assert [c['id'] for c in current] == [camera['id']], 'Camera identity changed'
        # The serialized public stream directory may contain dynamic health fields.
        def stable(streams):
            return [(s['id'], s['roles'], s['downstream']) for s in streams]
        assert stable(current[0]['streams']) == stable(camera['streams']), 'Stable downstream streams changed'


def measure():
    data = json.load(urllib.request.urlopen('http://127.0.0.1:1984/api/streams', timeout=3))
    consumers = [c for s in data.values() for c in (s.get('consumers') or [])]
    result = {'at': time.time(), 'consumers': dict(collections.Counter(c.get('format_name') for c in consumers))}
    result['keyframe_ids'] = sorted(c['id'] for c in consumers if c.get('format_name') == 'keyframe')
    result['ffmpeg'] = 0
    for p in Path('/proc').glob('[0-9]*'):
        try:
            name = (p / 'comm').read_text().strip()
            result['ffmpeg'] += name == 'ffmpeg'
            if name == 'go2rtc':
                status = dict(line.split(':', 1) for line in (p / 'status').read_text().splitlines() if ':' in line)
                result.update(go2rtc_pid=int(p.name), rss_kib=int(status['VmRSS'].split()[0]),
                              fds=len(list((p / 'fd').iterdir())))
        except FileNotFoundError:
            pass
    for name in ('memory.events', 'pids.events'):
        result[name] = dict(line.split() for line in (Path('/sys/fs/cgroup') / name).read_text().splitlines())
    result['memory_bytes'] = int(Path('/sys/fs/cgroup/memory.current').read_text())
    print(json.dumps(result), flush=True)


def host():
    artifacts = ROOT / 'e2e-artifacts' / 'stalled-snapshot'
    artifacts.mkdir(parents=True, exist_ok=True)
    compose = ['docker', 'compose', '-p', 'camadmiral-stalled-e2e', '-f', str(ROOT / 'e2e/compose.yaml'),
               '-f', str(ROOT / 'e2e/stalled-snapshot-compose.yaml')]

    def run(*args, check=True):
        result = subprocess.run([*compose, *args], text=True, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT)
        if check and result.returncode:
            print(result.stdout, flush=True)
            result.check_returncode()
        return result.stdout

    def stage(name):
        output = run('run', '--rm', '--entrypoint', 'python', 'test-driver', '/e2e/stalled_snapshot.py', name)
        print(name, output, flush=True)
        with (artifacts / 'workload.log').open('a') as f:
            f.write(name + '\n' + output)

    samples = []

    def sample():
        value = json.loads(run('exec', '-T', 'camadmiral', 'python', '/e2e/stalled_snapshot.py', 'measure'))
        samples.append(value)
        print(json.dumps(value), flush=True)
        return value

    try:
        print(run('build', 'camadmiral'), flush=True)
        print(run('up', '-d', 'camera-open', 'stalled-camera', 'camadmiral'), flush=True)
        stage('setup')
        baseline = sample()
        stage('stall')
        time.sleep(10)
        for _ in range(6):
            stage('burst')
            time.sleep(7)  # Longer than the application's five-second timeout.
            sample()
        final = samples[-1]
        assert len(final['keyframe_ids']) <= 1, f"Abandoned snapshot consumers retained: {len(final['keyframe_ids'])}"
        assert final['rss_kib'] - samples[2]['rss_kib'] < 16 * 1024, 'Stalled snapshots retain growing relay memory'
        assert final['ffmpeg'] <= 4, 'Snapshot subprocesses accumulated'
        for point in samples:
            assert point['go2rtc_pid'] == baseline['go2rtc_pid'], 'Relay restarted instead of reclaiming consumers'
            assert int(point['memory.events']['oom_kill']) == 0, 'Memory OOM'
            assert int(point['pids.events']['max']) == 0, 'PID exhaustion'
        stage('resume')
        time.sleep(15)
        stage('healthy')
        time.sleep(7)
        recovered = sample()
        assert not recovered['keyframe_ids'], 'Snapshot consumers remain after recovery'
        assert recovered['ffmpeg'] <= 1, 'Snapshot processes remain after recovery'
        assert recovered['go2rtc_pid'] == baseline['go2rtc_pid']
        print('PASS: stalled snapshots reclaimed; same stream resumes video without relay restart', flush=True)
    finally:
        (artifacts / 'samples.json').write_text(json.dumps(samples, indent=2))
        (artifacts / 'container.log').write_text(run('logs', '--no-color', 'camadmiral', check=False))
        print(run('down', '--volumes', '--remove-orphans', check=False), flush=True)


if __name__ == '__main__':
    if sys.argv[1:] == ['measure']:
        measure()
    elif sys.argv[1:]:
        driver(sys.argv[1])
    else:
        host()

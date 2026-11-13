"""Regression: abandoned offline-source requests must stay bounded.

Run the packaged relay in the isolated CI Docker lab. Exercise healthy clients,
three disconnected-client retry waves, actual recovery video, and a second
outage. Never signal or access a fleet relay.
"""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import re
import socket
import subprocess
import sys
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
COUNT = 120


def metrics():
    text = urllib.request.urlopen('http://relay:1984/api/stack', timeout=5).read().decode()
    total = re.search(r'Total: (\d+)', text)
    streams = json.load(urllib.request.urlopen('http://relay:1984/api/streams', timeout=5))
    return {'goroutines': int(total[1]), 'consumers': {key: len(value.get('consumers') or []) for key, value in streams.items()}}


def describe(stream, timeout):
    with socket.create_connection(('relay', 18554), timeout=3) as sock:
        sock.settimeout(timeout)
        sock.sendall(f'DESCRIBE rtsp://relay:18554/{stream} RTSP/1.0\r\nCSeq: 1\r\nAccept: application/sdp\r\nUser-Agent: Synthetic-Retention-Probe\r\n\r\n'.encode())
        try:
            return sock.recv(4096).split(b'\r\n', 1)[0].decode()
        except socket.timeout:
            return 'client_timeout'


def video(stream='healthy'):
    result = subprocess.run(['ffmpeg', '-v', 'error', '-threads', '1', '-rtsp_transport', 'tcp', '-timeout', '5000000', '-i', f'rtsp://relay:18554/{stream}', '-an', '-frames:v', '5', '-f', 'framemd5', '-'], capture_output=True, timeout=20, check=True)
    assert sum(bool(line) and not line.startswith(b'#') for line in result.stdout.splitlines()) == 5


def driver(phase):
    if phase == 'recover-video':
        video('unavailable')
        print(json.dumps({'recovered_decoded_frames': 5}), flush=True)
        return
    if phase == 'metrics':
        print(json.dumps(metrics()), flush=True)
        return
    video()
    stream, timeout = ('healthy', 5) if phase == 'healthy' else ('unavailable', 0.15)
    with ThreadPoolExecutor(max_workers=16) as pool:
        futures = []
        for _ in range(COUNT):
            futures.append(pool.submit(describe, stream, timeout))
            time.sleep(0.05)
        responses = [future.result() for future in futures]
    if phase == 'healthy':
        assert all(response.startswith('RTSP/1.0 200') for response in responses), responses
    else:
        assert not any(response.startswith('RTSP/1.0 200') for response in responses), responses
    time.sleep(10)
    video()
    result = metrics()
    result.update(phase=phase, clients=COUNT, timeouts=responses.count('client_timeout'), healthy_decoded_frames=5)
    print(json.dumps(result), flush=True)


def host():
    artifacts = ROOT / 'e2e-artifacts/rtsp-retention'
    artifacts.mkdir(parents=True, exist_ok=True)
    compose = ['docker', 'compose', '-p', 'camadmiral-rtsp-retention', '-f', str(ROOT / 'e2e/rtsp-retention-compose.yaml')]
    evidence = {'source': subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip(), 'upstream_source': 'AlexxIT/go2rtc@b5948cfb25404cc5cb37b166ecaa2dca20b11d4b', 'clients_per_phase': COUNT}
    def run(*args, check=True):
        result = subprocess.run([*compose, *args], capture_output=True, text=True)
        with (artifacts / 'workload.log').open('a') as stream: stream.write(result.stdout + result.stderr)
        if check and result.returncode: raise RuntimeError(result.stdout + result.stderr)
        return result
    def phase(name):
        print('RETENTION_PHASE ' + name, flush=True)
        result = run('run', '--rm', 'test-driver', 'driver', name)
        return json.loads(result.stdout.splitlines()[-1])
    def resources():
        code = "import json,os;from pathlib import Path;print(json.dumps({'fds':len(list(Path('/proc/1/fd').iterdir())),'memory_bytes':int(Path('/sys/fs/cgroup/memory.current').read_text()),'oom_events':Path('/sys/fs/cgroup/memory.events').read_text()}))"
        return json.loads(run('exec', '-T', 'relay', 'python', '-c', code).stdout)
    try:
        run('down', '--volumes', '--remove-orphans')
        run('build', 'relay')
        run('up', '-d', 'relay', 'camera-open')
        for attempt in range(30):
            try:
                phase('metrics')
                break
            except RuntimeError:
                if attempt == 29: raise
                time.sleep(1)
        evidence['relay_binary_sha256'] = run('exec', '-T', 'relay', 'sha256sum', '/usr/local/bin/go2rtc').stdout.split()[0]
        evidence['relay_config_sha256'] = run('exec', '-T', 'relay', 'sha256sum', '/e2e/relay.yaml').stdout.split()[0]
        evidence['healthy'] = phase('healthy')
        evidence['healthy_resources'] = resources()
        evidence['offline_waves'] = []
        for wave in range(3):
            sample = phase('unavailable')
            sample['resources'] = resources()
            sample['goroutine_delta'] = sample['goroutines'] - evidence['healthy']['goroutines']
            sample['fd_delta'] = sample['resources']['fds'] - evidence['healthy_resources']['fds']
            sample['wave'] = wave + 1
            evidence['offline_waves'].append(sample)
        evidence['bounded_resource_assertion_passed'] = all(
            sample['goroutine_delta'] < 32 and sample['fd_delta'] < 32
            for sample in evidence['offline_waves']
        )
        evidence['symptom_reproduced'] = any(
            sample['goroutine_delta'] >= COUNT // 2 and sample['fd_delta'] >= COUNT // 2
            for sample in evidence['offline_waves']
        )
        assert all(sample['consumers']['unavailable'] == 0 for sample in evidence['offline_waves'])
        run('up', '-d', 'camera-recovered')
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            current = phase('metrics')
            if current['goroutines'] < evidence['healthy']['goroutines'] + 32:
                evidence['recovered'] = current
                break
            time.sleep(1)
        assert 'recovered' in evidence, 'Abandoned queue did not drain after source recovery'
        evidence['recovered_resources'] = resources()
        assert evidence['recovered_resources']['fds'] < evidence['healthy_resources']['fds'] + 32, 'Descriptors did not drain after source recovery'
        deadline = time.monotonic() + 45
        while True:
            try:
                evidence['recovered_video'] = phase('recover-video')
                break
            except RuntimeError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(1)
        run('stop', 'camera-recovered')
        run('rm', '-f', 'camera-recovered')
        evidence['repeat_unavailable'] = phase('unavailable')
        evidence['repeat_resources'] = resources()
        evidence['bounded_resource_assertion_passed'] &= (
            evidence['repeat_unavailable']['goroutines'] < evidence['healthy']['goroutines'] + 32
            and evidence['repeat_resources']['fds'] < evidence['healthy_resources']['fds'] + 32
        )
        evidence['relay_container'] = json.loads(run('ps', '-a', '--format', 'json', 'relay').stdout)
        assert evidence['relay_container']['State'] == 'running', 'Relay stopped during retries'
        assert 'oom_kill 0' in evidence['repeat_resources']['oom_events']
        assert run('exec', '-T', 'relay', 'sha256sum', '/e2e/relay.yaml').stdout.split()[0] == evidence['relay_config_sha256'], 'Configuration changed during retry/recovery'
        evidence['scenario_complete'] = True
        print('RTSP_RETENTION_REGRESSION ' + json.dumps(evidence), flush=True)
        assert evidence['bounded_resource_assertion_passed'], 'Disconnected offline-source clients retained unbounded goroutines/descriptors'
    finally:
        (artifacts / 'evidence.json').write_text(json.dumps(evidence, indent=2))
        (artifacts / 'containers.log').write_text(run('logs', '--no-color', check=False).stdout)
        run('down', '--volumes', '--remove-orphans', check=False)


if __name__ == '__main__':
    driver(sys.argv[2]) if sys.argv[1:2] == ['driver'] else host()

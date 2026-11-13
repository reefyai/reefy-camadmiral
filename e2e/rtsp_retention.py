"""Diagnostic: stock relay retention after clients abandon an unavailable source.

All connections and media are synthetic inside the established isolated CI lab.
No fleet endpoints or camera credentials are used. This records baseline behavior,
not a fixed-release acceptance result. A bounded-resource assertion is recorded
separately so a subsequent fix can run the identical scenario.
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


def video():
    result = subprocess.run(['ffmpeg', '-v', 'error', '-threads', '1', '-rtsp_transport', 'tcp', '-timeout', '5000000', '-i', 'rtsp://relay:18554/healthy', '-an', '-frames:v', '5', '-f', 'framemd5', '-'], capture_output=True, timeout=20, check=True)
    assert sum(bool(line) and not line.startswith(b'#') for line in result.stdout.splitlines()) == 5


def driver(phase):
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
        assert responses.count('client_timeout') > COUNT // 2, responses
    time.sleep(10)
    video()
    result = metrics()
    result.update(phase=phase, clients=COUNT, timeouts=responses.count('client_timeout'), healthy_decoded_frames=5)
    print(json.dumps(result), flush=True)


def host():
    artifacts = ROOT / 'e2e-artifacts/rtsp-retention'
    artifacts.mkdir(parents=True, exist_ok=True)
    compose = ['docker', 'compose', '-p', 'camadmiral-retention-diagnostic', '-f', str(ROOT / 'e2e/rtsp-retention-compose.yaml')]
    evidence = {'source': subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip(), 'binary_source': 'AlexxIT/go2rtc@b5948cfb25404cc5cb37b166ecaa2dca20b11d4b', 'clients_per_phase': COUNT}
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
        evidence['healthy'] = phase('healthy')
        evidence['healthy_resources'] = resources()
        evidence['unavailable'] = phase('unavailable')
        evidence['unavailable_resources'] = resources()
        delta = evidence['unavailable']['goroutines'] - evidence['healthy']['goroutines']
        evidence['retained_goroutine_delta'] = delta
        fd_delta = evidence['unavailable_resources']['fds'] - evidence['healthy_resources']['fds']
        evidence['retained_fd_delta'] = fd_delta
        evidence['bounded_resource_assertion_passed'] = delta < 32 and fd_delta < 32
        evidence['symptom_reproduced'] = delta >= COUNT // 2 and fd_delta >= COUNT // 2
        assert evidence['symptom_reproduced'], 'Stock baseline did not reproduce retained requests'
        assert evidence['unavailable']['consumers']['unavailable'] == 0, 'Pending requests unexpectedly counted as active consumers'
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
        run('stop', 'camera-recovered')
        run('rm', '-f', 'camera-recovered')
        evidence['repeat_unavailable'] = phase('unavailable')
        # Disposable synthetic relay only: retain a complete Go stack dump.
        run('kill', '-s', 'SIGQUIT', 'relay')
        time.sleep(2)
        evidence['diagnostic_complete'] = True
        print('RETENTION_DIAGNOSTIC ' + json.dumps(evidence), flush=True)
    finally:
        (artifacts / 'evidence.json').write_text(json.dumps(evidence, indent=2))
        (artifacts / 'containers.log').write_text(run('logs', '--no-color', check=False).stdout)
        run('down', '--volumes', '--remove-orphans', check=False)


if __name__ == '__main__':
    driver(sys.argv[2]) if sys.argv[1:2] == ['driver'] else host()

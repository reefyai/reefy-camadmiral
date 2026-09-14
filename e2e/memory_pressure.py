"""Real HTTP snapshot workload, production cgroup envelope, no allocation mocks."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]


def workload():
    from scenarios import consumer_directory, request, request_json, wait_for_health
    wait_for_health()
    for index in range(8):
        request_json('/internal/cameras/rtsp', method='POST', expected=201,
                     payload={'display_name': f'Synthetic camera {index}', 'sources': [
                         {'label': 'Main', 'url': f'rtsp://camera-open:8554/camera{index}'}]},
                     headers={'X-CamAdmiral-Action': 'create-direct-rtsp-camera'})
    cameras = consumer_directory()['cameras']
    assert len(cameras) == 8
    def snapshot(camera):
        status, body, _ = request(f"/internal/cameras/{camera['id']}/snapshot.jpg", timeout=45)
        assert status == 200 and body.startswith(b'\xff\xd8\xff') and body.endswith(b'\xff\xd9'), status
        return len(body)
    cycles = int(os.environ.get('CAMADMIRAL_MEMORY_CYCLES', '12'))
    for cycle in range(cycles):
        with ThreadPoolExecutor(max_workers=8) as pool:
            sizes = list(pool.map(snapshot, cameras))
        print(json.dumps({'cycle': cycle, 'jpeg_bytes': sizes}), flush=True)
        # Observe repeated allocation/release, including background thumbnail rounds.
        time.sleep(10)
        print(json.dumps({'idle_cycle': cycle, 'at': time.monotonic()}), flush=True)
    print(f'PASS: {cycles * 8} decoded JPEG snapshots from eight adopted 1080p cameras', flush=True)


def host():
    artifacts = ROOT / 'e2e-artifacts' / 'memory-pressure'
    artifacts.mkdir(parents=True, exist_ok=True)
    compose = ['docker', 'compose', '-p', 'camadmiral-memory-e2e',
               '-f', str(ROOT / 'e2e/compose.yaml'), '-f', str(ROOT / 'e2e/memory-compose.yaml')]
    def run(*args, capture=False, check=True):
        result = subprocess.run([*compose, *args], check=check, text=True,
                                stdout=subprocess.PIPE if capture else None,
                                stderr=subprocess.STDOUT if capture else None)
        return result.stdout or ''
    samples = []
    stop = threading.Event()
    def sample():
        while not stop.is_set():
            raw = run('exec', '-T', 'camadmiral', 'cat',
                      '/sys/fs/cgroup/memory.current', '/sys/fs/cgroup/memory.events',
                      '/sys/fs/cgroup/pids.current', '/sys/fs/cgroup/pids.events',
                      capture=True, check=False)
            lines = raw.splitlines()
            if lines and lines[0].isdigit():
                split = next((i for i in range(1, len(lines)) if lines[i].isdigit()), len(lines))
                events = dict(line.split() for line in lines[1:split] if len(line.split()) == 2)
                pids_events = dict(line.split() for line in lines[split+1:] if len(line.split()) == 2)
                samples.append({'time': time.monotonic(), 'bytes': int(lines[0]),
                                'pids': int(lines[split]) if split < len(lines) else 0,
                                'pids_denied': int(pids_events.get('max', 0)),
                                'oom_kill': int(events.get('oom_kill', 0))})
            stop.wait(0.5)
    thread = None
    events_file = (artifacts / 'docker-events.jsonl').open('w')
    events_process = subprocess.Popen(
        ['docker', 'events', '--format', '{{json .}}', '--filter',
         'label=com.docker.compose.project=camadmiral-memory-e2e'], stdout=events_file)
    try:
        run('build', 'camadmiral')
        run('up', '-d', 'camera-open', 'camadmiral')
        thread = threading.Thread(target=sample)
        thread.start()
        with (artifacts / 'workload.log').open('w') as output:
            result = subprocess.run([*compose, 'run', '--rm', '--entrypoint', 'python',
                                     'test-driver', '/e2e/memory_pressure.py', 'workload'],
                                    stdout=output, stderr=subprocess.STDOUT)
        print((artifacts / 'workload.log').read_text(), flush=True)
        time.sleep(30)
        container = run('ps', '-q', 'camadmiral', capture=True).strip()
        state = json.loads(subprocess.check_output(['docker', 'inspect', container], text=True))[0]
        events_file.flush()
        events = [json.loads(line) for line in (artifacts / 'docker-events.jsonl').read_text().splitlines()]
        idle = []
        for line in (artifacts / 'workload.log').read_text().splitlines():
            if not line.startswith('{"idle_cycle":'):
                continue
            point = json.loads(line)
            window = [s['bytes'] for s in samples if point['at'] - 3 <= s['time'] <= point['at']]
            if window:
                idle.append(statistics.median(window))
        idle_growth = statistics.median(idle[-3:]) - statistics.median(idle[2:5]) if len(idle) >= 8 else None
        # Read the container's procfs; Docker hosts may use BusyBox ps without -eo.
        process_probe = "from pathlib import Path; print(sum(p.read_text().strip() == 'ffmpeg' for p in Path('/proc').glob('[0-9]*/comm') if p.exists()))"
        ffmpeg_count = int(run('exec', '-T', 'camadmiral', 'python', '-c', process_probe, capture=True))
        summary = {'limit_bytes': state['HostConfig']['Memory'],
                   'peak_sample_bytes': max((s['bytes'] for s in samples), default=0),
                   'max_oom_kills': max((s['oom_kill'] for s in samples), default=0),
                   'restarts': state['RestartCount'], 'workload_exit': result.returncode,
                   'oom_events': sum(event.get('Action') == 'oom' for event in events),
                   'idle_bytes': idle, 'idle_growth_bytes': idle_growth,
                   'final_ffmpeg_processes': ffmpeg_count,
                   'max_pids': max((s['pids'] for s in samples), default=0),
                   'pids_denied': max((s['pids_denied'] for s in samples), default=0),
                   'tail_median_bytes': statistics.median(s['bytes'] for s in samples[-30:])}
        (artifacts / 'summary.json').write_text(json.dumps(summary, indent=2))
        print(json.dumps(summary, indent=2), flush=True)
        assert result.returncode == 0, 'Real snapshot workload failed'
        assert summary['max_oom_kills'] == 0, 'Kernel killed media processes at the configured limit'
        assert summary['oom_events'] == 0, 'Docker reported a container OOM'
        assert summary['restarts'] == 0, 'CamAdmiral restarted during normal media work'
        assert summary['pids_denied'] == 0, 'Kernel refused media worker threads'
        assert idle_growth is not None and idle_growth < 32 * 1024 * 1024, 'Retained memory grew across repeated snapshot batches'
        assert summary['final_ffmpeg_processes'] == 0, 'Snapshot decoder processes did not exit after the workload'
    finally:
        stop.set()
        if thread:
            thread.join(timeout=15)
        (artifacts / 'samples.json').write_text(json.dumps(samples))
        (artifacts / 'container.log').write_text(run('logs', '--no-color', 'camadmiral', capture=True, check=False))
        run('down', '--volumes', '--remove-orphans', check=False)
        events_process.terminate()
        events_process.wait(timeout=10)
        events_file.close()


if __name__ == '__main__':
    workload() if sys.argv[1:] == ['workload'] else host()

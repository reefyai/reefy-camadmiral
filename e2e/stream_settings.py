"""Browser stream selection, real upstream connection isolation and Frigate video."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.request
import urllib.parse


def driver(stage):
    from scenarios import consumer_directory, discovery, request_json, wait_for, wait_for_health
    wait_for_health()
    state_path = Path('/state/stream-settings.json')

    def adoption(cid):
        return next(d['adoption'] for d in discovery()['devices']
                    if (d.get('adoption') or {}).get('camera_uuid') == cid)

    def stats():
        return json.load(urllib.request.urlopen('http://stalled-camera:8080/stream-stats', timeout=5))

    if stage == 'prepare':
        request_json('/internal/cameras/rtsp', method='POST', expected=201,
                     payload={'display_name': 'Synthetic bandwidth camera', 'sources': [
                         {'label': 'Main', 'url': 'rtsp://stalled-camera:8554/camera0'},
                         {'label': 'Sub', 'url': 'rtsp://stalled-camera:8554/camera1'}]},
                     headers={'X-CamAdmiral-Action': 'create-direct-rtsp-camera'})
        camera = next(c for c in consumer_directory()['cameras'] if c['name'] == 'Synthetic bandwidth camera')
        wait_for('Frigate API', lambda: urllib.request.urlopen('http://frigate:5000/api/config', timeout=5).read(), timeout=180)
        target = request_json('/internal/frigate-targets', method='POST', expected=201,
                              payload={'name': 'Synthetic Frigate', 'api_url': 'http://172.30.0.30:5000'},
                              headers={'X-CamAdmiral-Action': 'add-frigate-target'})
        target = request_json('/internal/frigate-targets')['targets'][0]
        request_json(f"/internal/frigate-targets/{target['target_id']}/cameras/{camera['id']}",
                     method='POST', expected=202, headers={'X-CamAdmiral-Action': 'sync-frigate-camera'}, timeout=40)
        key = 'camadmiral_' + camera['id'].replace('-', '_')
        def frigate_receiving():
            data = json.load(urllib.request.urlopen('http://frigate:5000/api/stats', timeout=5))
            return data.get('cameras', {}).get(key, {}).get('camera_fps', 0) > 0
        wait_for('Frigate receiving real video before editing', frigate_receiving, timeout=180)
        wait_for('high-resolution upstream consumer', lambda: stats()['active'].get('camera0', 0) > 0, timeout=180)
        state_path.write_text(json.dumps(camera))
        print('PASS: real high-resolution upstream session exists before disabling', flush=True)
        return
    camera = json.loads(state_path.read_text())
    main = next(s for s in camera['streams'] if 'record' in s['roles'])
    sub = next(s for s in camera['streams'] if 'detect' in s['roles'])
    assert main['id'] != sub['id']
    if stage == 'reenable':
        request_json(f"/internal/cameras/{camera['id']}/stream-settings", method='POST',
                     payload={'enabled': [main['id'], sub['id']], 'roles': {'record': main['id'], 'detect': sub['id']}},
                     headers={'X-CamAdmiral-Action': 'set-camera-stream-settings'}, timeout=40)
        now = next(c for c in consumer_directory()['cameras'] if c['id'] == camera['id'])
        assert [(s['id'], s['downstream']) for s in now['streams']] == [(s['id'], s['downstream']) for s in camera['streams']]
        # A healthy idle stream need not have a producer. Explicitly consume its
        # unchanged URL, then prove that it decodes the original high resolution.
        downstream = main['downstream']
        parsed = urllib.parse.urlsplit(downstream['url'])
        credentials = downstream['authentication']
        userinfo = urllib.parse.quote(credentials['username'], safe='') + ':' + urllib.parse.quote(credentials['password'], safe='')
        uri = urllib.parse.urlunsplit(parsed._replace(netloc=userinfo + '@' + parsed.netloc))
        result = subprocess.run(['ffmpeg', '-v', 'error', '-rtsp_transport', 'tcp', '-i', uri,
                                 '-frames:v', '1', '-threads', '1', '-f', 'image2pipe', '-vcodec', 'mjpeg', '-'],
                                capture_output=True, timeout=40)
        assert result.returncode == 0 and result.stdout.startswith(b'\xff\xd8'), 'Reenabled video failed'
        dimensions = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'stream=width,height',
                                     '-of', 'json', '-i', 'pipe:0'], input=result.stdout,
                                    capture_output=True, timeout=10)
        assert json.loads(dimensions.stdout)['streams'][0] == {'width': 1280, 'height': 720}
        print('PASS: reenable retains original stream IDs and URLs', flush=True)
        return

    scan = request_json('/internal/discovery/scan', method='POST', expected=202,
                        headers={'X-CamAdmiral-Action': 'scan'})
    wait_for('rescan completed', lambda: (state := discovery()).get('scan_id') == scan['scan_id']
             and state.get('status') not in {'queued', 'running'}, timeout=120)

    current = adoption(camera['id'])
    assert current['roles'] == {'record': sub['id'], 'detect': sub['id']}, current['roles']
    assert not next(s for s in current['streams'] if s['stream_uuid'] == main['id'])['enabled']
    public = next(c for c in consumer_directory()['cameras'] if c['id'] == camera['id'])
    assert len(public['streams']) == 1 and public['streams'][0]['id'] == sub['id']
    assert public['streams'][0]['downstream'] == sub['downstream']
    wait_for('old upstream session closed', lambda: stats()['active'].get('camera0', 0) == 0, timeout=60)
    baseline = stats()['describes'].get('camera0', 0)
    # More than two normal frame-probe cycles: no hidden high-res health probes.
    for _ in range(35):
        observed = stats()
        assert observed['active'].get('camera0', 0) == 0, observed
        assert observed['describes'].get('camera0', 0) == baseline, observed
        time.sleep(2)
    key = 'camadmiral_' + camera['id'].replace('-', '_')

    def configured():
        config = json.load(urllib.request.urlopen('http://frigate:5000/api/config', timeout=5))
        inputs = config.get('cameras', {}).get(key, {}).get('ffmpeg', {}).get('inputs', [])
        return len(inputs) == 1 and set(inputs[0]['roles']) >= {'record', 'detect'}
    wait_for('Frigate uses one input for both roles', configured, timeout=180)
    for alias in ('record', 'detect'):
        result = subprocess.run(['ffmpeg', '-v', 'error', '-rtsp_transport', 'tcp',
                                 '-i', f'rtsp://frigate:8554/{key}_{alias}', '-frames:v', '1',
                                 '-threads', '1', '-f', 'image2pipe', '-vcodec', 'mjpeg', '-'],
                                capture_output=True, timeout=40)
        assert result.returncode == 0 and result.stdout.startswith(b'\xff\xd8'), 'Frigate video failed'
        dimensions = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'stream=width,height',
                                     '-of', 'json', '-i', 'pipe:0'], input=result.stdout,
                                    capture_output=True, timeout=10)
        assert json.loads(dimensions.stdout)['streams'][0] == {'width': 640, 'height': 360}, 'Wrong stream decoded'
    print('PASS: disabled upstream stays disconnected; both Frigate roles decode video; persisted settings', flush=True)


def host():
    from playwright.sync_api import sync_playwright, expect
    root = Path(__file__).resolve().parents[1]
    artifacts = root / 'e2e-artifacts' / 'stream-settings'
    artifacts.mkdir(parents=True, exist_ok=True)
    compose = ['docker', 'compose', '-p', 'camadmiral-stream-settings', '-f', str(root / 'e2e/compose.yaml'),
               '-f', str(root / 'e2e/stalled-snapshot-compose.yaml'), '-f', str(root / 'e2e/snapshot-health-compose.yaml'),
               '-f', str(root / 'e2e/stream-settings-compose.yaml')]
    def run(*args, check=True):
        p = subprocess.run([*compose, *args], text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        print(p.stdout, flush=True)
        with (artifacts / 'workload.log').open('a') as log:
            log.write(p.stdout)
        if check:
            p.check_returncode()
        return p.stdout.strip()
    def stage(name):
        run('run', '--rm', '--entrypoint', 'python', 'test-driver', '/e2e/stream_settings.py', name)
    try:
        run('build', 'camadmiral')
        run('up', '-d', 'camera-open', 'stalled-camera', 'camadmiral', 'frigate')
        stage('prepare')
        base = 'http://' + run('port', 'camadmiral', '18080')
        with sync_playwright() as playwright:
            browser = playwright.webkit.launch()
            page = browser.new_page(http_credentials={'username': 'admin', 'password': 'synthetic-e2e-admin-password'})
            page.goto(base)
            row = page.locator('#camera-rows tr.camera-row').filter(has_text='Synthetic bandwidth camera')
            row.get_by_role('button', name='Streams', exact=True).click()
            main = page.get_by_label('Main settings', exact=True)
            sub = page.get_by_label('Sub settings', exact=True)
            main.get_by_label('Enabled', exact=True).uncheck()
            sub.get_by_label('Record', exact=True).check()
            sub.get_by_label('Detect', exact=True).check()
            page.get_by_role('button', name='Save stream settings', exact=True).click()
            expect(page.get_by_text('Stream settings saved. Frigate synchronization queued.', exact=True)).to_be_visible(timeout=60000)
            page.screenshot(path=str(artifacts / 'saved.png'))
            browser.close()
        stage('verify')
        run('restart', 'camadmiral')
        stage('verify')
        stage('reenable')
    finally:
        run('logs', '--no-color', 'camadmiral', 'frigate', check=False)
        run('down', '--volumes', '--remove-orphans', check=False)


if __name__ == '__main__':
    driver(sys.argv[1]) if sys.argv[1:] else host()

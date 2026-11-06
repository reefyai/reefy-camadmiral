"""Durable import/takeover regression against real Frigate and synthetic media.

Run by the GitHub Actions release gate, never against an operator's device.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
STATE = Path('/state/frigate-import.json')


def driver(stage):
    from scenarios import request_json, wait_for, wait_for_health, consumer_directory

    def frigate(path):
        with urllib.request.urlopen('http://frigate:5000/api' + path, timeout=10) as r:
            return json.load(r)

    wait_for_health()
    wait_for('Frigate API', lambda: frigate('/stats'), timeout=180)
    if stage == 'prepare':
        request_json('/internal/frigate-targets', method='POST', expected=201,
                     payload={'name': 'Synthetic import target', 'api_url': 'http://frigate:5000'},
                     headers={'X-CamAdmiral-Action': 'add-frigate-target'})
        target = request_json('/internal/frigate-targets')['targets'][0]
        original = frigate('/config')
        saved = frigate('/config/raw')
        STATE.write_text(json.dumps({'target_id': target['target_id'], 'saved': saved,
                                     'camera': original['cameras']['operator_camera']}))
        return
    state = json.loads(STATE.read_text())
    base = '/internal/frigate-targets/' + state['target_id']
    if stage == 'preview':
        preview = request_json(base + '/import')
        assert preview['restart_required'] is True
        row = next(r for r in preview['cameras'] if r['key'] == 'operator_camera')
        assert row['category'] == 'new', row
        assert frigate('/config/raw') == state['saved'], 'Preview changed Frigate'
        assert not consumer_directory()['cameras'], 'Preview adopted a camera'
        return
    if stage == 'verify':
        wait_for('imported capture', lambda: frigate('/stats').get('cameras', {}).get(
            'operator_camera', {}).get('camera_fps', 0) > 0, timeout=180)
        current = frigate('/config')['cameras']['operator_camera']
        before = state['camera']
        for field in ('friendly_name', 'detect', 'record', 'objects', 'zones', 'motion', 'enabled'):
            assert current.get(field) == before.get(field), 'Changed preserved field: ' + field
        assert len(consumer_directory()['cameras']) == 1
        preview = request_json(base + '/import')
        row = next(r for r in preview['cameras'] if r['key'] == 'operator_camera')
        assert row['category'] == 'managed', row
        assert set(frigate('/config')['cameras']) == {'operator_camera'}
        raw = frigate('/config/raw_paths')
        assert all('127.0.0.1:8554/' in i['path'] for i in raw['cameras']['operator_camera']['ffmpeg']['inputs'])
        assert any(':18554/' in str(s) for s in raw['go2rtc']['streams'].values())
        return
    raise ValueError(stage)


def host():
    from playwright.sync_api import sync_playwright, expect
    artifacts = ROOT / 'e2e-artifacts/frigate-import'
    artifacts.mkdir(parents=True, exist_ok=True)
    compose = ['docker', 'compose', '-p', 'camadmiral-frigate-import',
               '-f', str(ROOT / 'e2e/compose.yaml'),
               '-f', str(ROOT / 'e2e/recording-compose.yaml')]
    result = {'status': 'failed', 'revision': subprocess.check_output(
        ['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()}

    def run(*args, check=True):
        p = subprocess.run([*compose, *args], cwd=ROOT, text=True,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        with (artifacts / 'transcript.log').open('a') as log:
            log.write(p.stdout)
        if check:
            p.check_returncode()
        return p.stdout.strip()

    def stage(name):
        run('run', '--rm', '--entrypoint', 'python', 'test-driver', '/e2e/frigate_import.py', name)

    def recordings():
        code = """import json,sqlite3
from pathlib import Path
with sqlite3.connect('file:/config/frigate.db?mode=ro',uri=True) as db:
 rows=db.execute("select id,path,start_time,end_time from recordings where camera='operator_camera' order by start_time").fetchall()
print(json.dumps([list(r) for r in rows if Path(r[1]).is_file() and Path(r[1]).stat().st_size>0]))
"""
        return json.loads(run('exec', '-T', 'frigate', 'python3', '-c', code))

    try:
        run('build', 'camadmiral')
        run('up', '-d', 'camadmiral', 'camera-open', 'frigate')
        stage('prepare')
        deadline = time.monotonic() + 180
        before = []
        while time.monotonic() < deadline:
            before = recordings()
            if len(before) >= 2:
                break
            time.sleep(3)
        assert len(before) >= 2, 'Fixture must save real recordings before migration'
        result['baseline_recordings'] = before
        stage('preview')
        base = 'http://' + run('port', 'camadmiral', '18080')
        with sync_playwright() as p:
            browser = p.webkit.launch()
            page = browser.new_page(http_credentials={'username': 'admin', 'password': 'synthetic-e2e-admin-password'})
            try:
                page.goto(base + '/settings/integrations')
                page.get_by_role('button', name='Import from Frigate', exact=True).click()
                expect(page.get_by_text('operator_camera', exact=True)).to_be_visible()
                page.get_by_role('button', name='Select all', exact=True).click()
                page.get_by_label('Allow Frigate restart').check()
                page.get_by_role('button', name='Import and manage selected', exact=True).click()
                expect(page.get_by_text('applied', exact=True)).to_be_visible(timeout=180000)
                page.screenshot(path=str(artifacts / 'import-results.png'))
            except Exception:
                page.screenshot(path=str(artifacts / 'failure.png'))
                raise
            finally:
                browser.close()
        stage('verify')
        switched = time.time()
        deadline = time.monotonic() + 150
        after = []
        while time.monotonic() < deadline:
            after = recordings()
            if any(r[2] > switched for r in after):
                break
            time.sleep(3)
        assert any(r[2] > switched for r in after), 'No new recording after takeover'
        assert {r[0] for r in before} <= {r[0] for r in after}, 'Lost historical recordings'
        run('restart', 'camadmiral', 'frigate')
        stage('verify')
        result.update(status='passed', final_recordings=after)
    finally:
        (artifacts / 'result.json').write_text(json.dumps(result, indent=2))
        run('logs', '--no-color', check=False)
        if not os.environ.get('CAMADMIRAL_E2E_KEEP'):
            run('down', '--volumes', '--remove-orphans', check=False)


if __name__ == '__main__':
    driver(sys.argv[1]) if len(sys.argv) > 1 else host()

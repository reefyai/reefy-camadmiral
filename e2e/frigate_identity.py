"""Actions regression: literal-IP imports follow verified physical identities.

Two logical channels share one synthetic ONVIF device. Docker address changes
model DHCP after a power event; the application must discover and validate them.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
STATE = Path('/state/frigate-identity.json')
DATABASE = '/var/lib/camadmiral/camadmiral.db'
KEYS = ('identity_camera', 'channel_camera')
ENDPOINT = 'urn:uuid:synthetic-onvif-camera'


def driver(stage):
    import yaml
    from scenarios import (consumer_directory, directory_signature, request_json,
                           wait_for, wait_for_health)

    def frigate(path):
        with urllib.request.urlopen('http://frigate:5000/api' + path, timeout=15) as response:
            return json.load(response)

    def sql(query, parameters=()):
        with sqlite3.connect('file:' + DATABASE + '?mode=ro', uri=True) as connection:
            connection.row_factory = sqlite3.Row
            return [dict(row) for row in connection.execute(query, parameters)]

    def links():
        # The baseline must fail a behavioral assertion, not a missing-table error.
        if not sql("SELECT name FROM sqlite_master WHERE name='camera_identity_links'"):
            return {}
        return {row['camera_uuid']: json.loads(row['payload']) for row in sql('SELECT * FROM camera_identity_links')}

    if stage in {'remove-links', 'remove-primary-anchor'}:
        state = json.loads(STATE.read_text())
        with sqlite3.connect(DATABASE) as connection:
            if stage == 'remove-links':
                connection.execute('DELETE FROM camera_identity_links')
            else:
                camera_uuid = state['camera_ids']['identity_camera']
                connection.execute('DELETE FROM camera_identity_links WHERE camera_uuid=?', (camera_uuid,))
                connection.execute('DELETE FROM camera_identity_periods WHERE camera_uuid=?', (camera_uuid,))
        return

    wait_for_health()
    wait_for('Frigate API ready', lambda: frigate('/stats'), timeout=180)
    if stage == 'prepare':
        target = request_json('/internal/frigate-targets', method='POST', expected=201,
            payload={'name': 'Synthetic identity target', 'api_url': 'http://frigate:5000'},
            headers={'X-CamAdmiral-Action': 'add-frigate-target'})
        target = request_json('/internal/frigate-targets')['targets'][0]
        STATE.write_text(json.dumps({'target': target['target_id'], 'original': yaml.safe_load(frigate('/config/raw'))}))
        return
    state = json.loads(STATE.read_text())
    base = '/internal/frigate-targets/' + state['target']
    if stage == 'imported':
        job = request_json(base + '/import/' + state['job'])
        assert job['status'] == 'completed' and all(row['status'] == 'applied' for row in job['cameras']), job
        state['camera_ids'] = {row['key']: row['camera_uuid'] for row in job['cameras']}
        assert set(state['camera_ids']) == set(KEYS)
        assert len(set(state['camera_ids'].values())) == 2, 'Physical channels were merged'
        state['signature'] = directory_signature(consumer_directory())
        assert sum(len(c['streams']) for c in state['signature'].values()) == 4
        state['credentials'] = sql('SELECT credential_uuid,username,hex(password_ciphertext) AS ciphertext FROM camera_credentials ORDER BY credential_uuid')
        state['managed_config'] = yaml.safe_load(frigate('/config/raw'))
        original = state['original']
        current = state['managed_config']
        assert set(current['cameras']) == set(original['cameras'])
        assert current['camera_groups'] == original['camera_groups']
        for key in KEYS:
            for field, value in original['cameras'][key].items():
                if field not in {'ffmpeg', 'live'}:
                    assert current['cameras'][key][field] == value, (key, field)
        initial = links()
        state['initial_identity_learned'] = (len(initial) == 2 and
            all(link.get('onvif', {}).get('endpoint_reference') == ENDPOINT for link in initial.values()) and
            len({link['candidate_uuid'] for link in initial.values()}) == 1)
        STATE.write_text(json.dumps(state))
        return
    if stage == 'unanchored':
        primary = state['camera_ids']['identity_camera']
        secondary = state['camera_ids']['channel_camera']
        wait_for('anchored channel follows the moved device', lambda: all(row['source_host'] == '172.28.0.18'
            for row in sql('SELECT source_host FROM onvif_profiles WHERE camera_uuid=?', (secondary,))), timeout=240)
        assert primary not in links(), 'Legacy source was associated by stale IP without operator review'
        assert {r['source_host'] for r in sql('SELECT source_host FROM onvif_profiles WHERE camera_uuid=?', (primary,))} == {'172.28.0.17'}
        review = request_json('/internal/cameras/' + primary + '/identity', method='POST',
            payload={'address': '172.28.0.18'}, headers={'X-CamAdmiral-Action': 'link-camera-identity'})
        assert review['status'] == 'review' and review['identity']['endpoint_reference'] == ENDPOINT
        assert primary not in links(), 'Identify must not bind before confirmation'
        changed = request_json('/internal/cameras/' + primary + '/identity', method='POST', expected=409,
            payload={'address': '172.28.0.18', 'confirm': True, 'endpoint_reference': 'urn:uuid:synthetic-wrong-device'},
            headers={'X-CamAdmiral-Action': 'link-camera-identity'})
        assert changed['status'] == 'identity_changed' and primary not in links()
        return
    if stage.startswith('verify-'):
        address = stage.removeprefix('verify-')
        def recovered():
            sources = sql('SELECT source_host FROM onvif_profiles')
            current = links()
            periods = sql('SELECT camera_uuid,ip_address,onvif_identity FROM camera_identity_periods WHERE ended_at IS NULL')
            health = sql('SELECT health_status,probe_status FROM managed_streams WHERE enabled=1')
            # Source hosts are written before relay validation, and may still
            # roll back. Wait for the validated identity commit for both owners.
            return (len(sources) == 4 and all(row['source_host'] == address for row in sources)
                    and len(current) == 2 and all(link.get('ip') == address for link in current.values())
                    and len(health) == 4 and all(row['health_status'] == 'healthy' and row['probe_status'] == 'ready' for row in health)
                    and {row['camera_uuid'] for row in periods if row['ip_address'] == address and row['onvif_identity'] == ENDPOINT}
                        == set(state['camera_ids'].values()))
        wait_for('both logical channels recover through physical identity', recovered, timeout=240)
        current_links = links()
        assert state['initial_identity_learned'], 'Literal-IP import did not learn stable physical identity'
        assert len(current_links) == 2 and all(link['onvif']['endpoint_reference'] == ENDPOINT for link in current_links.values())
        assert len({link['candidate_uuid'] for link in current_links.values()}) == 1
        assert directory_signature(consumer_directory()) == state['signature'], 'Camera/stream IDs, names, roles or downstream URLs changed'
        assert sql('SELECT credential_uuid,username,hex(password_ciphertext) AS ciphertext FROM camera_credentials ORDER BY credential_uuid') == state['credentials']
        assert yaml.safe_load(frigate('/config/raw')) == state['managed_config'], 'Address recovery rewrote Frigate configuration'
        for camera_uuid in state['camera_ids'].values():
            periods = sql('SELECT * FROM camera_identity_periods WHERE camera_uuid=? ORDER BY started_at DESC', (camera_uuid,))
            assert periods and periods[0]['ip_address'] == address and periods[0]['onvif_identity'] == ENDPOINT
        return
    if stage == 'foreign-rejected':
        # A different camera at the original IP must never take over a known identity.
        camera_uuid = state['camera_ids']['channel_camera']
        conflict = request_json('/internal/cameras/' + camera_uuid + '/identity', method='POST', expected=409,
            payload={'address': '172.28.0.13', 'confirm': True, 'endpoint_reference': 'urn:uuid:synthetic-unrelated-device'},
            headers={'X-CamAdmiral-Action': 'link-camera-identity'})
        assert conflict['status'] == 'identity_conflict'
        return
    if stage == 'evidence':
        print(json.dumps({'camera_ids': state['camera_ids'], 'signature': state['signature'],
            'initial_identity_learned': state['initial_identity_learned'],
            'sources': sql('SELECT camera_uuid,source_host,source_path,source_query FROM onvif_profiles'),
            'links': links(), 'history': sql('SELECT camera_uuid,ip_address,mac_address,onvif_identity,ended_at FROM camera_identity_periods')}))
        return
    raise ValueError(stage)


def host():
    from playwright.sync_api import sync_playwright, expect
    revision = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    os.environ['IDENTITY_TEST_IMAGE'] = 'camadmiral-frigate-identity:' + revision[:12]
    artifacts = ROOT / 'e2e-artifacts/frigate-identity'
    artifacts.mkdir(parents=True, exist_ok=True)
    compose = ['docker', 'compose', '-p', 'camadmiral-frigate-identity', '-f', str(ROOT / 'e2e/frigate-identity-compose.yaml')]
    result = {'status': 'failed', 'revision': revision, 'moves': []}

    def run(*args, check=True):
        completed = subprocess.run([*compose, *args], cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        with (artifacts / 'transcript.log').open('a') as log:
            log.write(completed.stdout)
        if check and completed.returncode:
            print(completed.stdout[-10000:], flush=True)
            completed.check_returncode()
        return completed.stdout.strip()

    def stage(name):
        print('Imported identity stage: ' + name, flush=True)
        return run('run', '--rm', '--no-deps', 'test-driver', name)

    def state():
        return json.loads(run('run', '--rm', '--no-deps', '--entrypoint', 'cat', 'test-driver', str(STATE)))

    def recordings():
        return json.loads(run('exec', '-T', 'frigate', 'python3', '-c', """import json,sqlite3
from pathlib import Path
with sqlite3.connect('file:/config/frigate.db?mode=ro',uri=True) as db:
 rows=db.execute('SELECT id,camera,path,start_time,end_time FROM recordings ORDER BY start_time').fetchall()
print(json.dumps([list(r) for r in rows if Path(r[2]).is_file() and Path(r[2]).stat().st_size>0]))"""))

    def fresh(since):
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            rows = recordings()
            if all(any(row[1] == key and row[3] > since for row in rows) for key in KEYS):
                return rows
            time.sleep(3)
        raise AssertionError('Both imported channels must save new recording files')

    def history(seed=False):
        # Reuse the production-table fixture contract from the established import lab.
        code = """import json,sqlite3,time
with sqlite3.connect('/config/frigate.db') as db:
 if SEED:
  stamp=time.time()-20
  event={'id':'synthetic-identity-event','camera':'identity_camera','label':'person','start_time':stamp,'end_time':stamp+10,'top_score':0.9,'score':0.9,'false_positive':0,'zones':'[]','thumbnail':'','has_clip':1,'has_snapshot':0,'region':'[]','box':'[]','area':100,'retain_indefinitely':1,'ratio':1,'plus_id':'','model_hash':'synthetic','detector_type':'cpu','model_type':'synthetic','data':'{}'}
  columns={r[1] for r in db.execute('pragma table_info(event)')}
  event={k:v for k,v in event.items() if k in columns}
  db.execute('INSERT INTO event ('+','.join(event)+') VALUES ('+','.join('?' for _ in event)+')',list(event.values()))
  db.execute('INSERT INTO reviewsegment(id,camera,start_time,end_time,severity,thumb_path,data) VALUES (?,?,?,?,?,?,?)',('synthetic-identity-review','identity_camera',stamp,stamp+10,'alert','/media/frigate/clips/synthetic-review.jpg','{}'))
  db.commit()
 print(json.dumps({'events':db.execute("SELECT * FROM event WHERE id='synthetic-identity-event'").fetchall(),'review':db.execute("SELECT * FROM reviewsegment WHERE id='synthetic-identity-review'").fetchall()}))"""
        return json.loads(run('exec', '-T', 'frigate', 'python3', '-c', 'SEED=' + repr(seed) + '\n' + code))

    def verify(address, since):
        stage('verify-' + address)
        after = fresh(since)
        assert {row[0] for row in before} <= {row[0] for row in after}, 'Historical recordings were lost'
        assert history() == history_before, 'Event/review rows changed'
        # Decode original history and a new file for each logical channel.
        for key in KEYS:
            for row in (next(row for row in before if row[1] == key), next(row for row in reversed(after) if row[1] == key and row[3] > since)):
                playback = f'http://frigate:5000/api/{key}/start/{row[3]}/end/{row[4]}/clip.mp4'
                run('run', '--rm', '--no-deps', '--entrypoint', 'ffmpeg', 'test-driver',
                    '-v', 'error', '-i', playback, '-frames:v', '1', '-threads', '1', '-f', 'null', '-')
            latest = next(row for row in reversed(after) if row[1] == key and row[3] > since)
            playback = f'http://frigate:5000/api/{key}/start/{latest[3]}/end/{latest[4]}/clip.mp4'
            observed = json.loads(run('run', '--rm', '--no-deps', '--entrypoint', 'ffprobe', 'test-driver',
                '-v', 'error', '-i', playback, '-select_streams', 'v:0', '-show_entries', 'stream=width,height', '-of', 'json'))
            moved = address in {'172.28.0.15', '172.28.0.18'}
            width, height = ((960, 540) if moved else (1280, 720)) if key == 'identity_camera' else ((480, 270) if moved else (640, 360))
            assert observed['streams'] == [{'width': width, 'height': height}], 'New recording came from the wrong physical source'
        result['moves'].append({'address': address, 'fresh_recording_ids': [r[0] for r in after if r[3] > since],
            'history_preserved': True, 'old_and_new_video_decoded': True})

    try:
        run('down', '--volumes', '--remove-orphans')
        run('build', 'camadmiral')
        # Share synthetic scratch state while keeping the database's production
        # owner and 0600 permissions. The driver reads it as the application UID.
        run('run', '--rm', '--no-deps', '--user', '0:0', '--entrypoint', 'chmod', 'test-driver', '0777', '/state')
        run('up', '-d', 'camadmiral', 'frigate', 'camera-onvif')
        result['images'] = subprocess.check_output(['docker', 'image', 'inspect', os.environ['IDENTITY_TEST_IMAGE'],
            'ghcr.io/blakeblackshear/frigate:0.17.2', '--format', '{{.Id}} {{json .RepoDigests}}'], text=True).splitlines()
        stage('prepare')
        before = fresh(time.time())
        history_before = history(seed=True)
        result['history_before'] = history_before
        result['recordings_before'] = before
        with sync_playwright() as playwright:
            browser = playwright.webkit.launch()
            page = browser.new_page(http_credentials={'username': 'admin', 'password': 'synthetic-e2e-admin-password'})
            try:
                page.goto('http://' + run('port', 'camadmiral', '18080') + '/settings/integrations')
                page.get_by_role('button', name='Import from Frigate', exact=True).click()
                page.get_by_label('Import Synthetic imported identity', exact=True).check()
                page.get_by_label('Import Synthetic independent channel', exact=True).check()
                page.get_by_label('Allow Frigate restart').check()
                with page.expect_response(lambda response: response.request.method == 'POST' and '/import/' in response.url) as imported:
                    page.get_by_role('button', name='Import and manage selected', exact=True).click()
                job = imported.value.json()['id']
                run('run', '--rm', '--no-deps', '--entrypoint', 'python', 'test-driver', '-c',
                    "import json;from pathlib import Path;p=Path('/state/frigate-identity.json');s=json.loads(p.read_text());s['job']=" + repr(job) + ";p.write_text(json.dumps(s))")
                expect(page.get_by_label('Allow Frigate restart')).to_be_enabled(timeout=300000)
                page.screenshot(path=str(artifacts / 'imported.png'))
                stage('imported')
                result['import_completed'] = True
                result['initial_identity_learned'] = state()['initial_identity_learned']
                since = time.time()
                run('stop', 'camera-onvif')
                run('rm', '-f', 'camera-onvif')
                run('up', '-d', 'camera-moved')
                verify('172.28.0.15', since)
                page.goto('http://' + run('port', 'camadmiral', '18080'))
                page.locator('#camera-rows tr.camera-row').filter(has_text='Synthetic imported identity').get_by_role('button', name='Details', exact=True).click()
                expect(page.get_by_text('Identity history', exact=True)).to_be_visible()
                expect(page.locator('#app-modal')).to_contain_text('172.28.0.15', timeout=30000)
                expect(page.locator('#app-modal')).to_contain_text('172.28.0.13')
                page.screenshot(path=str(artifacts / 'imported-identity-history.png'))
                run('up', '-d', 'old-address-device')
                stage('foreign-rejected')
                run('restart', 'camadmiral')
                verify('172.28.0.15', time.time())
                run('stop', 'camadmiral')
                stage('remove-links')
                run('start', 'camadmiral')
                verify('172.28.0.15', time.time())
                since = time.time()
                run('stop', 'camera-moved')
                run('rm', '-f', 'camera-moved')
                run('up', '-d', 'camera-returned')
                verify('172.28.0.17', since)
                run('stop', 'camadmiral')
                stage('remove-primary-anchor')
                run('stop', 'camera-returned')
                run('rm', '-f', 'camera-returned')
                run('up', '-d', 'camera-confirmed')
                run('start', 'camadmiral')
                stage('unanchored')
                page.goto('http://' + run('port', 'camadmiral', '18080'))
                row = page.locator('#camera-rows tr.camera-row').filter(has_text='Synthetic imported identity')
                row.get_by_role('button', name='Details', exact=True).click()
                page.get_by_role('button', name='Link device identity', exact=True).click()
                page.get_by_label("Camera's current IP address", exact=True).fill('172.28.0.18')
                page.get_by_role('button', name='Identify camera', exact=True).click()
                expect(page.get_by_text('ONVIF identity: ' + ENDPOINT, exact=True)).to_be_visible(timeout=60000)
                with page.expect_response(lambda response: response.request.method == 'POST' and response.url.endswith('/identity')) as confirmed:
                    page.get_by_role('button', name='Confirm device identity', exact=True).click()
                assert confirmed.value.json()['status'] == 'linked'
                expect(page.get_by_text('Device identity linked. Video recovery is being validated.', exact=True)).to_be_visible()
                page.screenshot(path=str(artifacts / 'legacy-identity-confirmed.png'))
                verify('172.28.0.18', time.time())
                result['identity'] = json.loads(stage('evidence'))
                result['status'] = 'passed'
            except Exception:
                page.screenshot(path=str(artifacts / 'failure.png'))
                raise
            finally:
                browser.close()
    finally:
        if result.get('import_completed'):
            try:
                result['identity'] = json.loads(stage('evidence'))
            except Exception:
                result['identity_evidence_unavailable'] = True
        (artifacts / 'result.json').write_text(json.dumps(result, indent=2))
        run('logs', '--no-color', check=False)
        if not os.environ.get('CAMADMIRAL_E2E_KEEP'):
            run('down', '--volumes', '--remove-orphans', check=False)


if __name__ == '__main__':
    driver(sys.argv[1]) if len(sys.argv) > 1 else host()

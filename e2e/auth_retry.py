"""Real RTSP authentication outage, bounded retries and decoded-video recovery."""
import json
import sys
import time
import urllib.request


def driver():
    from scenarios import consumer_directory, discovery, incidents, request, request_json, wait_for, wait_for_health
    wait_for_health()
    def control(path):
        return urllib.request.urlopen('http://stalled-camera:8080/' + path, timeout=5).read()
    wait_for('proxy', lambda: control('auth-stats'))
    request_json('/internal/cameras/rtsp', method='POST', expected=201,
                 payload={'display_name': 'Synthetic auth camera', 'sources': [
                     {'label': 'Main', 'url': 'rtsp://stalled-camera:8554/camera0'},
                     {'label': 'Sub', 'url': 'rtsp://stalled-camera:8554/camera1'}]},
                 headers={'X-CamAdmiral-Action': 'create-direct-rtsp-camera'})
    camera = next(c for c in consumer_directory()['cameras'] if c['name'] == 'Synthetic auth camera')
    def states():
        a = next(d['adoption'] for d in discovery()['devices']
                 if (d.get('adoption') or {}).get('camera_uuid') == camera['id'])
        return {role: next(s['health_status'] for s in a['streams'] if s['stream_uuid'] == uid)
                for role, uid in a['roles'].items()}
    wait_for('initial media healthy', lambda: all(v == 'healthy' for v in states().values()))
    control('reject/camera1')
    def rejected():
        s = states()
        assert s['record'] != 'auth_failed', s
        return s['detect'] == 'auth_failed'
    wait_for('scoped authentication error', rejected, timeout=120)
    opened = [i for i in incidents('open')['incidents'] if i['camera_id'] == camera['id']]
    assert any(i['kind'] == 'authentication_failed' for i in opened)
    baseline = json.loads(control('auth-stats'))['requests']
    start = time.monotonic()
    def first_retry():
        assert states()['record'] != 'auth_failed'
        return json.loads(control('auth-stats'))['requests'] > baseline
    wait_for('first authentication retry', first_retry, timeout=90, interval=2)
    elapsed = time.monotonic() - start
    assert elapsed >= 40, ('retried too soon', elapsed)
    time.sleep(5)  # Let the single attempt close before measuring quiet backoff.
    after = json.loads(control('auth-stats'))['requests']
    assert after - baseline <= 2, ('too many authentication requests', baseline, after)
    time.sleep(35)
    assert json.loads(control('auth-stats'))['requests'] == after, 'Authentication retry storm'
    assert states()['detect'] == 'auth_failed', 'Rejection cleared without video'
    print(json.dumps({'first_retry_seconds': elapsed, 'requests_in_attempt': after - baseline}), flush=True)
    control('resume')
    wait_for('automatic recovery after five-minute backoff',
             lambda: all(v == 'healthy' for v in states().values()), timeout=330, interval=2)
    status, jpeg, _ = request(f"/internal/cameras/{camera['id']}/snapshot.jpg", timeout=40)
    assert status == 200 and jpeg.startswith(b'\xff\xd8\xff') and jpeg.endswith(b'\xff\xd9')
    assert not [i for i in incidents('open')['incidents'] if i['camera_id'] == camera['id']]
    now = next(c for c in consumer_directory()['cameras'] if c['id'] == camera['id'])
    assert [(s['id'], s['roles'], s['downstream']) for s in now['streams']] == [
        (s['id'], s['roles'], s['downstream']) for s in camera['streams']]
    print('PASS: real 401, scoped failure, production backoff, decoded recovery, incident resolution, stable URLs', flush=True)


if __name__ == '__main__':
    if sys.argv[1:]:
        driver()
    else:
        from snapshot_health import host
        host('auth_retry.py', 'auth-retry')

"""Real same-address video stall with live audio, recordings and bounded recovery."""
from __future__ import annotations
import hashlib
import json
import re
import tempfile
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.request
import urllib.parse

ROOT = Path(__file__).resolve().parents[1]
STATE = Path('/state/stall-recovery.json')


def driver(stage):
    from scenarios import consumer_directory, request_json, wait_for, wait_for_health
    from frigate_identity import recording_for_playback, recording_playback_url
    wait_for_health()
    def get(path):
        return json.load(urllib.request.urlopen('http://frigate:5000/api' + path, timeout=10))
    def control(path):
        return urllib.request.urlopen('http://stalled-camera:8080/' + path, timeout=5).read()
    def decode(uri):
        p = subprocess.run(['ffmpeg', '-v', 'error', '-nostdin', '-threads', '1',
                            '-rtsp_transport', 'tcp', '-i', uri, '-map', '0:v:0', '-an',
                            '-frames:v', '1', '-f', 'framemd5', '-'], capture_output=True, timeout=20)
        frames = [l for l in p.stdout.splitlines() if l and not l.startswith(b'#')]
        assert p.returncode == 0 and frames, 'Fresh relay video did not decode'
        return len(frames)
    if stage == 'setup':
        wait_for('synthetic proxy', lambda: control('stream-stats'))
        cameras = []
        for name, host, paths in [('Synthetic stalled camera', 'stalled-camera', ['camera0','camera1']),
                                  ('Synthetic unaffected camera', 'camera-open', ['camera2'])]:
            request_json('/internal/cameras/rtsp', method='POST', expected=201,
                         payload={'display_name': name, 'sources': [
                             {'label': str(i), 'url': f'rtsp://{host}:8554/{path}'}
                             for i,path in enumerate(paths)]},
                         headers={'X-CamAdmiral-Action':'create-direct-rtsp-camera'})
            cameras.append(next(c for c in consumer_directory()['cameras'] if c['name'] == name))
        wait_for('Frigate API', lambda: get('/config'), timeout=180)
        request_json('/internal/frigate-targets', method='POST', expected=201,
                     payload={'name':'Synthetic recorder','api_url':'http://172.30.0.30:5000'},
                     headers={'X-CamAdmiral-Action':'add-frigate-target'})
        target = request_json('/internal/frigate-targets')['targets'][0]
        keys = []
        for camera in cameras:
            request_json(f"/internal/frigate-targets/{target['target_id']}/cameras/{camera['id']}",
                         method='POST', expected=202, timeout=40,
                         headers={'X-CamAdmiral-Action':'sync-frigate-camera'})
            keys.append('camadmiral_' + camera['id'].replace('-', '_'))
        wait_for('all recorder cameras processing video', lambda: all(
            get('/stats').get('cameras',{}).get(k,{}).get('camera_fps',0)>0 for k in keys), timeout=180)
        STATE.write_text(json.dumps({'cameras':cameras,'keys':keys,'config':get('/config/raw')}))
        print('PASS: two synthetic cameras processing actual video', flush=True)
        return
    state = json.loads(STATE.read_text())
    if stage == 'hold-audio':
        processes=[]
        try:
            for stream in state['cameras'][0]['streams']:
                target=stream['downstream']
                u=urllib.parse.urlsplit(target['url'])
                auth=target['authentication']
                netloc=urllib.parse.quote(auth['username'],safe='')+':'+urllib.parse.quote(auth['password'],safe='')+'@'+u.netloc
                uri=urllib.parse.urlunsplit(u._replace(netloc=netloc))
                processes.append(subprocess.Popen(['ffmpeg','-v','error','-nostdin','-rtsp_transport','tcp',
                    '-allowed_media_types','audio','-i',uri,'-map','0:a:0','-c','copy','-f','null','-'],
                    stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL))
            while all(p.poll() is None for p in processes):time.sleep(1)
            raise AssertionError('Synthetic audio reader stopped unexpectedly')
        finally:
            for process in processes:
                process.terminate();process.wait(timeout=5)
    if stage == 'arm':
        control('freeze-current-video')
        print('Video frozen only on existing sessions; audio and RTSP control remain live', flush=True)
        return
    if stage in ('verify','verify-initial'):
        # A reader attached to the old producer cannot recover until CamAdmiral
        # replaces it. No test-side reconnect, resume, rescan or restart is used.
        wait_for('automatic same-address capture recovery', lambda: all(
            get('/stats').get('cameras',{}).get(k,{}).get('camera_fps',0)>0 for k in state['keys']),
            timeout=110, interval=2)
        for key in state['keys']:
            for role in ('record','detect'):
                decode(f'rtsp://frigate:8554/{key}_{role}')
        current = {c['id']:c for c in consumer_directory()['cameras']}
        for before in state['cameras']:
            after = current[before['id']]
            assert [(s['id'],s['roles'],s['downstream']) for s in before['streams']] == [
                   (s['id'],s['roles'],s['downstream']) for s in after['streams']], 'Stream identity or credentials changed'
        assert get('/config/raw') == state['config'], 'Watchdog changed saved recorder configuration'
        print('PASS: real video decoded on every role; identities and configuration preserved', flush=True)
        return
    if stage == 'verify-unaffected':
        key=state['keys'][1]
        assert get('/stats').get('cameras',{}).get(key,{}).get('camera_fps',0)>0,'Unrelated camera lost capture during camera outage'
        decode(f'rtsp://frigate:8554/{key}_record')
        assert get('/config/raw')==state['config'],'Offline camera changed recorder settings'
        print('PASS: unrelated camera continues decoding while the failed camera is offline',flush=True)
        return
    if stage == 'playback':
        rows = json.loads(Path('/state/stall-after.json').read_text())
        before = json.loads(Path('/state/stall-before.json').read_text())
        since = json.loads(Path('/state/stall-clock.json').read_text())['since']
        for key in state['keys']:
            for label,values,when in [('historical',before,-1),('fresh',rows,since)]:
                sample = recording_for_playback(values,key,since=when)
                assert sample, 'Complete recording missing: '+label
                data = urllib.request.urlopen(recording_playback_url(sample,values),timeout=30).read()
                clip=Path('/state/stall-playback.mp4');clip.write_bytes(data)
                p = subprocess.run(['ffmpeg','-v','error','-threads','1','-i',str(clip),'-an',
                                    '-frames:v','1','-f','framemd5','-'],capture_output=True,timeout=30)
                assert p.returncode == 0 and any(l and not l.startswith(b'#') for l in p.stdout.splitlines()), 'Recording did not decode'
        print('PASS: historical and fresh recordings decode for affected and unrelated cameras', flush=True)
        return
    raise ValueError(stage)


def host():
    artifacts = ROOT/'e2e-artifacts/stall-recovery'
    artifacts.mkdir(parents=True,exist_ok=True)
    compose=['docker','compose','-p','camadmiral-stall-recovery-e2e','-f',str(ROOT/'e2e/compose.yaml'),
             '-f',str(ROOT/'e2e/stalled-snapshot-compose.yaml'),'-f',str(ROOT/'e2e/stall-recovery-compose.yaml')]
    def run(*args,check=True):
        p=subprocess.run([*compose,*args],text=True,stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
        with (artifacts/'workload.log').open('a') as f:f.write(p.stdout)
        print(p.stdout,flush=True)
        if check:p.check_returncode()
        return p.stdout
    def stage(name):return run('run','--rm','--entrypoint','python','test-driver','/e2e/stall_recovery.py',name)
    def state(service):
        cid=run('ps','-q',service).strip()
        return json.loads(subprocess.check_output(['docker','inspect',cid]))[0]['State']['StartedAt']
    def pid():
        return run('exec','-T','camadmiral','sh','-c','pidof go2rtc').strip()
    sql="import sqlite3,json; d=sqlite3.connect('file:/config/frigate.db?mode=ro',uri=True); print(json.dumps(d.execute('select id,camera,path,start_time,end_time from recordings order by start_time').fetchall()))"
    def recordings(name):
        rows=json.loads(run('exec','-T','frigate','python3','-c',sql))
        # Shared driver volume is written through a bounded driver command.
        run('run','--rm','--entrypoint','python','test-driver','-c',
            'from pathlib import Path; Path('+repr('/state/'+name)+').write_text('+repr(json.dumps(rows))+')')
        return rows
    result={'source':subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()}
    try:
        run('down','--volumes','--remove-orphans')
        application_ref=os.environ.get('CAMADMIRAL_E2E_APPLICATION_REF','')
        if application_ref:
            assert re.fullmatch('[0-9a-f]{40}',application_ref),'Baseline requires an exact repository commit'
            with tempfile.TemporaryDirectory(prefix='camadmiral-baseline-') as directory:
                archive=subprocess.check_output(['git','archive',application_ref],cwd=ROOT)
                subprocess.run(['tar','-x','-C',directory],input=archive,check=True)
                subprocess.run(['docker','build','--build-arg','CHECKPOINT_REVISION='+application_ref,
                                '-t','camadmiral-e2e:local',directory],check=True)
        else:
            application_ref=result['source']
            run('build','camadmiral')
        result['application_revision']=application_ref
        result['application_tree']=subprocess.check_output(['git','rev-parse',application_ref+':camadmiral'],cwd=ROOT,text=True).strip()
        run('up','-d','camera-open','stalled-camera','camadmiral','frigate')
        stage('setup')
        run('run','-d','--name','camadmiral-stall-audio','--entrypoint','python','test-driver',
            '/e2e/stall_recovery.py','hold-audio')
        time.sleep(10)
        stage('verify-initial')
        deadline=time.monotonic()+100
        while True:
            rows=recordings('stall-before.json')
            if len({r[1] for r in rows if r[4]-r[3]>=2})==2:break
            assert time.monotonic()<deadline,'Initial complete recordings missing'
            time.sleep(3)
        before={'camadmiral':state('camadmiral'),'frigate':state('frigate'),'relay_pid':pid()}
        since=time.time()
        run('run','--rm','--entrypoint','python','test-driver','-c',
            'from pathlib import Path; Path("/state/stall-clock.json").write_text('+repr(json.dumps({'since':since}))+')')
        stage('arm')
        # Longer than the stock socket timeout. Live audio must keep this
        # producer alive; the application watchdog must observe video itself.
        time.sleep(12)
        fault=json.loads(run('exec','-T','stalled-camera','python','-c',
            "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8080/stream-stats').read().decode())"))
        assert fault['forwarded_audio_packets']>0,'No audio flowed during the video stall'
        assert fault['frozen_video_packets']>0,'Video fault never activated'
        assert fault['frozen_video_sessions']>0,'Frozen sessions reconnected before watchdog recovery'
        stage('verify')
        deadline=time.monotonic()+100
        while True:
            rows=recordings('stall-after.json')
            if len({r[1] for r in rows if r[3]>since and r[4]-r[3]>=2})==2:break
            assert time.monotonic()<deadline,'Fresh complete recordings missing'
            time.sleep(3)
        stage('playback')
        after={'camadmiral':state('camadmiral'),'frigate':state('frigate'),'relay_pid':pid()}
        assert before['camadmiral']==after['camadmiral'],'CamAdmiral container restarted'
        assert before['frigate']==after['frigate'],'Frigate container restarted'
        proxy=json.loads(run('exec','-T','stalled-camera','python','-c',
            "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8080/stream-stats').read().decode())"))
        assert proxy['frozen_video_sessions']==0,'Old stuck sessions survived recovery'
        logs=run('logs','--no-color','camadmiral')
        assert logs.count('"event": "restart_started"')==1,'Recovery reset was missing or repeated'
        assert '"event": "recovery_verified"' in logs,'Recovery was not verified with a decoded frame'
        run('stop','-t','3','stalled-camera')
        time.sleep(25)
        stage('verify-unaffected')
        offline_logs=run('logs','--no-color','camadmiral')
        assert offline_logs.count('"event": "restart_started"')==1,'An offline camera caused another shared reset'
        run('start','stalled-camera')
        stage('verify')
        assert before['frigate']==state('frigate'),'Offline recovery restarted the recorder'
        result.update(status='passed',before=before,after=after,fault=fault,offline_camera_did_not_reset_relay=True)
        print('PASS: same-address video stall recovered without container restarts or data changes',flush=True)
    except Exception as exc:
        result.update(status='failed',error=type(exc).__name__+': '+str(exc))
        raise
    finally:
        (artifacts/'result.json').write_text(json.dumps(result,indent=2))
        (artifacts/'container.log').write_text(run('logs','--no-color','camadmiral',check=False))
        (artifacts/'proxy.log').write_text(run('logs','--no-color','stalled-camera',check=False))
        (artifacts/'recorder.log').write_text(run('logs','--no-color','frigate',check=False))
        run('down','--volumes','--remove-orphans',check=False)


if __name__=='__main__':
    driver(sys.argv[1]) if sys.argv[1:] else host()

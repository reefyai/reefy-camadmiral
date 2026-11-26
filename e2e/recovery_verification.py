"""Address recovery across real camera/relay sockets, including a baseline control."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
STATE = Path('/var/lib/camadmiral/recovery-verification.json')


def get(url, body=None):
    request=urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None,
                                  headers={'Content-Type':'application/json'})
    with urllib.request.urlopen(request,timeout=5) as response:return json.load(response)


def repository():
    from camadmiral.crypto import load_master_key
    from camadmiral.storage import CameraRepository
    return CameraRepository(Path('/var/lib/camadmiral/camadmiral.db'),load_master_key())


def inventory(address):
    payload={'scan_id':'synthetic-recovery-verification','devices':[{
        'candidate_uuid':'synthetic-recovery-camera','display_name':'Synthetic entrance',
        'ip':address,'mac':'02:00:00:00:05:10','status':'online'}]}
    path=Path('/var/lib/camadmiral/inventory.json');temporary=path.with_suffix('.new')
    temporary.write_text(json.dumps(payload));temporary.replace(path)


def signature(adoption):
    return {'camera_uuid':adoption['camera_uuid'],'roles':adoption['roles'],
            'streams':[[s['stream_uuid'],s['stream_key'],s['enabled']] for s in adoption['streams']]}


def stage(name,case):
    from camadmiral.media import ProbeResult, reconcile_and_probe
    repo=repository()
    if name=='seed':
        from camadmiral.media import probe_source
        profiles=[]
        for i,profile in enumerate(('main','sub','spare')):
            uri='rtsp://172.27.5.10:8554/'+profile
            result=probe_source(uri,'operator','synthetic-source-secret')
            assert result.status=='ready','Synthetic source did not provide valid video'
            profiles.append({'token':profile,'name':profile,'uri':uri,
                'source_kind':'manual_rtsp','width':result.width,'height':result.height,
                'encoding':'H264','fps':result.fps,'bitrate_kbps':0})
        inventory('172.27.5.10')
        before=repo.adopt({'candidate_uuid':'synthetic-recovery-camera','display_name':'Synthetic entrance'},
                          'operator','synthetic-source-secret',profiles,{'record':'main','detect':'sub'})
        reconcile_and_probe(repo)
        initial = repo.adoption_for_candidate('synthetic-recovery-camera')
        assert all(s['health_status']=='healthy' for s in initial['streams']), 'Initial relay media is not ready'
        with repo.connect() as connection:
            initial_revision = connection.execute('SELECT MAX(revision_id) FROM media_config_revisions').fetchone()[0]
        # A real encoded file must survive both successful and failed cutovers.
        media=Path('/var/lib/camadmiral/retained-synthetic.mp4')
        result=subprocess.run(['ffmpeg','-v','error','-rtsp_transport','tcp','-i',
            'rtsp://operator:synthetic-source-secret@172.27.5.10:8554/main',
            '-an','-frames:v','15','-c:v','copy','-y',str(media)],capture_output=True,timeout=25)
        assert result.returncode==0 and media.stat().st_size>1000,'Synthetic retained recording missing'
        repo.save_telegram_settings(enabled=True,bot_token='123456:synthetic-bot-token-value',
            bot_id='123456',bot_username='synthetic_alert_bot',pairing_token='synthetic-pairing',
            pairing_expires_at='2099-01-01T00:00:00+00:00')
        repo.complete_telegram_pairing(chat_id='100200300',chat_label='Synthetic operator',update_offset=1)
        STATE.write_text(json.dumps({'before':signature(before),'source_uris':[s['uri'] for s in before['streams']],
            'initial_revision':initial_revision,'media_sha256':hashlib.sha256(media.read_bytes()).hexdigest(),'case':case}))
        print('PASS: three enabled profiles, stable identities and real retained video seeded',flush=True)
        return
    state=json.loads(STATE.read_text());before=state['before'];camera_uuid=before['camera_uuid']
    if name=='begin':
        for _ in range(3):
            repo.record_probe_results({s[0]:ProbeResult('unavailable',10) for s in before['streams']})
        assert repo.incidents(status='open')['open_count']==1,'Offline incident fixture missing'
        inventory('172.27.5.12')
        print('PASS: verified inventory moved; normal background worker owns recovery',flush=True)
        return
    if name=='verify':
        deadline=time.monotonic()+65
        observed={}
        while time.monotonic()<deadline:
            after=repo.adoption_for_candidate('synthetic-recovery-camera')
            with repo.connect() as connection:
                revisions=[dict(r) for r in connection.execute(
                    'SELECT status,error_code FROM media_config_revisions WHERE revision_id>? ORDER BY revision_id',
                    (state['initial_revision'],))]
                addresses=[dict(r) for r in connection.execute('SELECT previous_address,current_address FROM camera_address_events')]
                notices=[dict(r) for r in connection.execute(
                    "SELECT event_type,payload_json FROM notification_outbox WHERE event_type='incident_resolved'")]
            fault=get('http://127.0.0.1:1986/')
            moved=all('172.27.5.12' in s['uri'] for s in after['streams'])
            failed=any(r['status']=='failed' for r in revisions)
            # A failed revision precedes per-profile source restoration and
            # the rollback restart. Observe the completed rollback, rather
            # than asserting against that legitimate intermediate state.
            rolled_back=(failed and fault['restarts']==2
                         and [s['uri'] for s in after['streams']]==state['source_uris'])
            if case=='transient' and (moved and addresses or rolled_back):break
            if case=='persistent' and rolled_back:break
            if case=='auth' and time.monotonic()>deadline-50:break
            time.sleep(.25)
        observed={'case':case,'revisions':revisions,'addresses':addresses,'fault':fault,
                  'identity_preserved':signature(after)==before,'sources_preserved':[s['uri'] for s in after['streams']]==state['source_uris'],
                  'moved':moved,'resolved_notifications':[json.loads(n['payload_json'])['kind'] for n in notices],
                  'incidents':repo.incidents(status='all')['incidents'],
                  'media_preserved':hashlib.sha256(Path('/var/lib/camadmiral/retained-synthetic.mp4').read_bytes()).hexdigest()==state['media_sha256']}
        Path('/var/lib/camadmiral/recovery-result.json').write_text(json.dumps(observed))
        if case=='transient' and failed:
            observed['failure']='premature_rollback'
            Path('/var/lib/camadmiral/recovery-result.json').write_text(json.dumps(observed));print(json.dumps(observed),flush=True)
            raise AssertionError('One transient relay timeout rolled back a validated source')
        assert observed['identity_preserved'] and observed['media_preserved'],'Identity or recording changed'
        if case=='transient':
            assert moved and addresses and fault['restarts']==1,'Recovery restarted or did not commit'
            assert fault['faults']>=3,'Fault did not cover all three enabled profiles'
            assert observed['resolved_notifications']==['camera_address_changed'],'Duplicate recovery notices'
            assert {x['kind'] for x in observed['incidents']}=={'media_offline','camera_address_changed'}
            assert all(x['resolved_at'] for x in observed['incidents']),'Both histories must resolve'
        elif case=='persistent':
            assert not moved and not addresses and fault['restarts']==2,'Failed verification did not roll back exactly once'
            assert observed['sources_preserved'] and not notices,'Failure changed sources or claimed recovery'
        else:
            assert not moved and not addresses and fault['restarts']==0,'Rejected credentials changed runtime'
            assert observed['sources_preserved'] and not notices
        Path('/var/lib/camadmiral/recovery-result.json').write_text(json.dumps(observed));print(json.dumps(observed),flush=True)
        if case=='transient':
            password=repo.rtsp_access_password()
            for stream in after['streams']:
                url='rtsp://camadmiral:'+password+'@127.0.0.1:18555/'+stream['stream_key']
                result=subprocess.run(['ffmpeg','-v','error','-threads','1','-rtsp_transport','tcp',
                    '-i',url,'-an','-frames:v','5','-f','framemd5','-'],capture_output=True,timeout=30)
                frames=[x for x in result.stdout.splitlines() if x and not x.startswith(b'#')]
                assert result.returncode==0 and len(frames)==5,'Recovered enabled profile did not decode five frames'
            print('PASS: every enabled profile decoded five actual frames; one notification and one restart',flush=True)
        return
    raise ValueError(name)


def host():
    artifacts=ROOT/'e2e-artifacts/recovery-verification';artifacts.mkdir(parents=True,exist_ok=True)
    baseline=os.environ.get('RECOVERY_BASELINE_REF','')
    assert len(baseline)==40,'Exact immutable baseline source is required'
    fixed=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()
    provenance={'baseline':baseline,'fixed':fixed,'cases':[]}
    compose=['docker','compose','-p','camadmiral-recovery-verification','-f',str(ROOT/'e2e/recovery-verification-compose.yaml')]
    env=os.environ.copy()
    def run(*args,check=True):
        p=subprocess.run([*compose,*args],cwd=ROOT,env=env,text=True,stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
        with (artifacts/'workload.log').open('a') as f:f.write(p.stdout)
        if check and p.returncode:print(p.stdout,flush=True);p.check_returncode()
        return p
    with tempfile.TemporaryDirectory(prefix='recovery-baseline-') as tmp:
        archive=subprocess.Popen(['git','archive',baseline],cwd=ROOT,stdout=subprocess.PIPE)
        subprocess.run(['tar','-xf','-','-C',tmp],stdin=archive.stdout,check=True);archive.stdout.close();assert archive.wait()==0
        for label,context,source in (('baseline',tmp,baseline),('fixed',str(ROOT),fixed)):
            image='camadmiral-recovery-verification:'+label;env['RECOVERY_TEST_IMAGE']=image
            sys.path.insert(0,str(ROOT))
            from scripts.ci_image_retry import literal_build_images, pull_image
            for base in literal_build_images({'context':context}):
                exists=subprocess.run(['docker','image','inspect',base],capture_output=True).returncode==0
                if not exists:pull_image(['docker'],base)
            subprocess.run(['docker','build','--build-arg','CHECKPOINT_REVISION='+source,
                            '-t',image,context],cwd=ROOT,check=True)
            for case in ('transient','persistent','auth'):
                env['RECOVERY_NEW_CAMERA_CONFIG'] = '/e2e/fixtures/camera-recovery-auth-rejected.yaml' if case == 'auth' else '/e2e/fixtures/camera-recovery-verification.yaml'
                try:
                    run('up','-d','camera-old','camera-new','camadmiral','relay-proxy')
                    deadline=time.monotonic()+90
                    while True:
                        try:
                            readiness=run('exec','-T','camadmiral','python','-c',
                                'import json,urllib.request;print(json.load(urllib.request.urlopen("http://127.0.0.1:18080/healthz",timeout=5))["status"])',check=False)
                            if readiness.returncode==0 and readiness.stdout.strip()=='healthy':break
                        except Exception:pass
                        if time.monotonic()>=deadline:raise RuntimeError('CamAdmiral did not become healthy')
                        time.sleep(.5)
                    run('exec','-T','camadmiral','python','/e2e/recovery_verification.py','seed',case)
                    mode=case if case!='auth' else 'none'
                    run('exec','-T','camadmiral','python','-c',
                        'import urllib.request,json;urllib.request.urlopen(urllib.request.Request("http://127.0.0.1:1986/",data=json.dumps({"mode":"'+mode+'"}).encode(),headers={"Content-Type":"application/json"})).read()')
                    run('exec','-T','camadmiral','python','/e2e/recovery_verification.py','begin',case)
                    verification=run('exec','-T','camadmiral','python','/e2e/recovery_verification.py','verify',case,check=False)
                    payload=json.loads(run('exec','-T','camadmiral','cat','/var/lib/camadmiral/recovery-result.json').stdout)
                    if label=='baseline' and case=='transient':
                        assert verification.returncode!=0 and payload.get('failure')=='premature_rollback','Baseline did not reproduce target failure'
                        assert payload['fault']['faults']>=3 and payload['fault']['restarts']==2,'Baseline failed outside injected relay verification'
                        assert payload['identity_preserved'] and payload['sources_preserved'] and payload['media_preserved']
                    else:assert verification.returncode==0,'Fixed or negative-control verification failed'
                    provenance['cases'].append({'variant':label,**payload})
                finally:
                    logs=run('logs','--no-color','camadmiral',check=False).stdout
                    (artifacts/(label+'-'+case+'.log')).write_text(logs)
                    if label=='fixed' and case=='transient':
                        events=[json.loads(x.split('media-recovery: ',1)[1]) for x in logs.splitlines() if 'media-recovery: ' in x]
                        assert any(x.get('event')=='verification_retry' for x in events),'Retry telemetry missing'
                        assert len({x['stream_uuid'] for x in events if x.get('event')=='probe' and x.get('stage')=='relay'})==3
                        assert not any('synthetic-source-secret' in json.dumps(x) or 'rtsp://' in json.dumps(x) for x in events),'Telemetry leaked a source'
                    run('down','--volumes','--remove-orphans',check=False)
                    (artifacts/'result.json').write_text(json.dumps(provenance,indent=2))
    print('PASS: baseline reproduced rollback; fixed transient, permanent and credential controls passed',flush=True)


if __name__=='__main__':
    if len(sys.argv)>1:stage(sys.argv[1],sys.argv[2])
    else:host()

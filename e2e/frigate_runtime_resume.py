"""Real Frigate regression for an owned runtime pause surviving native restart."""
from __future__ import annotations
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
STATE = Path('/state/runtime-resume.json')

def driver(stage):
    sys.path.insert(0, "/opt/camadmiral")
    import yaml
    from scenarios import request_json, wait_for, wait_for_health, consumer_directory
    from camadmiral.frigate import FrigateClient, FrigateTarget
    client = FrigateClient(FrigateTarget('synthetic-runtime','Synthetic target','http://frigate:5000'))
    def save(): STATE.write_text(json.dumps(state))
    def running():
        conf=client.config()['cameras'];stats=client.stats()
        return all(conf[k]['enabled'] and stats[k]['camera_fps']>0 for k in ('managed_camera','healthy_peer','operator_peer'))
    def exercise(**options):
        return request_json('/__e2e/runtime-resume',method='POST',
            headers={'X-CamAdmiral-Action':'synthetic-runtime-resume'},payload=options,timeout=420)
    wait_for_health()
    wait_for('real Frigate ready',lambda: client.stats(),timeout=180)
    if stage=='prepare':
        wait_for('original synthetic video',running,timeout=180)
        request_json('/internal/frigate-targets',method='POST',expected=201,
            headers={'X-CamAdmiral-Action':'add-frigate-target'},
            payload={'name':'Synthetic runtime target','api_url':'http://frigate:5000'})
        tid=request_json('/internal/frigate-targets')['targets'][0]['target_id']
        base='/internal/frigate-targets/'+tid
        preview=request_json(base+'/import')
        row=next(x for x in preview['cameras'] if x['key']=='managed_camera')
        assert row['can_import'],row
        request_json(base+'/import/'+preview['id'],method='POST',expected=202,
            headers={'X-CamAdmiral-Action':'import-frigate-cameras'},
            payload={'selected':['managed_camera'],'allow_restart':True})
        job=wait_for('normal camera import',lambda:(j if (j:=request_json(base+'/import/'+preview['id']))['status']=='completed' else False),timeout=480)
        assert next(x for x in job['cameras'] if x['key']=='managed_camera')['status']=='applied',job
        wait_for('imported synthetic video',running,timeout=180)
        state={'saved':client.raw_config(),'identities':[(c['id'],[(s['id'],s['roles']) for s in c['streams']]) for c in consumer_directory()['cameras']],'results':{}}
        save();return
    state=json.loads(STATE.read_text())
    if stage=='guard':
        client.set_runtime_camera_enabled('operator_peer',False)
        result=exercise()
        assert result['status']=='error' and result['code']=='import_source_conflict',result
        assert result['restart_requests']==0 and result['enabled'],result
        assert client.config()['cameras']['operator_peer']['enabled'] is False,'Operator pause was discarded'
        assert not client.config()['cameras']['saved_disabled']['enabled']
        assert client.raw_config()==state['saved']
        state['results'][stage]=result;save()
        client.set_runtime_camera_enabled('operator_peer',True)
    elif stage=='recover':
        result=exercise()
        state['results'][stage]=result;save()
        assert result.get('off_after_restart'),'Fixture failed to preserve the runtime OFF override'
        assert result['status']=='recovered','OWNED_RUNTIME_PAUSE_NOT_RESUMED: '+json.dumps(result)
        assert result['restart_requests']==1 and result['enabled'] and result['fps']>0,result
        assert not result['capture_resume_pending'] and not result['target_restart_pending'],result
        assert result['raw_config_preserved'] and client.raw_config()==state['saved']
        assert not client.config()['cameras']['saved_disabled']['enabled']
        wait_for('all active peers resumed',running,timeout=180)
    elif stage=='interrupt':
        state['started_before_interrupt']=client.service_started_at();save()
        try: exercise(interrupt=True)
        except Exception as exc:
            # The synthetic control process exits after the accepted native
            # restart and sticky OFF state, before its caller can resume it.
            state['interrupt_client_error']=type(exc).__name__;save();return
        raise AssertionError('Control-process interruption was not injected')
    elif stage=='after-interrupt':
        wait_for('owned capture resumed after control-process loss',running,timeout=240)
        wait_for('owned recovery journal cleared',lambda: not any(request_json('/__e2e/runtime-resume-state').values()),timeout=240)
        assert client.service_started_at()>state['started_before_interrupt']+2
        current=client.service_started_at();time.sleep(3)
        assert abs(client.service_started_at()-current)<2,'Recovery dispatched another Frigate restart'
        assert client.raw_config()==state['saved']
        assert not client.config()['cameras']['saved_disabled']['enabled']
        current_ids=[(c['id'],[(s['id'],s['roles']) for s in c['streams']]) for c in consumer_directory()['cameras']]
        assert json.loads(json.dumps(current_ids))==state['identities'],'Camera or stream identities changed'
        state['results'][stage]={'enabled':True,'fps':client.stats()['managed_camera']['camera_fps'],'frigate_started':current,'journal_cleared':True};save()
    elif stage=='playback':
        from frigate_identity import recording_for_playback,recording_playback_url
        before=json.loads(Path('/state/runtime-before.json').read_text());rows=json.loads(Path('/state/runtime-after.json').read_text())
        assert {x[0] for x in before}<={x[0] for x in rows},'Historical recordings disappeared'
        decoded={}
        for label,rec in [('old',recording_for_playback(before,'managed_camera')),('fresh',recording_for_playback(rows,'managed_camera',since=state['fresh_since']))]:
            assert rec,'Missing '+label+' recording'
            path=Path('/state/runtime-'+label+'.mp4');path.write_bytes(urllib.request.urlopen(recording_playback_url(rec,rows),timeout=30).read())
            p=subprocess.run(['ffmpeg','-v','error','-threads','1','-i',str(path),'-an','-frames:v','5','-f','framemd5','-'],capture_output=True,timeout=30)
            frames=sum(bool(l.strip()) and not l.startswith(b'#') for l in p.stdout.splitlines())
            assert p.returncode==0 and frames>=5,'Old/fresh recorded video did not decode'
            decoded[label]=frames
        assert abs(client.service_started_at()-state['results']['after-interrupt']['frigate_started'])<2,'Recovery repeated a Frigate restart during recording verification'
        state['results'][stage]=decoded;save()
    else: raise ValueError(stage)


def host():
    from frigate_identity import recording_for_playback
    version=os.environ.get('FRIGATE_TEST_VERSION','0.18.2');assert version in ('0.17.2','0.18.2')
    env=dict(os.environ,FRIGATE_TEST_IMAGE='ghcr.io/blakeblackshear/frigate:'+version)
    artifacts=ROOT/'e2e-artifacts/frigate-runtime-resume'/version;artifacts.mkdir(parents=True,exist_ok=True)
    compose=['docker','compose','-p','camadmiral-runtime-'+version.replace('.','-'),'-f',str(ROOT/'e2e/compose.yaml'),'-f',str(ROOT/'e2e/recording-compose.yaml'),'-f',str(ROOT/'e2e/frigate-runtime-resume-compose.yaml')]
    result={'status':'failed','version':version,'source':subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip()}
    def run(*args,check=True):
        p=subprocess.run([*compose,*args],env=env,text=True,stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
        with (artifacts/'transcript.log').open('a') as f:f.write(p.stdout)
        if check and p.returncode:raise RuntimeError(p.stdout[-4000:])
        return p.stdout
    def stage(name):
        print('Runtime-resume stage: '+name,flush=True)
        run('run','--rm','--entrypoint','python','test-driver','/e2e/frigate_runtime_resume.py',name)
    def recordings():
        code="import json,sqlite3;from pathlib import Path;d=sqlite3.connect('file:/config/frigate.db?mode=ro',uri=True);print(json.dumps([list(r) for r in d.execute('select id,camera,path,start_time,end_time from recordings order by start_time') if Path(r[2]).is_file() and r[4]-r[3]>=2]))"
        return json.loads(run('exec','-T','frigate','python3','-c',code))
    def shared(name,value):
        run('run','--rm','--entrypoint','python','test-driver','-c','from pathlib import Path;Path('+repr('/state/'+name)+').write_text('+repr(json.dumps(value))+')')
    try:
        run('down','--volumes','--remove-orphans');run('build','camadmiral')
        run('up','-d','camera-open','camadmiral','frigate')
        stage('prepare')
        deadline=time.monotonic()+180
        while time.monotonic()<deadline:
            before=recordings()
            if recording_for_playback(before,'managed_camera'):break
            time.sleep(3)
        else:raise AssertionError('No original real recording before recovery')
        shared('runtime-before.json',before)
        stage('guard');stage('recover');stage('interrupt')
        deadline=time.monotonic()+30
        while time.monotonic()<deadline:
            state=json.loads(run('ps','-a','--format','json','camadmiral'))
            if state['State']=='exited':break
            time.sleep(1)
        else:raise AssertionError('Synthetic control interruption did not stop its supervisor')
        run('cp','camadmiral:/var/lib/camadmiral/runtime-resume-evidence.json',str(artifacts/'interrupted.json'))
        interrupted=json.loads((artifacts/'interrupted.json').read_text())
        assert interrupted['restart_requests']==1 and interrupted['off_after_restart'] and interrupted['interrupted']
        run('restart','camadmiral');stage('after-interrupt')
        since=time.time()
        run('run','--rm','--entrypoint','python','test-driver','-c',"import json;from pathlib import Path;p=Path('/state/runtime-resume.json');x=json.loads(p.read_text());x['fresh_since']="+repr(since)+";p.write_text(json.dumps(x))")
        deadline=time.monotonic()+180
        while time.monotonic()<deadline:
            after=recordings()
            if recording_for_playback(after,'managed_camera',since=since):break
            time.sleep(3)
        else:raise AssertionError('No new complete recording after recovery')
        shared('runtime-after.json',after);stage('playback')
        result['status']='passed'
    finally:
        run('create','test-driver',check=False)
        run('cp','test-driver:'+str(STATE),str(artifacts/'driver.json'),check=False)
        if (artifacts/'driver.json').exists():result['driver']=json.loads((artifacts/'driver.json').read_text())
        (artifacts/'result.json').write_text(json.dumps(result,indent=2))
        (artifacts/'containers.log').write_text(run('logs','--no-color',check=False))
        run('down','--volumes','--remove-orphans',check=False)

if __name__=='__main__':
    driver(sys.argv[1]) if len(sys.argv)>1 else host()

"""Owned host refresh against real Frigate while a peer's recovery is pending.

Runs only inside the established synthetic batch lab. The API process is paused
while the fault is installed; the scanner and relay remain live. No production
endpoint, customer fixture or restart permission is involved.
"""
import copy
import json
import os
from pathlib import Path
import signal
import sys
import urllib.parse
from unittest.mock import patch

sys.path.insert(0, '/opt/camadmiral')

from camadmiral.config import database_path
from camadmiral.crypto import load_master_key
from camadmiral.storage import CameraRepository
from camadmiral.frigate import FrigateClient, FrigateApiError, load_frigate_targets
from camadmiral.frigate_import import ImportStore, digest, sync_imported


def run():
    api = next(int(p.name) for p in Path('/proc').iterdir() if p.name.isdigit()
               and b'uvicorn' in (p / 'cmdline').read_bytes())
    os.kill(api, signal.SIGSTOP)
    try:
        return exercise()
    finally:
        os.kill(api, signal.SIGCONT)


def exercise():
    repo = CameraRepository(database_path(), load_master_key())
    target = load_frigate_targets(repo)[0]
    client = FrigateClient(target)
    store = ImportStore(repo)
    links = {k: r for k, r in store.links(target.target_id).items() if r.get('state') == 'active'}
    assert len(links) >= 2, 'Host refresh needs multiple imported peers'
    trigger_key = next(iter(links))
    prior_pending = copy.deepcopy(links[trigger_key].get('target_restart'))
    before = client.raw_config()
    old_host = '192.0.2.200'
    source = next(iter(next(iter(links.values()))['managed_streams'].values()))[0]
    current_host = urllib.parse.urlsplit(source).hostname
    def retarget(url):
        p = urllib.parse.urlsplit(url)
        return urllib.parse.urlunsplit(p._replace(netloc=p.netloc.replace(p.hostname, old_host)))
    updates = {alias: [retarget(u) for u in values] for row in links.values()
               for alias, values in row['managed_streams'].items()}
    result = {'status': 'failed', 'peers': sorted(links), 'source_host': current_host}
    try:
        client.set_config({'go2rtc': {'streams': updates}})
        for key, row in links.items():
            row['managed_streams'] = {alias: updates[alias] for alias in row['managed_streams']}
            for alias, values in row['managed_streams'].items():
                client.set_runtime_stream(alias, values[0])
            store.save_link(target.target_id, key, row)
        trigger = store.links(target.target_id)[trigger_key]
        trigger['target_restart'] = {'raw_hash': digest(client.raw_config())}
        store.save_link(target.target_id, trigger_key, trigger)
        observed = []
        def blocked(*args, **kwargs):
            actual = client.raw_config()
            for row in store.links(target.target_id).values():
                for alias, values in row['managed_streams'].items():
                    assert all(urllib.parse.urlsplit(u).hostname == current_host for u in values), 'HOST_REFRESH_STARVED'
                    assert actual['go2rtc']['streams'][alias] == values
            assert actual['cameras'] == before['cameras'], 'Operator camera configuration changed'
            assert actual.get('camera_groups') == before.get('camera_groups'), 'Camera groups changed'
            assert store.links(target.target_id)[trigger_key]['target_restart']['raw_hash'] == digest(actual)
            observed.append(True)
            raise FrigateApiError('capture_verification_failed')
        with patch('camadmiral.frigate_import._restart_imported_target', side_effect=blocked):
            sync_imported(repo, target, client=client, host_resolver=lambda mode: current_host)
        assert observed, 'Pending recovery was not exercised'
        result.update(status='passed', guarded_pending_recovery=True, configuration_preserved=True)
    finally:
        # Remove only our artificial pending marker. On failure, put source
        # ownership and config back before allowing normal lab recovery.
        if result['status'] != 'passed':
            client.set_config({'go2rtc': {'streams': before['go2rtc']['streams']}})
            for key, original in links.items():
                original['managed_streams'] = {a: before['go2rtc']['streams'][a] for a in original['managed_streams']}
                store.save_link(target.target_id, key, original)
        trigger = store.links(target.target_id)[trigger_key]
        if prior_pending is None:
            trigger.pop('target_restart', None)
        else:
            trigger['target_restart'] = prior_pending
        store.save_link(target.target_id, trigger_key, trigger)
        print(json.dumps(result), flush=True)
    return result


if __name__ == '__main__':
    run()

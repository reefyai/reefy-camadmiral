"""Verified physical identities for direct RTSP cameras, independent of their IDs."""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import ipaddress
import json
from pathlib import Path
import time
import urllib.parse
import uuid

from .discovery import scan_explicit_address

LEARN_ATTEMPTS: dict[str, float] = {}


def physical_identity(candidate):
    return (str((candidate.get('onvif') or {}).get('endpoint_reference') or '').strip().lower(),
            str(candidate.get('mac') or '').strip().lower())


def same_identity(expected, observed):
    endpoint, mac = physical_identity(expected)
    new_endpoint, new_mac = physical_identity(observed)
    if endpoint and new_endpoint:
        return endpoint == new_endpoint
    return bool(mac and new_mac and mac == new_mac)


def inventory_candidates(path):
    try:
        data = json.loads(Path(path).read_text())
        return {str(d['candidate_uuid']): d for d in data.get('devices', []) if d.get('candidate_uuid')}
    except (OSError, ValueError, TypeError):
        return {}


def discover_identity(address, inventory_path):
    """Probe only the selected address; cached IP alone never establishes identity."""
    try:
        ip = ipaddress.ip_address(address)
        if ip.version != 4 or not ip.is_private or ip.is_loopback or ip.is_link_local:
            return None
        observations = scan_explicit_address(str(ip)).get('devices', [])
    except (OSError, RuntimeError, ValueError):
        return None
    candidates = [d for d in observations if d.get('ip') == str(ip) and any(physical_identity(d)) and not d.get('identity_conflict')]
    if len(candidates) != 1:
        return None
    candidate = copy.deepcopy(candidates[0])
    previous = [d for d in inventory_candidates(inventory_path).values() if same_identity(candidate, d)]
    if len(previous) > 1 or any(d.get('identity_conflict') for d in previous):
        return None
    endpoint, mac = physical_identity(candidate)
    physical_key = 'onvif:' + endpoint if endpoint else 'mac:' + mac
    candidate.update(candidate_uuid=previous[0]['candidate_uuid'] if previous else str(uuid.uuid5(uuid.NAMESPACE_URL, physical_key)),
                     status='online', last_seen=datetime.now(timezone.utc).isoformat())
    if candidate.get('onvif'):
        services = []
        for url in candidate['onvif'].get('service_urls', []):
            parsed = urllib.parse.urlsplit(url)
            if parsed.hostname == str(ip) and parsed.scheme in {'http', 'https'} and not parsed.username and not parsed.password:
                services.append(url)
        candidate['onvif'] = {'endpoint_reference': endpoint, 'service_urls': services}
    # Persist discovery metadata only, never stream URLs or credentials.
    return {k: candidate[k] for k in ('candidate_uuid', 'ip', 'mac', 'onvif', 'rtsp', 'status', 'last_seen') if k in candidate}


def linked_candidate(adoption, candidates, links, candidate_uuid=None):
    """Resolve shared physical identity without merging logical cameras/channels."""
    saved = links.get(str(adoption['camera_uuid']))
    if not saved:
        return candidates.get(str(candidate_uuid or adoption.get('candidate_uuid')))
    matches = [d for d in candidates.values() if same_identity(saved, d)]
    if len(matches) > 1 or any(d.get('identity_conflict') for d in matches):
        return None
    if matches:
        current = matches[0]
        if str(current.get('last_service_seen') or current.get('last_seen') or '') >= str(saved.get('last_seen') or ''):
            return current
    # A freshly confirmed link may be newer than the discovery cache. Its
    # physical keys also let targeted discovery recover after cache loss.
    return saved


def learn_import_identity(repository, camera_uuid, inventory_path):
    if inventory_path is None or camera_uuid in repository.camera_identity_links():
        return False
    sources = repository.managed_stream_sources(include_disabled=True, camera_uuid=camera_uuid)
    hosts = {urllib.parse.urlsplit(s['uri']).hostname for s in sources}
    if len(hosts) != 1:
        return False
    candidate = discover_identity(next(iter(hosts)) or '', inventory_path)
    if candidate is None:
        return False
    # Discovery can fill its cache between channel imports. Keep the verified
    # physical anchor already learned by a peer, independent of cache timing.
    peers = [link for link in repository.camera_identity_links().values()
             if same_identity(link, candidate)]
    peer_ids = {link.get('candidate_uuid') for link in peers}
    if peers:
        if len(peer_ids) != 1 or not next(iter(peer_ids)) or any(link.get('identity_conflict') for link in peers):
            return False
        candidate['candidate_uuid'] = next(iter(peer_ids))
    repository.bind_camera_identity(camera_uuid, candidate)
    return True


def enrich_existing_imports(repository, inventory_path):
    """Upgrade working older imports; unavailable unanchored sources need review."""
    links = repository.camera_identity_links()
    with repository.connect() as connection:
        imported = {str(row[0]) for row in connection.execute('SELECT camera_uuid FROM frigate_import_links')}
    now = time.monotonic()
    for candidate_uuid, adoption in repository.adoption_map().items():
        camera_uuid = str(adoption['camera_uuid'])
        if (not candidate_uuid.startswith('frigate-import:') and camera_uuid not in imported) or not adoption.get('enabled', True) or camera_uuid in links:
            continue
        streams = [s for s in adoption.get('streams', []) if s.get('enabled', True)]
        if not streams or any(s.get('health_status') != 'healthy' for s in streams):
            continue
        # Legacy imports without a prior physical observation cannot be safely
        # anchored from a reused IP. The operator must identify/confirm them.
        history = repository.camera_identity_history(camera_uuid) or []
        known = next((h for h in history if h.get('current') and (h.get('onvif_identity') or h.get('mac'))), None)
        if not known:
            continue
        if now - LEARN_ATTEMPTS.get(camera_uuid, float('-inf')) < 300:
            continue
        LEARN_ATTEMPTS[camera_uuid] = now
        try:
            sources = repository.managed_stream_sources(camera_uuid=camera_uuid)
            hosts = {urllib.parse.urlsplit(s['uri']).hostname for s in sources}
            candidate = discover_identity(next(iter(hosts)) or '', inventory_path) if len(hosts) == 1 else None
            expected = {'mac': known.get('mac'), 'onvif': {'endpoint_reference': known.get('onvif_identity')}}
            if candidate and same_identity(expected, candidate):
                repository.bind_camera_identity(camera_uuid, candidate)
        except (OSError, RuntimeError, ValueError):
            pass
        return  # Bound background work to one camera per cycle.

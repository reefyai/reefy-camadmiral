# CamAdmiral E2E lab

This suite exercises the built CamAdmiral container only through its HTTP,
RTSP, process, and persistent-volume boundaries. It does not import
CamAdmiral application modules.

The isolated Docker Compose lab covers:

- explicit IP discovery and adoption through a synthetic ONVIF camera
- unauthenticated and authenticated RTSP adoption
- incorrect camera-credential rejection
- empirical H.264 metadata and automatic recording/detection role selection
- snapshots and authenticated stable downstream RTSP URLs
- two downstream consumers sharing one physical-camera session
- reversible camera disable and enable
- out-of-band managed-stream deletion and automatic runtime-drift repair
- go2rtc child failure without a CamAdmiral container restart
- synthetic camera outage across a CamAdmiral restart and recovery without user action
- persisted availability buckets across camera outage and recovery
- complete CamAdmiral container restart with stable IDs, paths, and secrets
- real Frigate 0.17 camera injection, runtime creation, and frame processing
- invalid recovered-address rejection with last-known-good media preservation
- camera IP change with validated upstream replacement and stable downstream
  identities
- camera credential rotation, failed repair preservation, and successful repair

Run from the repository root:

```console
python3 e2e/run.py
```

Docker with Compose v2 is the only host dependency. The runner builds the
current source, creates a private bridge network and disposable volumes, and
removes them when complete. It does not publish ports, scan the host LAN, mount
the Docker socket into a container, or use host network capabilities.

Set `CAMADMIRAL_E2E_KEEP=1` to retain a failed lab for manual inspection. Remove
it afterward with:

```console
docker compose --project-name camadmiral-e2e --file e2e/compose.yaml \
  --profile moved --profile rotated down --volumes --remove-orphans
```

Fast algorithm, parsing, storage, crypto, adapter, and HTTP-boundary tests stay
under `tests/`. They may use mocks to isolate a single behavior. Real synthetic
media and multi-process failure workflows belong here.

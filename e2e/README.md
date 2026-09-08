# CamAdmiral E2E lab

This suite exercises the built CamAdmiral container only through its HTTP,
RTSP, browser, process, and persistent-volume boundaries. It does not import
CamAdmiral application modules.

The isolated Docker Compose lab covers:

- standalone Docker-only launcher startup and shutdown, generated admin credentials, live
  HTTP access, hardened runtime settings, safe repeated execution, and state-preserving restart
- manual and full RTSP discovery on a non-default connected private subnet
- multicast ONVIF discovery plus bounded learned-neighbor ONVIF and RTSP
  probing on an oversized /16 subnet without a per-address sweep
- explicit IP discovery and adoption through a synthetic ONVIF camera
- unauthenticated and authenticated RTSP adoption
- incorrect camera-credential rejection
- empirical H.264 metadata and automatic recording/detection role selection
- periodic cache-only camera thumbnails, snapshots, and authenticated stable downstream RTSP URLs
- two downstream consumers sharing one physical-camera session
- reversible camera disable and enable
- out-of-band managed-stream deletion and automatic runtime-drift repair
- go2rtc child failure without a CamAdmiral container restart
- synthetic camera outage across a CamAdmiral restart and recovery without user action
- recovered media overriding stale offline scan state in camera summary counts
- persisted availability buckets across camera outage and recovery
- complete CamAdmiral container restart with stable IDs, paths, and secrets
- real Frigate 0.17 camera injection, global detect-FPS inheritance, legacy
  camera-level FPS cleanup, runtime creation, and frame processing
- invalid recovered-address rejection with last-known-good media preservation
- camera IP change with validated upstream replacement and stable downstream
  identities
- camera credential rotation, failed repair preservation, and successful repair
- WebKit phone-viewport rendering with a stable dashboard action bar, 44px scan
  and add-camera targets, every camera action fully visible inside its card,
  downstream passwords masked in the modal, and plaintext credentials preserved
  only for Copy

Run from the repository root:

```console
python -m pip install -r e2e/requirements.txt
python -m playwright install webkit
python3 e2e/run.py
```

Measure the steady-state delay added by one go2rtc RTSP relay with matched
decoded H.264 frames:

```console
python3 e2e/latency.py
```

The benchmark runs equal low-buffer FFmpeg consumers against a direct camera
stream and the same stream through one additional go2rtc hop. It reports the
signed arrival-time difference for matching frames as median, p95, minimum,
and maximum. Run it on an otherwise idle host. It is intentionally not a hard
release gate because host scheduling and media-pipeline startup add timing
noise that is unrelated to CamAdmiral correctness. The
`Measure go2rtc relay latency` GitHub workflow provides the same benchmark on
Reefy's Linux runner and can be started manually after it reaches the default
branch.

Docker with Compose v2 and Playwright WebKit are the host dependencies. The
runner builds the current source, creates a private bridge network and
disposable volumes, and removes them when complete. It publishes only an
ephemeral loopback port for the browser check. It does not scan the host LAN,
mount the Docker socket into a container, or use host network capabilities.

Set `CAMADMIRAL_E2E_KEEP=1` to retain a failed lab for manual inspection. Remove
it afterward with:

```console
docker compose --project-name camadmiral-e2e --file e2e/compose.yaml \
  --profile moved --profile rotated down --volumes --remove-orphans
```

Fast algorithm, parsing, storage, crypto, adapter, and HTTP-boundary tests stay
under `tests/`. They may use mocks to isolate a single behavior. Real synthetic
media and multi-process failure workflows belong here.

## Recording continuity regression

Run `python3 e2e/recording_continuity.py` on a disposable Docker host. This
focused regression uses a separate `camadmiral-recording-e2e` Compose project,
but the same private subnets as the main lab. Do not run both labs together.

The test enables continuous recording, requires two cameras to save actual
recordings, removes the managed camera through CamAdmiral's HTTP API, and
performs the operator restart. It then requires the remaining camera's saved
recording timestamps to advance beyond the restart, with nonempty media files.
It tests healthy behavior rather than treating the known failure as success.

The override pins Frigate 0.17.2 and persists `/tmp/cache` across restarts.
The usual lab's tmpfs cache would erase pending segments during a container
restart and could hide this failure. No fake segments or application mocks
are injected. Whether this sequence reliably exposes the pending-segment race
must be established by running it; a passing run does not disprove that race.

Evidence is written to `e2e-artifacts/recording-continuity/`. Set
`CAMADMIRAL_E2E_KEEP=1` to retain the disposable lab after failure. To remove it:

```bash
docker compose -p camadmiral-recording-e2e \
  -f e2e/compose.yaml -f e2e/recording-compose.yaml \
  down --volumes --remove-orphans
```

The release gate runs this regression after the main isolated E2E lab.
The regression also verifies that the removed camera is hidden from the live
dashboard, has no capture processes after restart, survives full sync as a
disabled entry, and resumes saving recordings when selected again.

The first GitHub Actions reproduction confirmed the failure on unmodified
Frigate 0.17.2. Both synthetic cameras saved two recordings before removal.
After the managed camera was removed and Frigate restarted, the remaining
camera still processed 5.1 frames/sec, but its recording count stayed at two
throughout the 120-second observation window. The removed camera's pending
cache segment remained, and Frigate logged 25 recording-cache maintenance
errors naming that removed camera. The existing E2E suite passed in the same
workflow run. This is one confirmed reproduction, not a repeatability study.

The workaround retains the original camera key with the camera, recording,
and dashboard visibility disabled. The recording-continuity assertion is unchanged;
the test must now pass without modifying Frigate. CamAdmiral preserves overridden
settings in its database for re-selection, including inherited configuration values.

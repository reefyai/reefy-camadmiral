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
- persistent detected/custom subnet selection and routed unicast ONVIF plus
  RTSP discovery within a bounded custom CIDR
- explicit IP discovery and adoption through a synthetic ONVIF camera
- unauthenticated and authenticated RTSP adoption
- two direct RTSP cameras added through the real browser from distinct paths on one DNS
  endpoint, including exact-duplicate rejection, decoded source verification, scan and restart
  persistence, independent Frigate resources, recovery when the DNS endpoint moves to another
  private address, isolated path failure, and isolated unadopt cleanup
- incorrect camera-credential rejection
- empirical H.264 metadata and automatic recording/detection role selection
- periodic cache-only camera thumbnails, snapshots, and authenticated stable downstream RTSP URLs
- two downstream consumers sharing one physical-camera session
- reversible camera disable and enable
- camera unadopt cleanup, including removal from a real Frigate target,
  plus persistent stable-identity block and unblock
- out-of-band managed-stream deletion and automatic runtime-drift repair
- go2rtc child failure without a CamAdmiral container restart
- synthetic camera outage across a CamAdmiral restart and recovery without user action
- recovered media overriding stale offline scan state in camera summary counts
- persisted availability buckets across camera outage and recovery
- complete CamAdmiral container restart with stable IDs, paths, and secrets
- real Frigate 0.17 through a remote container-network API URL, per-camera LAN and
  localhost configuration previews, global detect-FPS inheritance, legacy camera-level
  FPS cleanup, runtime creation, and frame processing
- invalid recovered-address rejection with last-known-good media preservation
- camera IP change with validated upstream replacement and stable downstream
  identities
- automatic two-camera address recovery across targeted retry scans, including stable ONVIF and
  unique-MAC matching, an initial recovery scan that misses rebooting cameras, bounded retries,
  stock go2rtc restarts, downstream client reconnection through unchanged URLs, distinguishable
  moved media within 45 seconds, resolution-change recovery, resolved offline and address-change
  incidents, persisted sources across a supervised child restart, and Frigate frame recovery with
  only the expected detection-dimension metadata update
- changed IP, MAC, and ONVIF identity producing a separate adoptable camera while
  retaining the old adopted camera offline
- camera credential rotation, failed repair preservation, and successful repair
- WebKit phone-viewport rendering with a stable dashboard action bar, 44px scan
  and add-camera targets, every camera action fully visible inside its card,
  downstream passwords masked in the modal, plaintext credentials preserved only
  for Copy, fixed-size sync spinner geometry, and specific sync failure details

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
  --profile moved --profile rotated --profile identity down --volumes --remove-orphans
```

Fast algorithm, parsing, storage, crypto, adapter, and HTTP-boundary tests stay
under `tests/`. They may use mocks to isolate a single behavior. Real synthetic
media and multi-process failure workflows belong here.

## Media memory regression

### Stalled snapshot cleanup

Run `python3 e2e/stalled_snapshot.py` on a disposable Docker host. A real RTSP
proxy initially forwards a synthetic 1080p camera, then drops media while still
answering RTSP control requests. After adoption, six batches of four concurrent
HTTP snapshots must fail within the deadline without retaining relay consumers,
decoder processes, or growing relay memory. Normal background health checks stay
enabled. Resuming media must produce a decodable JPEG at the same stream URL,
without restarting go2rtc. The lab uses the production 512 MiB / 192 PID limits.

This runs as a separate parallel job in the full release gate. Its artifacts in
`e2e-artifacts/stalled-snapshot/` include consumer IDs, process counts, relay RSS,
cgroup failure counters, workload timings, and logs. Unlike the successful-frame
workload below, it catches server-side snapshot work surviving client timeouts.

### Concurrent successful snapshots

Run `python3 e2e/memory_pressure.py` on a disposable Docker host. It adopts eight
synthetic 1080p RTSP cameras through the real HTTP API, then requests eight
concurrent JPEG snapshots per batch while ordinary health and thumbnail work stays
enabled. The separate `camadmiral-memory-e2e` lab applies the shipped 512 MiB RAM
and 192 PID limits, with no swap allowance. No allocation mocks or forced kills
are used. The full release gate runs this alongside the existing E2E job.

The default 12 batches must return 96 actual JPEG frames without OOM events or
container restarts. Samples record cgroup memory throughout the run, including
idle periods between batches. Late idle memory must remain within 32 MiB of the
post-warmup baseline, and FFmpeg snapshot processes must exit after the workload.
This checks bounded retention for this workload, not absence of every possible leak.
Evidence is saved under `e2e-artifacts/memory-pressure/`, including Docker events
that survive cgroup replacement on container restart.

Use `CAMADMIRAL_MEMORY_CYCLES=60` for a roughly ten-minute soak. To reproduce
the previous runtime envelope without changing application code, set
`CAMADMIRAL_E2E_MEMORY_LIMIT=256m`. The same healthy-behavior assertions remain in
effect, so an OOM reproduction fails the test rather than being counted as a pass.

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

The release gate runs this regression before the main isolated E2E lab.
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

## Frigate import and takeover regression

`python3 e2e/frigate_import.py` runs the committed migration lab with real
Frigate 0.17.2, synthetic authenticated/open media, persistent recording cache,
and disposable volumes. The release gate runs it before the main lab and
uploads its evidence on success or failure. These labs share private subnets
and must not be launched together; the workflow preserves the existing parallel
jobs and runs the new lab within the established sequential lab job.

The fixture mixes a new camera, an existing CamAdmiral camera with a different
display name, authenticated Record/Detect sources, and disabled, offline, and
unsupported entries. The test exercises browser preview, cancellation,
selection, restart acknowledgement, takeover, repeat execution, full sync,
stream-role changes, application restarts, and restoration. It checks unchanged
Frigate keys, display names, masks, zones, detection FPS, recording settings,
groups, and untouched cameras. Real recordings are saved before and after
migration, historical playback is decoded, and synthetic event/review fixture
rows must survive. Unit/component tests in `tests/test_frigate_import.py` cover
source resolution, ambiguous matches, encryption/redaction, source validation
failure, stale previews, interrupted-save retry, and ownership boundaries.

Browser assertions require Select all above the first camera, checked/disabled
camera and conflict-review selections while running, visible spinner/progress
and current restart stage inside the viewport even at the action buttons,
and selection restoration after reopening the
dialog. The offline fixture produces partial success: the summary must report
three managed cameras and one failure, preserve completed selections, and retry
only the failed camera. Progress snapshots must exclude source credentials.

The import target uses a loopback API port through a lab-only HTTP proxy. After
real Frigate accepts a restart, the proxy drops its reply and makes the API
unavailable for twelve seconds. Batch import must wait for the expected running
inputs and capture, then continue with the remaining cameras. Explicit restore
must recover from a truncated response body, and interrupted-job retry covers
an empty successful HTTP response. The retained proxy counters prove the fault
injections occurred; `result.json` records import and restore recovery. Component
tests also reject explicit restart failures and require restoration if capture
cannot be verified after an uncertain reply.

Evidence is retained in `e2e-artifacts/frigate-import/`: `result.json` includes
source revision, historical recording identities, event/review snapshots, and
playback results; `transcript.log` retains the lab output and container logs;
browser screenshots show only synthetic data. Set `CAMADMIRAL_E2E_KEEP=1` to
retain a failed lab. Cleanup uses the same three Compose files with project
`camadmiral-frigate-import` and `down --volumes --remove-orphans`.

The import fixture also injects a deterministic stalled-media fault. The test
restarts CamAdmiral while an import is probing that source, requires the durable
job to become retryable with Frigate unchanged, resumes media, and retries the
same journal without creating duplicate cameras. This exercises process-crash
recovery through container and HTTP boundaries rather than an in-process mock.

After takeover, the authenticated synthetic source moves to a new private
address under the same DNS name and emits a different frame size. The test must
decode that new size through Frigate's recording alias while preserving camera
and stream IDs, downstream URLs, Frigate display name, and detection tuning.
The browser also exercises explicit acceptance of existing CamAdmiral settings
when imported credentials differ, and leaves ambiguous profile matches alone.

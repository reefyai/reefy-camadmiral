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

The external-reader case uses separate Record/Detect sources and the existing
decoded-frame consumer as an independent recorder. It first saves real footage
and event/review history, then places the independent recorder in the only
Record slot while Frigate Detect remains live. Failed import must preserve the
exact configuration and history, leave that reader decoding without reconnecting,
avoid adoption, and let an unrelated camera keep recording. The browser must
report configuration restoration without claiming that Record media recovered.
After releasing only the external fixture reader, retry must import both profiles,
save fresh recordings, and decode the original historical footage. Evidence,
including the restored-result and retry screenshots, remains in the same normal
`frigate-import-evidence` artifact. The unchanged application baseline must fail
the restored-result browser assertion after proving the connection contention.

The import target uses a loopback API port through a lab-only HTTP proxy. After
real Frigate accepts a restart, the proxy drops its reply and makes the API
unavailable for twelve seconds. Batch import must wait for the expected running
inputs and capture, then continue with the remaining cameras. Explicit restore
must recover from a truncated response body, and interrupted-job retry covers
an empty successful HTTP response. The retained proxy counters prove the fault
injections occurred; `result.json` records import and restore recovery. Component
tests also reject explicit restart failures and require restoration if capture
cannot be verified after an uncertain reply.

The operator fixture also passes through a synthetic RTSP server with one
playback slot. Real Frigate occupies it before import; a second reader must be
rejected while existing video and recordings remain healthy. The same committed
lab verifies a journaled release and takeover, rejected media after release with
exact configuration restoration and no adoption, and process loss during the
released-reader window followed by automatic startup restoration. Fresh
recordings are required for every selected camera, in addition to historical
playback and event/review preservation. The result artifact retains session-cap
and denial counters and each recovery outcome. The fixture is not used by other
labs unless its session-cap environment option is explicitly enabled.

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

## Imported camera physical identity and DHCP recovery

`python3 e2e/frigate_identity.py` runs a separate disposable lab in the existing
release gate's **Imported camera address recovery** job. It uses its own subnet,
Compose project and source-specific image, and runs alongside the established
parallel jobs. The job honors the existing GitHub-hosted runner option. Its
shared-runner concurrency group prevents overlapping copies of this one lab.

The browser imports two Frigate cameras with literal-IP Record/Detect sources
on one synthetic ONVIF device. Import must learn a stable physical identity
without merging the logical channels. Stopping the device and starting it at
another address models a power/DHCP event. Normal targeted discovery and media
recovery must follow it without a forced source edit, preserving all camera and
stream IDs, credentials, paths, roles, downstream URLs, Frigate keys, names,
groups and tuning. Both channels must save fresh recordings; pre-import footage
must still decode and synthetic event/review rows must remain unchanged.

A foreign device then takes the old address. Restart must retain the correct
route, and explicitly attempting to bind the foreign UUID must fail. Removing
only saved links while retaining prior identity observations models an upgrade;
bounded background enrichment must restore them and follow another address
change. Removing one camera's link and observations models an unanchored legacy
import: it must not guess from an IP while the other channel recovers. The
browser identifies the current device, reviews its UUID/MAC and confirms it.
Identification alone and a changed confirmation cannot bind. Recovery validates
video separately and retains the original logical IDs/history.

Unit/component cases in `tests/test_camera_identity.py` cover missing/ambiguous
identity, duplicate UUID observations, recycled MAC with a different UUID,
disabled cameras, failed media validation, discovery projection and deduplicated
physical recovery requests for separate channels. Evidence is uploaded on
success or failure as `frigate-identity-evidence`: source/image provenance,
`result.json`, browser screenshots and container transcript. The identical
regression must complete import on the unchanged application, simulate the DHCP
move, and fail because both logical channels cannot recover to the new address.
The initial identity measurement and final saved source hosts remain in the
machine-readable artifact, including on that expected failure.

Playback probe JSON is captured from stdout alone. Decoder diagnostics on
stderr remain in the normal transcript, and nonzero command exits still fail
the lab. `tests/test_e2e_command_output.py` reproduces a diagnostic arriving
between JSON writes, checks rejected commands and malformed stdout, and runs
in the established Actions unit/component step. Playback decoding, moved-frame
dimensions and recording/history assertions remain mandatory.

Run the complete default-parallel gate with the established workflow:

```sh
gh workflow run release-gate.yml --repo reefyai/reefy-camadmiral \
  --ref <review-branch> -f github_hosted=true -f force_full=true
```

## Parallel labs on shared self-hosted runners

Media workflows use the general runner pool. Each Docker job runs in an
ephemeral Ubuntu job container with its own Docker daemon, Unix socket,
storage and empty registry credentials. Fixed synthetic camera subnets and
localhost access therefore remain private to each job, even when workers
share a physical host. Forks retain the GitHub-hosted routing boundary.

The local Docker-lab action checks real cgroup v2 memory and PID support before
tests start. Its post step checks daemon ownership before deleting only that
job's disposable resources. No parent Docker cleanup, runner relabeling,
firmware cache changes or suite serialization is used. The normal
`docker-lab-<job>` artifact retains source revision, daemon fingerprint,
readiness and startup logs; fingerprints must differ across concurrent jobs.
`tests/test_ci_docker.py` covers separate contexts, empty credentials and
refusal to clean a foreign job, path or daemon. Existing media, latency,
history, recovery and resource assertions still run unchanged.
Docker-lab helper, action and workflow changes automatically require the full
parallel PR gate; helper/action changes also trigger the relay benchmark.
Readiness launches a container and verifies its actual memory and PID limits,
in addition to checking controller support.

## Receive-only RTSP compatibility regression

Run `python3 e2e/passive_rtsp.py` on a disposable Docker host after fetching the
repository history. The release gate runs this as an independent parallel job in
a private Docker daemon; it does not change the other labs' execution policy.

The same black-box media check runs against unmodified source
`28ee79068ea25017fe670bc9a68808d0528aea5c` and the candidate. Both images are
built from their exact source. A synthetic camera provides generated H.264 video
and a PCMU microphone tone. If asked for ONVIF backchannel, it advertises an
additional receive-only audio track, then returns an incomplete SETUP response.
Direct FFmpeg preflight must work before the baseline is considered reproduced.
The baseline must fail real A/V recording with observed broken audio setup;
unrelated infrastructure failures do not count as a reproduction.

The candidate must decode at least 50 video frames and audible microphone samples
from each recording, share one upstream playback session between two readers,
and retain camera IDs, role bindings, downstream URLs and authentication across
an app restart. An ordinary camera must still supply video and microphone audio.
Wrong downstream credentials and genuinely missing video must fail, and restoring
media must recover without changing the camera's identity.

`e2e-artifacts/passive-rtsp/evidence.json` records source commits, built image IDs,
baseline/fixed outcomes and protocol counters. Driver and container logs are
uploaded with the evidence. The unit regression was first run against unchanged
runtime source: 31 tests passed and the two relay-policy assertions failed.
After the fix, all 33 media tests passed. Full E2E results belong to the exact
release-gate run and its retained artifacts.

The imported-identity playback test also isolates the selected recording from
overlapping rows. Frigate's pinned clip API concatenates all matching rows,
which can mix old and new codec headers during a resolution change even when
the requested endpoints are inside the selected row. Synthetic SQL-selection
tests reproduce that helper defect and verify exclusive intervals. Complete
post-checkpoint segments, decoded frames, exact dimensions, source identity
and historical-data assertions remain required.

Committed camera address changes now also invalidate the imported camera's owned
Frigate runtime aliases once. Its stable source URLs and operator configuration
are retained, while cached RTSP codec parameters are discarded. A durable internal
address-event revision prevents repeated reconnects and allows failed API calls
to retry. Tentative recovery, disabled or unselected cameras, and operator source
conflicts do not trigger this refresh. Ambiguous replacement still attempts every
owned source and retains the revision for retry. Unit coverage reproduces the missing refresh on unchanged
runtime source; the existing multi-channel identity lab keeps exact dimension,
decoded video and history preservation assertions and retains selected synthetic
recording files as evidence.

Imported readers also refresh once after the control service restarts, because
relay RTP timing can reset while downstream source URLs remain stable. An
internal process revision prevents repeated reconnects on ordinary sync cycles.
The identity lab keeps its repeated restart and complete-recording assertions.

A relay definition update does not disconnect existing go2rtc consumers. Imported
recovery therefore uses Frigate runtime camera controls to stop capture, replaces
only owned aliases using delete/recreate, then resumes capture and waits for it to report
online. A missing go2rtc YAML entry is normalized to the owned source and deletion
is retried once within the same pause; other errors remain failures. No
Frigate YAML or history is rewritten. An encrypted pending-resume journal is
saved before pausing, cleared only after verified resume, and retried after a
crash or unavailable response. Operator runtime pauses remain paused.

If per-camera capture control cannot stop or resume, recovery may restart the
connected Frigate instance once. This interrupts all of that instance's cameras;
the behavior was explicitly approved. The fallback checks for unrelated runtime
control overrides before dispatch, confirms a new service start identity, verifies
active capture and record readers, and requires unchanged saved configuration.
A durable encrypted journal verifies uncertain restart replies without issuing a
second restart. Recovery marks all owned channels together after verified restart.
Shared media updates and downstream recovery are serialized to prevent a second
relay reset from invalidating newly reconnected readers.

Known offline Record or Detect roles defer reader refresh until video returns;
an unavailable source must not trigger a whole-target restart or commit recovery.

The identity recording checkpoint waits for both imported readers to commit the
current address revision and, after a restart, a new shared runtime generation.
Pending capture/resume or target restart journals cannot count as ready. This
prevents selecting an in-flight segment created before the second reader was
reconnected. Actual old/new decoding, exact dimensions, complete post-checkpoint
recordings and history preservation remain required. Older baseline runtime
without these journals retains the original black-box media assertions.

After its first successful targeted recovery, the identity lab makes only the
Frigate WebSocket control location return HTTP 503. Live RTSP and the HTTP APIs
remain available. The next relay restart must use the real whole-instance
fallback, with a changed Frigate service start identity, completed reader journals,
new decodable recordings, exact source dimensions and unchanged historical data.
This deterministic negative case does not rely on a timing-dependent stop hang.


## Authenticated direct-input import regression

`FRIGATE_TEST_VERSION=0.17.2 python3 e2e/frigate_direct_auth.py` and
`FRIGATE_TEST_VERSION=0.18.2 python3 e2e/frigate_direct_auth.py` use the existing
isolated Docker lab with real Frigate, a credential-protected synthetic RTSP
camera, direct Record/Detect inputs, and disposable persistent volumes. The
release gate runs both versions in separate parallel jobs and retains
`direct-auth-import-<version>` artifacts on success or failure.

The preflight requires masked `/api/config` input credentials, unmasked admin
`raw_paths`, successful camera authentication with the fixture password, and
rejection with a wrong password. The wrong-password import must fail without
changing saved configuration or adopting a camera. The valid direct-input import
must succeed, retain roles and unrelated camera settings/groups, preserve
recording/event/review history, decode both historical and fresh full-resolution
playback, and keep stable identities on repeat import. Artifacts include exact
source/application-tree and image provenance, JSON results, logs, and selected
synthetic playback files. No real device data is used.

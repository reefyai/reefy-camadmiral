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

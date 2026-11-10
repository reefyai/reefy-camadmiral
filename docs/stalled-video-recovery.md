# Stalled-video recovery

CamAdmiral observes enabled, role-bound streams with real external consumers.
A stable producer with no advancing video packets for 20 seconds is logged with
its stream/camera IDs, producer IDs, video/audio packet counters and received
transport bytes. Snapshot-only and idle sources are excluded. Source URLs,
passwords, SDP and camera-provided text are never included in watchdog events.

A stall with ongoing transport traffic receives one bounded, fresh decoded-frame
check through the existing authenticated relay. A decoded frame or local snapshot
capacity pressure prevents a reset. A completely silent or powered-off camera
continues to use go2rtc's own connection retry without resetting other cameras.

If video still cannot decode, CamAdmiral restarts the shared relay without editing
its configuration or restarting Frigate or the application container. Stock
go2rtc has no reliable per-producer reset for already attached readers. Every
CamAdmiral stream can briefly disconnect during this shared recovery. Camera IDs,
stream URLs, roles, credentials and recording history are preserved.

A private `relay-watchdog.json` beside the database reserves each attempt before
issuing the restart. It persists across application restarts, permits at most three
attempts per rolling hour and requires at least 120 seconds between attempts.
Unreadable or corrupt budget state prevents automatic resets and logs the reason.
Verification retries are bounded to three failed decoded-frame attempts and a
90-second window; disabled or changed streams invalidate pending verification.
Only a decoded frame produces `recovery_verified` and a successful health result.

Watchdog events have UTC timestamps and include `video_stalled`,
`restart_started`, `restart_failed`, `recovery_deferred`, `verification_failed`
and `recovery_verified`. Deferred budget and capacity messages are suppressed
until their reason changes. Continued stalls produce a counter summary no more
than once per minute. `video_packets_resumed` alone is not proof of decoding.
Supervisor exit logs also record the relay exit code and restart count.

## Repeatable validation

The ordinary parallel release gate includes `e2e/stall_recovery.py`. It runs a
real synthetic camera, an RTSP fault proxy, CamAdmiral and Frigate 0.18.2 in the
existing disposable Docker lab. Dedicated audio consumers keep both selected
upstream sessions alive while the proxy discards only their video packets.
The proxy does not resume those sessions. Automatic recovery must replace them,
decode every selected role, preserve stable IDs/secrets/configuration, and play
historical and fresh recordings for the affected and unrelated cameras. Neither
Frigate nor CamAdmiral's container may restart. A separate camera power-off check
must leave the unrelated camera decoding without another shared reset.

```console
python3 e2e/stall_recovery.py
```

To run the same committed test against an unchanged application, provide its
exact 40-character repository commit. Only the application build context changes;
the test driver, fault and assertions remain the current committed versions.

```console
CAMADMIRAL_E2E_APPLICATION_REF="$BASELINE_COMMIT" python3 e2e/stall_recovery.py
```

The existing Stalled snapshot regression workflow accepts `watchdog_only=true`
for focused diagnosis and `application_ref` for the baseline. Its default retains
all existing parallel labs. The new job runs only trusted repository code on the
self-hosted Linux runner, inside the existing private Docker daemon. It never
falls back to a hosted runner or uses the shared host Docker engine.

Evidence in `e2e-artifacts/stall-recovery/` records test/application commit and
application-tree provenance, the functional result, fault counters, app and
recorder start times, and the normal synthetic lab logs on both pass and failure.


The unchanged-application baseline is `0106d5d92b6e5926381cc2c3bf1fde9f9226cd81`.
Test source `fe4ef68abf92da5a22b3d9ed1f88675ebc5e325e`, run 42814240307,
artifact `stall-recovery-evidence`, reproduces failed fresh video decoding while
the proxy confirms dropped video packets and continuing audio on frozen sessions.
Earlier fixture-development runs without confirmed audio are not baseline proof.
Final fixed validation uses the same committed black-box regression, including
negative outage, settings preservation and historical/new recording playback.

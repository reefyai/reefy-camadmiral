## v2026.11.26-00

- Retry transient verification failures after camera address cutover without another shared relay restart. Every enabled stream still has to pass; persistent failures roll back and authentication failures fail immediately.
- Log each upstream and relay probe with generated camera/stream identifiers, stage, attempt and latency. Source URLs, profile tokens, credentials and exception messages are excluded.
- Commit verified camera health and address-incident resolution together, sending one address-recovery notification while retaining both incident histories. No API fields, incident types or configuration settings change.
- Keep fresh-recording verification anchored across producer reconnects, require current upstream observations before acknowledging reader recovery, and safely discard unused legacy baseline windows. Existing recovery deadlines, cooldowns and operator-controlled Frigate restarts remain in force.
- Refresh targeted Frigate capture once after a committed camera address change, even when current relay SDP and segment timestamps appear healthy, so recording processes discard stale codec metadata. Recognize a confirmed operator recorder restart without replenishing exhausted recovery attempts, and require post-restart recordings before acknowledgement.
- Add an Actions container regression that reproduces premature rollback on the unchanged source, then verifies delayed RTSP recovery, permanent failure, credential rejection, real decoded video and retained data on the fixed source.

## v2026.11.17-01

- Background imported-camera recovery no longer requests whole-Frigate restarts. Explicit import/restore operations keep their existing restart confirmation.
- Healthy readers acknowledge source and relay revisions without a camera OFF/ON cycle.
- Camera reconnect attempts are limited to two with a five-minute cooldown, persisted across CamAdmiral restarts and source changes. Failed pause/resume recovery asks for operator action; resume-only retries are bounded too.
- Verify fresh recording output after relay revisions; healthy camera FPS alone must not mask stalled recording. Quiet motion-only cameras wait without an automatic pause.
- Explain manual recovery, retained camera OFF state, and cooldowns in the UI. Periodic synchronization and Repair sync never restart Frigate; user-triggered Sync cameras states its possible restart before the action.

# CamAdmiral v2026.11.17-00

Keep all owned Frigate relay links current when the host address changes, even
while an earlier camera has a pending recovery. Endpoint maintenance is separate
from reader recovery, per-camera failures are isolated, and recovery attempts
rotate across passes. Accepted recorder-wide restarts remain serialized.

Exact configuration write intents preserve restart ownership checks through
lost replies. Operator edits, audio policy, camera identities, settings,
credentials, rollback sources and recording history are retained. Regression
coverage includes delayed recovery, host changes, partial runtime failures,
interrupted saves and decoded old/new recordings on Frigate 0.17.2 and 0.18.2.

Private Docker lab readiness now retries a slow info probe inside the existing
90-second window while preserving cgroup and resource-enforcement checks.

# CamAdmiral v2026.11.12-00

New Frigate integrations default to video-only relay feeds to avoid live-view
stalls caused by empty advertised audio tracks. The integration editor offers
Preserve camera audio for listening and audio analysis. Existing integrations
retain their audio policy on upgrade until explicitly changed.

Policy changes update all owned imported and native stream aliases, including
legacy live aliases and audio conversion wrappers, without restarting Frigate.
Camera identities, history, masks, tuning and original rollback sources remain
intact. Reopen existing live views after changing the policy.

The normal self-hosted release gate covers both supported Frigate versions with
real synthetic A/V, pinned baseline reproduction, decoded video, retained audio
samples, invalid settings and policy round trips. Unit coverage includes schema
migration, compatibility defaults and conflicting operator source edits.

# CamAdmiral v2026.11.10-00

Recover video that stops advancing while its relay transport remains active.
The bounded watchdog verifies a failed decoded-frame check before restarting
only the shared relay, then verifies decoded video after recovery. Persistent
limits prevent repeated shared interruptions; silent cameras keep native retries.
Camera identities, settings, credentials and recording history are preserved.

Add timestamped, credential-free packet counters, restart decisions and decoded
recovery evidence for diagnosis. The synthetic end-to-end lab freezes video while
audio continues, requires automatic recovery and decodable old/new recordings,
and verifies that an offline camera does not reset unrelated healthy streams.

Fix Frigate import readiness for cameras with a single input whose saved roles
omit Record or Detect. Frigate automatically assigns both roles at runtime;
CamAdmiral now compares those equivalent configurations correctly and preserves
both roles when splitting the managed capture inputs. Source URLs,
credentials, audio roles and multi-input role assignments remain protected.
Original saved settings and recording history are preserved for restoration.

The established grouped-import and restart/restoration E2E labs cover detect-only
and record-only single inputs against both Frigate 0.17.2 and 0.18.2.

Reader recovery also requires fresh upstream video before pausing capture or
using its restart fallback. Unavailable sources now yield so independent address
or identity correction can restore them. Safe fallback diagnostics record the
request cause and verification duration without source URLs or credentials.

Keep an idle SQLite connection through the supervised process lifetime so short
worker operations do not repeatedly trigger last-connection WAL cleanup. Workers
retain separate transactions and writer exclusion; shutdown closes the connection
after child processes stop. Unexpected import diagnostics include SQLite error
codes and phases while keeping credentials and source URLs private.

# CamAdmiral v2026.10.02-00

Brings the Frigate, stream and authentication improvements that were previously only
available as dev candidates (v2026.09.19-00 through v2026.09.21-01) into the main
release line, together with per-camera alert silencing.

Frigate:

- Set a detection resolution (detect width and height) per camera for each Frigate
  target. Saved overrides are kept in the Frigate configuration instead of being reset
  to the detect stream's native resolution, and appear in the configuration preview.
- Report Frigate sync problems separately from camera source health.

Streams:

- Enable or disable individual streams and assign Record and Detect roles in the
  Streams dialog. Both roles can use the low-resolution stream for limited-bandwidth
  cameras. Disabled streams are not restreamed or health-probed and are omitted from
  the consumer API; re-enabling keeps their original IDs and URLs.
- Roles are collapsed by default, camera source URLs are masked, and settings persist
  across restarts and camera address recovery.

Authentication recovery:

- Retry authentication-failed streams after one, five, fifteen and thirty minutes,
  capped at thirty minutes, with backoff preserved across restarts.
- Require a decoded video frame before clearing an authentication error.

Upgrades:

- Database updates are applied by checking the existing schema, so installations that
  ran a dev candidate, v2026.10.01-00 or v2026.10.01-01 all upgrade correctly. Detection
  overrides saved by a dev candidate are restored.
- Includes per-camera alert silencing from v2026.10.01-00.
- Preserve existing camera identities, RTSP URLs, credentials and Frigate bindings.

Validation covers upgrades from dev-candidate, v2026.10.01 and fresh databases,
detection overrides through a real Frigate 0.17.2, authentication recovery, and the
full release gate.

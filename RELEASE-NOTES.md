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

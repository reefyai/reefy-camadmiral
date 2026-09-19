# CamAdmiral v2026.09.19-01

- Enable or disable individual streams and assign Record and Detect in the Streams dialog.
  Both roles can use the low-resolution stream for limited-bandwidth cameras.
- Disabled streams are not restreamed or health-probed and are omitted from the consumer API.
  Re-enabling retains their original IDs and URLs.
- Save updates existing Frigate selections and briefly reconnects all streams through one
  shared relay restart. Settings persist across restarts and camera address recovery.

Also includes the authentication recovery improvements from the previous dev candidate:

- Automatically retry authentication-failed streams after one minute, then five,
  fifteen, and thirty minutes, capped at thirty minutes between attempts.
- Preserve retry backoff across restarts, with one recovery connection per camera
  at a time and bounded global concurrency.
- Require a decoded video frame before clearing the authentication error.
- Keep working sibling streams healthy and resolve incidents after recovery.
- Enroll previously stuck cameras automatically. Credential updates use the
  existing immediate validation flow without waiting for the old backoff.
- Preserve camera identities, URLs, credentials, and Frigate configuration.

Validation includes real RTSP authentication rejection and recovery at production
retry intervals, snapshot cleanup, resource limits, and the full release gate.

This candidate is published to Reefy dev first for operator testing.

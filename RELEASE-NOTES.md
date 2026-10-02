# CamAdmiral v2026.10.01-01

- Fix cameras disappearing after upgrading to v2026.10.01-00 on installations that
  previously ran a development build. The database update for alert silencing was
  skipped there, so the camera list, health checks and address recovery failed.
- Database updates after this release are applied by checking the existing schema,
  so they are applied correctly whichever builds an installation ran before.
- Includes per-camera alert silencing from v2026.10.01-00.
- Preserve existing camera identities, RTSP URLs, credentials and Frigate bindings.

Validation covers databases from development builds with diverging update history,
databases already upgraded to v2026.10.01-00, fresh installations, and the full
release gate.

# Import existing Frigate cameras

In **Settings > Integrations**, choose **Import from Frigate** for a connected
Frigate instance. Review the new cameras, existing CamAdmiral matches, already
managed cameras, and sources that need attention. Select cameras, acknowledge
any restart, then choose **Import and manage selected**.

CamAdmiral resolves direct RTSP sources and Frigate go2rtc aliases. IP and port
identify candidates, while exact stream paths and channel parameters distinguish
streams; names or addresses alone never merge cameras. Existing matching
cameras retain their UUIDs, credentials, and stream URLs. If credentials or roles
differ, explicitly choose to keep the existing CamAdmiral settings or leave the
camera unselected for manual review. Ambiguous matches remain unchanged.

Imported cameras retain their Frigate configuration key and display name,
recording/event/review history, masks, zones, recording policy, dashboard groups,
and detection tuning. CamAdmiral supplies recording, detection, and live-view
media and synchronizes stream-role changes through stable relay aliases.
Frigate-specific settings remain operator-owned. Detection-resolution changes
in the normal chooser are not applied to imported entries; edit their preserved
Frigate configuration when needed.

Before changing each camera's sources, CamAdmiral validates upstream and relayed
media, saves an encrypted rollback journal, and rechecks the preview against the
current configuration. Each migrated camera can require a Frigate restart,
briefly interrupting recording on other cameras. Failed and unselected cameras
retain their existing connections. Disabled cameras are reported as pending and
are never enabled or probed by the import.

Sources that use unresolved secrets, multiple fallback URLs, separate credentials
per stream, shared live-view aliases, or converted capture inputs require manual
review. Standard FFmpeg live-view wrappers are retained. RTSP credentials are
stored using CamAdmiral's existing encryption. Separate ONVIF settings are retained
in the encrypted migration snapshot; importing RTSP media does not itself verify
ONVIF access or establish a new ONVIF identity. Address recovery depends on the
camera's existing verified identity or resolvable source hostname.

The progress window can reconnect to an interrupted job in the same browser
session. A server restart marks interrupted jobs for retry. Completed cameras
are reused on subsequent imports. **Restore original Frigate connection**
releases the binding and restores the saved sources, with a restart. Restore an
imported camera before removing its binding or unadopting it; ordinary removal
will not silently delete the legacy entry or restart Frigate. Restoration
refuses to overwrite conflicting operator edits and keeps adopted CamAdmiral
camera data. Rollback snapshots are inside the existing persistent data and
master-key backup boundary. Old previews are bounded to twenty per integration;
active rollback snapshots are retained separately.

Validation lives in the established GitHub Actions release gate. See
[the E2E lab](../e2e/README.md#frigate-import-and-takeover-regression) for the
committed fixtures, repeatable command, artifacts, and assertions. No live fleet
data is used in tests, screenshots, examples, or validation notes.

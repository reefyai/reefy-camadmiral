# Import existing Frigate cameras

In **Settings > Integrations**, choose **Import from Frigate** for a connected
Frigate instance. Review the new cameras, existing CamAdmiral matches, already
managed cameras, and sources that need attention. Select cameras, acknowledge
any restart, then choose **Import and manage selected**.

During import, the selected cameras remain checked and disabled. A spinner,
progress bar, and current camera/stream stage show validation, relay setup,
Frigate restart, or restoration. The status stays visible when the camera list
is scrolled, including the final result counts. Reopening the dialog retains that selection
and any explicit decision to keep existing CamAdmiral settings. **Select all**
appears above the camera list. Completed results report managed, failed, and
pending cameras separately; **Retry selected** excludes completed cameras.
Camera names, statuses and restore actions share a compact row; error and review
details appear underneath, with wrapping on narrow screens.

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

For a multi-camera selection, CamAdmiral checks the whole preview and saves an
encrypted group journal before releasing any reader. Working, supported RTSP
URLs and credentials are transferred from Frigate without opening extra physical
camera connections first. The selected Frigate readers are paused together and
their private upstream aliases released. Newly created CamAdmiral cameras remain
disabled until that release, then their relays and decoded video are validated.
Successful cameras are saved together and share one final Frigate restart.
If runtime pause or alias release is unavailable, one shared initial restart
releases the group, followed by the final restart. This fallback uses two shared
restarts, rather than two per camera.

A failed camera returns to its original connection while successful peers remain
managed. Unselected entries and conflicting operator edits stay unchanged.
Disabled, offline, ambiguous, shared-source, or unsupported entries are left for
review. Runtime control overrides block a grouped import so its final restart
cannot silently discard an operator's temporary settings. Other recorders are
never stopped. Single-camera imports retain their existing validation and
connection-limited handoff behavior.

An uncertain restart reply is verified against the new running service and actual
media before committing ownership. After process loss, startup resumes a saved
cutover or restores an unfinished group. An already accepted restart is not
repeated merely because its reply was lost. Recovery journals remain encrypted
and survive ordinary preview pruning until the group is complete.

Failure results distinguish an unchanged configuration from one restored after
a temporary handoff. Restoration confirms the saved Frigate configuration and
capture inputs, but does not promise that a separate Record stream has resumed.
Another recorder or live client may still hold the camera's available connection
slots even while Detect works. Such setups need one physical camera owner and
shared relay streams for downstream recorders, with a separately planned cutover.
Check both recording and video availability after restoration. Retries clear the
previous restoration result and report the new attempt's outcome.

Failed handoffs restore the original entry and aliases. After a CamAdmiral crash,
startup automatically resumes grouped cutovers or attempts restoration of unfinished handoffs. Operator edits
or a changed integration address stop restoration and retain the encrypted
journal for manual recovery. When restoring a connection-limited import, a newly
created CamAdmiral camera is kept disabled with its data retained, so its relay
releases the slot needed by the original reader. Reused cameras are not disabled;
a relay selected by another integration requires that selection to be removed
first. A saved retry can reuse the retained camera; enable it explicitly before
linking it through a new preview.

Sources that use unresolved secrets, multiple fallback URLs, separate credentials
per stream, shared live-view aliases, or converted capture inputs require manual
review. Standard FFmpeg live-view wrappers are retained. RTSP credentials are
stored using CamAdmiral's existing encryption. Separate ONVIF settings are retained
in the encrypted migration snapshot. After validating an import, CamAdmiral probes
the current local address to learn its ONVIF discovery identity or observed MAC.
This does not require ONVIF management credentials or replace the imported RTSP
profiles. The physical identity is linked separately from logical camera and
stream IDs, so separate channels remain separate cameras. After a DHCP address
change, targeted discovery follows the same identity and validates every enabled
saved stream and relay before changing source hosts. Paths, channel parameters,
credentials, downstream URLs and Frigate configuration stay in place. Failed
validation preserves the prior sources. Conflicting or ambiguous identities are
not followed, and disabled cameras remain unchanged.

The link and identity history persist across restarts and normal data backups.
Older imports with a prior verified identity can acquire the link automatically
while their video works. An older import with no physical identity cannot safely
be associated from a cached or reused IP. In its camera **Details**, choose
**Link device identity**, enter the current address, identify the device, review
the ONVIF identity/MAC, and confirm it belongs to that camera. Confirmation probes
again and rejects a changed identity or a conflict with existing history. Video
recovery is validated separately. Cameras that cannot expose a unique physical
identity can continue using a stable hostname or an operator-maintained address.

The progress window can reconnect to an interrupted job in the same browser
session. A server restart marks interrupted jobs for recovery; saved grouped cutovers can complete automatically. Completed cameras
are reused on subsequent imports. **Restore original Frigate connection**
releases the binding and restores the saved sources, with a restart. Restore an
imported camera before removing its binding or unadopting it; ordinary removal
will not silently delete the legacy entry or restart Frigate. Restoration
refuses to overwrite conflicting operator edits and keeps adopted CamAdmiral
camera data. Rollback snapshots are inside the existing persistent data and
master-key backup boundary. Old previews are bounded to twenty per integration;
active rollback snapshots and unfinished group journals are retained separately.

Validation lives in the established GitHub Actions release gate. See
[the E2E lab](../e2e/README.md#frigate-import-and-takeover-regression) for the
committed fixtures, repeatable command, artifacts, and assertions. No live fleet
data is used in tests, screenshots, examples, or validation notes.

## Frigate audio policy

Each Frigate integration has an **Audio** setting in its add/edit dialog.
New integrations default to **Video only**. Existing integrations migrate to
**Preserve camera audio** without changing their streams. Choose preservation
when listening, recording audio or using Frigate audio detection. An update that
omits the setting retains its saved value.

The integration API accepts and returns `audio_mode`, either `video_only` or
`preserve`. Video-only exports append the native go2rtc `?video` selector to
CamAdmiral relay URLs. This prevents empty advertised audio tracks from stalling
live playback. The shared camera source, other consumers and integrations that
preserve audio keep their audio. Imported live aliases follow the same policy;
explicit audio conversion options are omitted only in video-only mode.

Changing the policy updates owned saved and runtime aliases without restarting
Frigate. Reopen existing live views to reconnect with the new tracks. Camera IDs,
history, detection tuning, recording policy and rollback snapshots stay intact.
Conflicting external source edits still require review. A manually configured
second recorder needs the corresponding URL changes separately.

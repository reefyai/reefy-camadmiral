# Bounded verification after camera address recovery

A recovered camera can pass direct validation while the newly restarted relay
is not yet ready to serve it. A single failed relay probe used to restore the
old address immediately and restart the shared relay again. Recovery now makes
at most three verification passes, separated by one second, retrying only
streams with transient errors. Each media probe retains its existing timeout.
All enabled streams, including profiles without a current consumer, must pass.
Authentication, invalid-media and no-video failures do not spend the transient
retry budget. Persistent failure still restores the previous sources and
records a failed revision. No additional restart is made between probe passes.

Diagnostics use `media-recovery` JSON records with a generated attempt ID,
camera and stream IDs, stage, status, attempt and latency. They never include
source URIs, profile tokens, passwords, addresses or exception messages. A
successful cutover commits probe health and both incident resolutions in one
SQLite transaction. Only the redundant pending generic recovery notification
is removed; the offline/authentication and address incident histories remain.
Ordinary health recovery outside address cutover retains its existing message.
There are no new API fields, incident types, settings or migration requirements.

Imported Frigate readers must also have current observations for every required
upstream producer before recovery is acknowledged. An observation gap waits
within the existing recording-verification window before any camera pause.
That window follows the relay/address generation and observed video format;
reconnecting a transport cannot repeatedly push its start past fresh recordings
while leaving its original deadline behind. Learning the first producer is a
baseline, and does not open a recovery deadline. Upgrade handling discards only
an already-acknowledged legacy baseline window; owned recovery budgets remain.
A committed physical address change always refreshes that camera's capture
once before acknowledgement. Current relay SDP and new segment timestamps do
not prove that an already-running recording process discarded old MP4 codec
metadata. The existing imported-identity E2E catches this by decoding retained
recordings and checking their dimensions. The ordinary address-refresh component
case also runs with healthy FPS, bytes and segments; it reproduces the skipped
refresh on unmodified source `d0165d134f2b6bb672a3b057626b7ebfb941c2bb`.
An internal address-verification journal keeps the recorder start epoch across
control-process changes and exhausted attempts. A later confirmed operator
restart can complete recovery after fresh output is observed; it cannot grant
more automatic pause attempts or accept a pre-restart recording. Exhausted and
blocked recovery component cases reproduce both failure modes on unchanged
source `09fffff324435aecb1b4430da111cfd01ea3edae`. The existing identity E2E also
reproduced the exhausted-budget case after verifying earlier physical moves and
old/new recording dimensions. The recorder epoch is journaled when the address revision is first observed,
before waiting for upstream producers. An operator restart during that grace
period must not become the old baseline later. The corresponding component
sequence and the final legacy-identity E2E reproduce that ordering failure on
unchanged source `1f4c9ab084ca41f155e10911d2c700502b8a649d`.
An owned capture resume also takes priority over recorder-epoch sampling: a temporarily unavailable statistics response must not leave a camera paused. The corresponding failure control reproduces a retained OFF state on the same unchanged source.
The imported-identity driver's operator-aware readiness loop also requires the
expected validated physical address, both identity owners and all enabled
profiles before leaving that loop. A prior address or tentative profile rewrite
cannot count as recovery. SQLite-backed checkpoint regressions reproduce the
premature exit on unchanged source
`47b6b2d811218493c1aa665a7cc66852a158c998`; negative identity, health and partial
ownership controls remain mandatory. The driver also waits until every reader has observed the current committed
address before acting on a manual-restart warning. A warning from a previous
move cannot trigger an operator restart before the new recorder epoch is
journaled. SQLite-backed negative cases reproduce that stale-warning race on
unchanged source `b46e337f687f32a2bd77beb62e3b018296f3c5dc`, including a partially
observed pair and a tentative identity. Both checkpoint corrections preserve
the application's conservative capture-recreation proof. The overall
240-second budget is unchanged.
No background recorder restart is added.
Reader diagnostics report observation availability, codec-parameter counts,
recording-window bounds and the segment that verified recovery, without SDP or
source details. Background recovery still cannot restart Frigate.

The existing self-hosted release gate runs `e2e/recovery_verification.py` in its
private Docker guest. It builds both immutable baseline
`ac7403dbc3ec8e7f5c3611e57e70eb0267799e45` and the exact candidate source. Both use
the same three-profile camera, RTSP proxy and assertions. Only the relay-facing
RTSP boundary is delayed for nine seconds after restart; direct upstream
validation remains real and successful. The baseline must reproduce premature
rollback. The fixed application must recover with one relay restart and one
recovery notification, preserve identities and retained encoded media, and
decode five frames from every enabled profile. Separate permanent relay and
bad-credential controls verify rollback/no false recovery and no unauthorized
cutover. Actual fault counts, revisions, application logs and exact source
provenance are retained in `address-recovery-verification/result.json` and
normal Actions artifacts. The synthetic notification provider has no external
route; outbox behavior is exercised without contacting a real operator.

Component regressions additionally cover selective retries, bounded attempts,
secret-free diagnostics for every profile, a concurrent outbox reader,
transaction rollback, idempotency and rejection of incomplete profile results.

The existing imported-identity E2E reproduced reader verification failures on
source `4f453ff0c12f7039c1ce63ed03b3ca40b2c603e2` in release-gate run
`45864137283` and its failed-job retry. Its physical-source dimensions, decoded
old/new recordings, retained history and 240-second recovery budget remain
mandatory. Six additional reader-window component cases run against that
unmodified application source: five fail at the intended assertions, while the
owned-budget negative control passes. They cover transport churn, format
changes, absent observations, first-observation baselines and legacy-state
compatibility. Run them with the existing suite:

```sh
PYTHONPATH=tests python -m unittest test_frigate_import
```

Run the normal full gate, retaining its default parallel jobs:

```sh
gh workflow run release-gate.yml --repo reefyai/reefy-camadmiral \
  --ref fix/reliable-address-recovery -f force_full=true -f github_hosted=false
```

This is not a claim that camera network faults, connection limits or slow
keyframes have been diagnosed or eliminated. Unavailable sources remain visible
and retries remain bounded. Real-device release qualification follows the same
published image from dev to production and preserves app data and identities.

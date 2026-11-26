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

Run the normal full gate, retaining its default parallel jobs:

```sh
gh workflow run release-gate.yml --repo reefyai/reefy-camadmiral \
  --ref fix/reliable-address-recovery -f force_full=true -f github_hosted=false
```

This is not a claim that camera network faults, connection limits or slow
keyframes have been diagnosed or eliminated. Unavailable sources remain visible
and retries remain bounded. Real-device release qualification follows the same
published image from dev to production and preserves app data and identities.

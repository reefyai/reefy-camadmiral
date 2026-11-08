# Imported reader recovery validation

Use only the synthetic fixtures in the established release gate. The application
baseline is `28ee79068ea25017fe670bc9a68808d0528aea5c`.

Run locally:

```sh
python -m unittest discover -s tests
```

Run the complete release-gate workflow with `force_full=true` and its default
parallel jobs. The permanent `e2e/frigate_identity.py` driver checks complete new
recordings, decoded frames, exact source dimensions, unchanged credentials,
identities, stream URLs, saved Frigate settings and historical recording rows.
It deliberately returns 503 for runtime controls, requires a verified instance
restart, restores normal controls, and repeats camera address changes. It checks
both readers after shared relay restarts, including an unchanged-address peer.
Normal CI artifacts retain source provenance, JSON results, recordings and logs.

## Baseline evidence

Run [42512567604](https://github.com/reefyai/reefy-camadmiral/actions/runs/42512567604),
job 128828632535, uses validation commit
`9fec03c60a290d8108509e9485447dde1795b6b9`. Its only difference from the baseline
is the codified identity driver; application source, Dockerfile and dependencies
are unchanged. It imports both channels, passes the first address change, then
fails after a control restart because both channels do not save complete fresh
recordings. The `frigate-identity-evidence` and Docker readiness artifacts retain
the recordings, JSON and logs. Infrastructure/import-path failures from earlier
attempts are excluded from behavioral evidence.

The shared-relay and selected-camera serialization assertions were also run
against runtime source `1c688e021272b1042792dfffb3f069303cf3acf1` before the
correction: the unchanged peer was not refreshed and downstream recovery ran
while the shared media lock was held elsewhere. Both assertions pass with the
correction, alongside the new persisted-generation test and the full local suite
(621 tests). Repeat the focused checks with:

```sh
PYTHONPATH=tests python -m unittest \
  test_frigate_import.FrigateImportTests.test_shared_relay_restart_refreshes_unchanged_peer_without_config_writes \
  test_app.FrigateRelayRecoverySerializationTests
```

The storage test covers generation persistence with notifications disabled and
schema reinitialization. Other tests cover offline source deferral, operator
source conflicts and runtime pauses, uncertain requests, alias restoration,
pending resume recovery and verification-only retries without repeat restarts.
A desired-source revision alone must not reconnect a reader.

Final release-gate, image and device verification results belong in PR validation
notes with their exact source revision and artifact digest. Local success alone
does not establish device readiness.

## Fallback guard regression

Candidate `f7317d736f7b388e46ea09c5d475e302860d113e`, release-gate run
[42514384691](https://github.com/reefyai/reefy-camadmiral/actions/runs/42514384691),
passed the first physical-address move and decoded new recordings from both
channels. Its forced control outage exposed an incorrect default in the restart
guard: detection defaults to false in both supported Frigate versions, rather
than true. The guard blocked the fallback as an apparent runtime override.
The normal `frigate-identity-evidence` artifact retains that failure and state.

The exact defaults are defined in upstream
[0.17.2](https://github.com/blakeblackshear/frigate/blob/v0.17.2/frigate/config/camera/detect.py)
and [0.18.2](https://github.com/blakeblackshear/frigate/blob/v0.18.2/frigate/config/camera/detect.py).
Three assertions fail against that candidate and pass after correction: default
detection disabled permits fallback; runtime detection enabled above the disk
default prevents fallback; and interrupted resume with unavailable controls uses
the same guarded restart path. All 624 local unit/component tests pass.
The identity driver is unchanged, so the same-driver baseline remains valid.

## Confirmed identity recovery under contention

Run [42520515873](https://github.com/reefyai/reefy-camadmiral/actions/runs/42520515873)
repeated the successful forced fallback and the first four recovery checkpoints,
but the final operator-confirmed move exceeded the unchanged readiness deadline.
Saved state showed moved sources and committed reader journals; it did not retain
every predicate needed to identify the last unmet readiness condition.

A separate regression against `73c043f3d3306213fa976d47b3a997499ed6d6d4`
reproduces a scheduling gap in that path: a confirmed identity's immediate source
recovery returns without running if the shared media lock is busy, and successful
manual recovery does not wake downstream reconciliation. The correction waits
for the lock in its existing background worker and queues Frigate reconciliation
after a successful source commit. A failed source recovery does not queue it.
All recovery deadlines and readiness/data-preservation assertions are unchanged.
Failure artifacts additionally retain source health, address-event revisions and
the shared relay generation to distinguish pending predicates precisely.

## Asynchronous observations across relay restart

Run [42523223876](https://github.com/reefyai/reefy-camadmiral/actions/runs/42523223876)
again passed forced fallback and four recording checkpoints. The retained final
predicates identify the unmet condition: all streams were healthy, both reader
journals matched the current address events and relay generation, but two latest
probe results were unavailable. No readiness deadline or assertion was changed.

A codified unit regression against `45b55acded5e50a6f235866bce35da3e30abe3d0`
reproduces asynchronous snapshot work from an old shared relay generation
replacing recovered observations for an unchanged source URI. Both an old
failure and an old successful thumbnail were incorrectly committed. Snapshot
work now captures the private relay generation; results from an older generation
are discarded. Old futures remain in the bounded worker set until they complete,
and new-generation failures are still reported. The same regression passes
with the correction, alongside all 630 local unit/component tests.

## Delete acknowledgement after runtime removal

Run [42526216070](https://github.com/reefyai/reefy-camadmiral/actions/runs/42526216070)
passed the main suite and repeated the forced fallback, but the final readiness
check still timed out. Its transcript identifies an avoidable second capture
reset: DELETE of an owned alias returns HTTP 400 with `yaml: path not exist`,
then the same camera is paused again on the next reconciliation cycle.

The ordering is explicit in upstream
[go2rtc](https://github.com/AlexxIT/go2rtc/blob/v1.9.14/internal/streams/api.go):
DELETE removes the runtime stream before patching its YAML entry. The
[Frigate wrapper](https://github.com/blakeblackshear/frigate/blob/v0.17.2/frigate/api/camera.py)
forwards the latter error even though runtime deletion already happened.
The regression against `5e839b307f7ab6aa13753c1738529bbb38ef1985` reproduces
that exact acknowledgement and the unwanted second reset.

Recovery now accepts only that specific HTTP 400/message combination, and only
when a fresh runtime read proves the owned alias is absent. It continues cleaning
up the remaining owned aliases and restores every source. Other errors, unknown
outcomes and aliases still present remain retryable failures. Tests retain raw
configuration preservation and verify that the next cycle does not pause capture
again. All 632 local unit/component tests pass. Deadlines, exact playback checks,
readiness baselines and historical-data assertions remain unchanged.

## Fresh stream replacement after capture stops

Run [42536699853](https://github.com/reefyai/reefy-camadmiral/actions/runs/42536699853)
still deferred an alias refresh because the fresh runtime response retained the
alias after the YAML DELETE error. Repeating the pause consumed the final recovery
window. The verified-absence workaround therefore did not resolve the blocker.

Recovery now uses the existing go2rtc PUT directly after capture is stopped.
Upstream [New](https://github.com/AlexxIT/go2rtc/blob/v1.9.10/internal/streams/streams.go)
constructs a new stream object and replaces the name's runtime mapping. With the
old FFmpeg consumers already stopped, this discards cached SDP without requiring
DELETE of a separate YAML path. Stable alias names, source ownership, raw Frigate
configuration, capture-resume journaling and restart fallback are preserved.

The behavior regression fails against `c10fe5875cd47150fce88f5636c5e8b41070659d`
and passes with direct replacement. It requires successful recovery despite the
observed DELETE fault, exact owned-source replacements, stop/resume ordering,
unchanged saved configuration, and no repeated pause on the next sync. Negative
cases still require ambiguous and rejected replacements to attempt every owned
source, restore capture, retain the pending revision and retry safely. Tests of
DELETE calls were updated to verify those behavior guarantees for replacement;
all actual playback, source-dimension, readiness/deadline and history assertions
in the codified identity driver are unchanged. All 632 local tests pass.

## Bounded repair without changing the established cache reset

The unchanged-source retry in run 42539372425 passed all five identity recovery
checkpoints and ten playback checks, including the real restart fallback.
However, the main moved-DNS playback check failed twice under direct replacement.
Direct PUT therefore did not establish the required compatibility.

The final sequence retains delete/recreate. For the specific missing-YAML HTTP
400 only, it seeds that owned alias with its unchanged source and retries DELETE
once while capture is already stopped. This normalizes the runtime/YAML state
instead of waiting for another capture reset. Every alias is restored afterward;
failed reseeding, failed second deletion and unrelated errors retain pending
state and resume capture. A whole-file rollback was avoided: targeted edits
preserve the current negative tests, with a recovery reference retaining the
previous source tree.

The new bounded-repair assertions fail against f20156f and pass after the
correction. They require one pause, exact source ownership, one bounded retry,
complete alias restoration, unchanged saved settings, no repeat on the next
cycle, and preserved errors if repair fails. All 634 local tests pass. Existing
physical identity, exact decoded dimensions, real recording, historical-data and
readiness/deadline assertions remain unchanged.

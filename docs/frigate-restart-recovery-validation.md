# Frigate restart and restoration validation

A recorder restart can outlast the importer's previous 90-second camera wait.
The import then attempted restoration once while the API remained unavailable,
leaving its encrypted recovery journal and paused original reader in place.

The importer now separates restart readiness from media verification. It allows
180 seconds for a new service start and working configuration API, then applies
the existing 90-second capture and Record-reader checks. Healthy statistics from
the old process do not acknowledge a restart. Startup read failures can be
retried; rejected configuration writes and authorization failures remain errors.

Foreground rollback, manual restoration, and startup recovery share a bounded
180-second retry window for temporary API failures and uncertain saves. Every
attempt reloads the encrypted journal and rechecks ownership against current
configuration. Conflicting operator edits remain protected. When saved sources,
running inputs, enabled state and positive capture already match the original,
recovery clears its journal without another configuration write or restart.
The retained camera data and recording history are preserved.

## Permanent coverage

`tests/test_frigate_import.py` covers slow handoff and restoration, old-process
uptime rounding and positive capture, transient read failures, lost save replies,
permanent write/access rejection, bounded persistent outages, operator edits
during an outage, and repeat startup recovery without another restart.

`e2e/frigate_restart_recovery.py` uses real Frigate 0.17.2 and 0.18.2 with synthetic
single-reader cameras. The release gate runs both versions in parallel in its
existing private Docker labs. Its fault proxy holds the API unavailable for
110 seconds after an accepted restart, rejects the first restoration write with
a temporary outage, and interrupts CamAdmiral after a restoration restart.
It requires successful takeover, complete playable new relay recordings,
successful slow restoration, automatic rollback after failed source validation,
a recovered journal with no extra restart, protected operator edits, unchanged
final camera/group/source configuration, preserved recordings/events/reviews,
and decoded historical and fresh video from every original camera.

Normal `restart-recovery-<version>` artifacts retain `result.json`, driver state,
container logs, transcripts, exact checkout/application tree, and immutable image
identities. No customer cameras or customer data are used.

## Baseline provenance

Unchanged application: `6e8baf7c59d669dbeb08be9405d7e859295f25f4`.
Tests-only revision: `3fa25174163013861032c6a4b4827a365861cf7b`.
Both application trees are `c557c54cecaa43d70e0ab87e49f62591529abf4d`.

[Baseline run 42654665767](https://github.com/reefyai/reefy-camadmiral/actions/runs/42654665767)
verified synthetic captures, exhausted physical reader slots, real recordings and
seeded history on Frigate 0.17.2, then reproduced `rollback_required` with
`restore_required`, no adopted camera, and 91 unavailable API reads. Its actual
PR-merge checkout was `effbd7f5b259b7c70ef2fe46660b751c035f57a3`, with the same
application tree. The corresponding 0.18.2 attempts failed during image download
with a TLS transport error and are excluded from reproduction evidence.
Unrelated baseline jobs were cancelled after evidence was retained.

Repeat the same durable lab, rather than a temporary reproduction:

```sh
python -m unittest discover -s tests
FRIGATE_TEST_VERSION=0.17.2 python3 e2e/frigate_restart_recovery.py
FRIGATE_TEST_VERSION=0.18.2 python3 e2e/frigate_restart_recovery.py
```

Each E2E command requires its own established private Docker lab. The full
release gate retains its default parallel scheduling and all existing readiness,
media, memory, and history assertions. Final fixed-run links and source/artifact
provenance are recorded in the pull request validation notes.

## Identity-cache race exposed by the complete gate

[Gate 42658038090](https://github.com/reefyai/reefy-camadmiral/actions/runs/42658038090)
exposed the existing identity assertion when a discovery-cache entry arrived
between two imports of channels from one physical device. Each fresh scan
verified the same physical keys, but one link used a deterministic identifier
and the other used the new cache identifier. The new unit case reproduces the
mismatch on `dc9550afc88f41be82c66620f965b7257ed9edeb`.

Identity learning now reuses a uniquely verified peer anchor for the same
physical device. Fresh physical verification remains mandatory, ambiguous
anchors remain ineligible, and independent logical camera IDs, roles and
credentials are preserved. Different verified devices remain separate. The
existing identity E2E assertion and all its deadlines remain unchanged; initial
anchor metadata is retained for future diagnosis.

The first fixed-run matrix passed slow takeover with decoded new recording,
slow restoration, operator-edit protection, and retried automatic rollback on
both versions. Its interruption case exposed a lab topology error: the proxy
shared the application's network namespace, so restarting the application
removed its localhost API path. The isolated restart lab now gives that proxy
an independent network endpoint and explicitly requires its outage state to
survive application interruption. Product assertions and deadlines are retained.

The complete gate now runs its independent recording-continuity, broad import,
and isolated UI labs as separate parallel jobs. Their test commands, fixture
builders, private Docker isolation, resource limits, deadlines and assertions
are unchanged. Unit/component checks and the publisher's existing isolated-UI
step remain in the main gate job. Each lab retains its own normal artifacts.

## Upstream reconnect recovery

[Gate 42661870523](https://github.com/reefyai/reefy-camadmiral/actions/runs/42661870523)
passes both complete restart matrices and identity recovery, but its unchanged
DNS-move assertion exposes stale downstream codec metadata: positive capture
and packet counters coexist with decoded frames at the old dimensions.
A focused unit case reproduces the missing reader refresh on unchanged source
`ad1339b089849b1e3f47c619d547d637eb0ab44d`.

The importer now observes active upstream connection identities through the
existing local relay statistics API, without opening camera connections. A
changed required source refreshes both owned readers while preserving saved
configuration. Recovery acknowledges the connections present after capture and
Record resume, preventing a loop caused by its own pause. Missing samples remain
pending, snapshot-only/idle sources are ignored, disabled recording is respected,
and failed targeted media verification uses the existing guarded restart fallback.

Connection markers remain private inside encrypted import journals. The broad
DNS test retains its original dimensions, decoded-frame and 180-second media
assertion; capable versions additionally retain and verify source-generation
acknowledgement. Historical baselines keep the original behavioral assertion.

The first reconnect correction exposed a scheduling interaction in the existing
multi-channel address-move gate: blocking capture/Record waits on one camera
could delay recovery of its peer beyond the unchanged 240-second deadline.
A permanent unit case reproduces that starvation. Media verification now polls
pending recovery across reconciliation cycles, allowing peers to progress while
retaining the 90-second verified-media fallback deadline. The identity gate also
requires the new private pending marker to clear before declaring readiness.
Its deadline and decoded historical/fresh playback requirements are unchanged.

The import UI retries only an explicitly rejected `sync_busy` submission, with
its exact original selection and restart consent, for at most 90 seconds. Closing
the dialog stops unaccepted retries; authorization and other rejections remain
immediate errors. The existing UI import test injects a genuine busy response
and still requires only the failed camera to be retried and the original final
results within its unchanged deadline. Functional JavaScript coverage verifies
selection preservation, cancellation, permanent errors and the retry bound.

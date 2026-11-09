# Grouped camera import and generated-YAML reader recovery

The multi-selection import releases selected original readers before enabling
new relays, validates relayed video, saves successful peers together, and shares
one final restart. Targets lacking runtime controls use one shared release
restart plus the final restart. Partial failures restore their original sources
without undoing successful peers. The private encrypted journal protects camera
IDs, original credentials, roles, settings, and recording history across process
loss. This adds no manifest, public API, CLI, or version-format fields.

Generated go2rtc YAML can reject a DELETE after its live stream is already
removed. Recovery acknowledges that result only after observing the alias absent.
Control reads bypass Frigate's short-lived API cache so deletion and settings
guards observe current server state. An alias that remains after one seed and
bounded retry uses the existing guarded
whole-instance restart fallback. Unrelated errors and operator overrides retain
their existing protections.

## Durable regression runner

The established parallel release gate includes `batch-import-regression.yml`:
batch cutover and alias recovery, each against Frigate 0.17.2 and 0.18.2. It reuses
the existing private Docker lab, synthetic session-limited RTSP sources and
recording fixtures. It requires:

- One shared final restart for a supported group, guarded operator edits and
  unchanged unselected cameras and dashboard groups.
- Process interruption during an accepted restart, with original camera IDs
  retained and no duplicate accepted restart.
- Alias deletion faults both after actual removal and before removal, checking
  bounded retries, recovery acknowledgement and absence of refresh loops.
- Complete fresh recordings from every original camera, real decoded frames
  from both historical and new recordings, and preserved event/review rows.

Unit coverage adds shared fallback restart limits, partial relay failure,
restoration after release, accepted restart with a temporary API outage,
interrupted grouped saves, operator edits, runtime controls, changed integration
addresses and journal retention beyond the ordinary preview limit. Cameras whose
YAML omitted `enabled` are saved explicitly enabled during grouped cutover to
clear Frigate 0.18's persisted runtime OFF override; restoration recovers the
original omitted field. The interruption matrix retains its original assertion.

Repeatable commands:

```sh
python -m unittest discover -s tests
FRIGATE_BATCH_CASE=batch FRIGATE_TEST_VERSION=0.17.2 python3 e2e/frigate_batch.py
FRIGATE_BATCH_CASE=batch FRIGATE_TEST_VERSION=0.18.2 python3 e2e/frigate_batch.py
FRIGATE_BATCH_CASE=recovery FRIGATE_TEST_VERSION=0.17.2 python3 e2e/frigate_batch.py
FRIGATE_BATCH_CASE=recovery FRIGATE_TEST_VERSION=0.18.2 python3 e2e/frigate_batch.py
```

Each E2E command uses its own private Docker lab. The full gate schedules all
existing and new jobs in parallel. Each `batch-<case>-<version>` artifact retains
machine-readable results, driver state, transcripts, container logs, exact source
and application tree, and immutable image identities. All fixtures are synthetic.

## Baseline provenance

Unmodified application: `e307fab5bb5faaffb59dd8458f382ea7ea14063a`.
Application tree: `5713362363da3cf50efbbc4ece14769e80d15115`.
The tests-only baseline preserves that exact application tree. Its initial run
[42701719632](https://github.com/reefyai/reefy-camadmiral/actions/runs/42701719632)
reproduced four restarts for two imported cameras on both Frigate versions.
That run also exposed missing WebSocket forwarding in the synthetic fault proxy,
which forced restart fallback rather than exercising real runtime controls.
The corrected proxy permits real pause/resume controls only in this regression;
existing slow-restart scenarios retain their deliberate fallback behavior.

The corrected tests-only source is
`76240e970cc8bdf5fdecd35b3a81ede81c151ca5`, with the same unmodified
application tree. Baseline/fixed job results, exact tested commit and application
artifact provenance are recorded in the pull request validation notes. The
complete release gate must pass for the exact immutable release candidate before
publication; a focused baseline run cannot satisfy the publisher's full gate.

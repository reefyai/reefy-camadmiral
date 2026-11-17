# Independent endpoint maintenance and reader recovery

Imported source reconciliation previously mixed link updates with blocking
reader recovery. An accepted recorder restart whose capture verification stayed
pending stopped each pass at the same row. Later imports kept old relay host
addresses even after the host network was ready.

Endpoint maintenance now visits all selected, enabled imports before reader
recovery. It checks source ownership, batches only the owned alias changes, and
isolates per-camera planning and runtime-write errors. Failed runtime updates
retain the previous ownership metadata so the next pass retries them. Reader
recovery rotates between cameras across passes. Accepted recorder-wide restarts
are shared state and are verified before another camera can request a restart.

An endpoint update can change the configuration protected by an accepted
restart's journal. The journal records the exact expected configuration hash
before saving. Only that result may advance its guard, including after a lost
reply or process interruption. Unrelated operator edits remain conflicts.
Camera settings, camera groups, credentials, history and rollback snapshots are
preserved. This changes no public contract.

## Permanent regression coverage

The normal unittest runner discovers the host-change, fairness, pending-restart,
operator-edit, deselection, partial-runtime-write and lost-save-reply cases in
`tests/test_frigate_import.py`. The host-change case reproduces on the unchanged
application in tests-only commit `295dba0`, application tree
`50d132192804187cc8e3313d7b2b16b71ad4c8fb`.

The existing parallel grouped-import matrix runs `e2e/frigate_host_recovery.py`
inside its synthetic Docker lab on Frigate 0.17.2 and 0.18.2. It installs stale
owned endpoints and a pending recovery, requires every imported peer's saved
link to be repaired before recovery is attempted, preserves camera/group config,
and then requires live video again. The remainder of the established lab still
requires playable historical and fresh recordings and unchanged event/review
metadata. The lab resumes its API process even after an assertion fails.

Repeatable commands:

```sh
python -m unittest discover -s tests -v
FRIGATE_BATCH_CASE=batch FRIGATE_TEST_VERSION=0.17.2 python3 e2e/frigate_batch.py
FRIGATE_BATCH_CASE=batch FRIGATE_TEST_VERSION=0.18.2 python3 e2e/frigate_batch.py
```

The E2E commands require separate established private Docker labs. CI retains
the default parallel matrix; do not combine jobs into one shared Docker daemon.
Normal batch artifacts retain machine-readable results, exact application-tree
and image provenance, transcripts, driver state and container logs. Local unit
logs and a validation summary are under `e2e-artifacts/host-recovery/`.

The preliminary run 44082609688 was cancelled after correcting the lab's Python
module path and is excluded from reproduction evidence. The finalized baseline
run is [44083061754](https://github.com/reefyai/reefy-camadmiral/actions/runs/44083061754).
Fixed-run results are recorded with the review notes after the matrix finishes.

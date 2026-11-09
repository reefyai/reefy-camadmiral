# Authenticated direct-input import validation

Frigate's normal `/api/config` response masks credentials in camera FFmpeg
input paths. Both Frigate 0.17.2 and 0.18.2 expose resolved, unmasked input
paths through their existing admin-only `/api/config/raw_paths` response.
The importer previously read direct inputs from the masked response, while
already reading go2rtc alias sources from the admin response. Real protected
direct inputs therefore failed authentication even when the original recorder
was healthy.

The importer now combines effective camera metadata with admin input paths.
Saved YAML remains the rollback/configuration snapshot; resolved admin paths
support environment-variable credentials. If a newly saved camera is absent from runtime paths until restart, the
importer uses its trusted admin-only saved paths. Missing trusted inputs or
unresolved variables make it ineligible; masked metadata is never a fallback. No public
endpoint, manifest, version format, or response field changes.

## Baseline reproduction

Unmodified application: `9d33ec8371b47495d8815d88ce7f8e410f727df0`.
Regression-only revision: `7458c86f80f9237bed3254795227318211e74dd4`.
Both have application tree `d7e80d4d340a231029f7f6f551efefa3b8b6a7f5`;
Dockerfile and runtime requirements are unchanged.

[Baseline run 42631553252](https://github.com/reefyai/reefy-camadmiral/actions/runs/42631553252)
ran the two version jobs in parallel. Both verified real camera authentication,
rejection with a wrong password, API masking, complete pre-import recordings,
and seeded recording/event/review history. Valid import then failed with
`source_auth_failed`, without adopting a camera or changing saved Frigate
configuration. Other jobs were cancelled after the two failures were captured;
this is baseline reproduction evidence, not a successful full release gate.

Normal `direct-auth-import-0.17.2` and `direct-auth-import-0.18.2` artifacts
retain machine-readable results, exact application/source/image provenance,
fixture state, and logs. Earlier fixture setup/preflight attempts are excluded
from the reproduction result. They caught a conflicting cache mount, required
runner-cache settings, and encoded-credential incompatibilities in the fixture.
No application code was changed until both real import failures were confirmed.

Three focused unit cases also fail on the baseline: masked direct credentials,
resolved variables in saved YAML, and missing trusted admin paths. The complete import
unit suite passes after the fix. Two additional cached-job cases fail on the
initial fix and pass with upgrade recovery: stored masked credentials must be
recovered, and a changed source address must require a fresh preview.

## Repeatable checks

```sh
python -m unittest discover -s tests
FRIGATE_TEST_VERSION=0.17.2 python3 e2e/frigate_direct_auth.py
FRIGATE_TEST_VERSION=0.18.2 python3 e2e/frigate_direct_auth.py
```

Run each E2E command inside its own established private Docker lab. The release
gate schedules both version jobs in parallel, with the existing isolation,
readiness checks, memory limits and default parallel execution. The final
driver boots the unchanged baseline application, confirms the failed import,
then upgrades to the candidate using the same persistent database and retries
the exact failed job ID. Both versions exercise this sequence. It also reruns
wrong-password rejection against the candidate. The earlier standalone baseline
run above independently demonstrates the initial failure on both versions.

Persisted jobs containing masked credentials are refreshed only after the
original configuration guard passes. Source URIs, wrappers, roles and live
mappings must still match the saved plan; a changed source requires a new
preview. Existing adopted cameras are not rewritten by this recovery.

The fixed application must reject invalid credentials without changes, import
the valid direct Record/Detect inputs, preserve unrelated cameras and operator
settings/groups, retain historical recordings/events/reviews, decode historical
and complete fresh playback at the expected dimensions, and preserve stable
identities on repeat import. Final fixed-run results and exact commit/run links
belong in the pull request validation notes. No customer devices or data are
part of this lab.

The existing broader import E2E also exercises a camera saved with restart
pending. Runtime `raw_paths` does not contain that camera yet. The literal
source from the admin raw YAML must remain importable; unit coverage also
requires rejecting missing trusted inputs and unresolved variables when no
runtime path is available.

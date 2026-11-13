"use strict";

const assert = require("node:assert/strict");

const { classifyReleaseGate } = require("./release-gate-classifier.cjs");

function runClassifierTests() {
  const multiCommitRelayChange = classifyReleaseGate([
    { filename: "Dockerfile" },
    { filename: "camadmiral/app.py" },
    { filename: "docs/recovery.md" },
  ]);
  assert.deepEqual(
    {
      runtime: multiCommitRelayChange.runtime,
      e2e: multiCommitRelayChange.e2e,
    },
    { runtime: true, e2e: true },
    "a relay change anywhere in a multi-commit PR requires full E2E",
  );

  const renamedPatch = classifyReleaseGate([
    {
      filename: "third_party/go2rtc/patches/current.patch",
      previous_filename: "third_party/go2rtc/patches/previous.patch",
    },
  ]);
  assert.equal(renamedPatch.e2e, true, "renaming a go2rtc patch requires full E2E");

  for (const filename of ["camadmiral/frigate_batch.py", "e2e/frigate_batch.py", "camadmiral/frigate_import.py", "e2e/frigate_import.py",
    "camadmiral/watchdog.py", "e2e/stall_recovery.py", "camadmiral/camera_identity.py", "camadmiral/recovery.py", "e2e/frigate_identity.py",
    "e2e/frigate-identity-compose.yaml", "e2e/frigate_direct_auth.py",
    "e2e/frigate-direct-auth-compose.yaml", "e2e/rtsp_retention.py", "e2e/rtsp-retention-compose.yaml", "e2e/fixtures/rtsp-retention-relay.yaml", "e2e/frigate_runtime_resume.py", "e2e/runtime_resume_control.py", "e2e/runtime_resume_supervisor.py", "e2e/frigate-runtime-resume-compose.yaml", "e2e/fixtures/frigate-runtime-resume.yml"]) {
    assert.equal(classifyReleaseGate([{ filename }]).e2e, true,
      "import implementation and regression changes require full E2E");
  }

  const documentationOnly = classifyReleaseGate([
    { filename: "README.md" },
    { filename: "docs/recovery.md" },
  ]);
  for (const filename of [".github/workflows/batch-import-regression.yml", "scripts/ci_docker.py", ".github/actions/docker-lab/start.js",
    ".github/actions/docker-lab/action.yml", ".github/workflows/release-gate.yml",
    ".github/workflows/stalled-snapshot.yml", ".github/workflows/memory-pressure.yml",
    ".github/workflows/relay-latency.yml", ".github/workflows/rtsp-retention.yml", ".github/workflows/frigate-runtime-resume.yml"]) {
    assert.equal(classifyReleaseGate([{ filename }]).e2e, true,
      "Docker lab and runner changes require the full parallel gate");
    assert.equal(classifyReleaseGate([{ filename: "docs/retired.md", previous_filename: filename }]).e2e,
      true, "renaming Docker lab infrastructure cannot skip full E2E");
  }
  assert.deepEqual(
    { runtime: documentationOnly.runtime, e2e: documentationOnly.e2e },
    { runtime: false, e2e: false },
    "documentation-only PRs skip runtime validation",
  );

  const incomplete = classifyReleaseGate(
    [{ filename: "camadmiral/app.py" }],
    false,
  );
  assert.equal(incomplete.runtime, true);
  assert.equal(incomplete.e2e, true, "an incomplete file listing fails closed");
}

if (require.main === module) {
  runClassifierTests();
  console.log("release-gate classifier tests passed");
}

module.exports = { runClassifierTests };

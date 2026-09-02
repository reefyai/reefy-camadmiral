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

  const documentationOnly = classifyReleaseGate([
    { filename: "README.md" },
    { filename: "docs/recovery.md" },
  ]);
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

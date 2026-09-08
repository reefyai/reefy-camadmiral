"use strict";

function documentationPath(path) {
  return path === "README.md" || path.startsWith("docs/");
}

function classifyReleaseGate(files, complete = true) {
  const documentationOnly = files.length > 0 && complete && files.every(file =>
    documentationPath(file.filename) &&
    (!file.previous_filename || documentationPath(file.previous_filename))
  );
  if (documentationOnly) {
    return {
      runtime: false,
      e2e: false,
      reason: "Documentation-only commit: skipping runtime validation.",
    };
  }

  const releaseCandidate = complete && files.some(file =>
    file.filename === "VERSION" || file.previous_filename === "VERSION"
  );
  const relayRuntimeChanged = complete && files.some(file =>
    file.filename === "Dockerfile" ||
    file.previous_filename === "Dockerfile" ||
    file.filename.startsWith("third_party/go2rtc/") ||
    (file.previous_filename || "").startsWith("third_party/go2rtc/")
  );
  const e2e = releaseCandidate || relayRuntimeChanged || !complete;
  let reason = "Development commit: running fast validation only.";
  if (!complete) {
    reason = "Large or incomplete change: running the complete E2E gate.";
  } else if (e2e) {
    reason = "Release or relay-runtime change: running the complete E2E gate.";
  }
  return {
    runtime: true,
    e2e,
    reason,
  };
}

module.exports = { classifyReleaseGate };

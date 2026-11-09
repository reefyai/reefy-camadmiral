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
  const importChanged = complete && files.some(file =>
    [file.filename, file.previous_filename].some(path =>
      path === "camadmiral/frigate_import.py" || path === "camadmiral/frigate_batch.py" || path === "e2e/frigate_batch.py" || path === "camadmiral/camera_identity.py" ||
      path === "camadmiral/recovery.py" || path === "e2e/frigate_import.py" || path === "e2e/frigate_direct_auth.py" || path === "e2e/frigate_restart_recovery.py" ||
      (path || "").startsWith("e2e/frigate-restart-recovery") || path === "e2e/frigate_restart_proxy.py" ||
      (path || "").startsWith("e2e/frigate-direct-auth") ||
      (path || "").startsWith("e2e/frigate-identity") || path === "e2e/frigate_identity.py"
    )
  );
  const dockerLabChanged = complete && files.some(file =>
    [file.filename, file.previous_filename].some(path =>
      path === "scripts/ci_docker.py" ||
      (path || "").startsWith(".github/actions/docker-lab/") ||
      ["batch-import-regression.yml", "release-gate.yml", "stalled-snapshot.yml", "memory-pressure.yml", "relay-latency.yml"]
        .some(name => path === `.github/workflows/${name}`)
    )
  );
  const e2e = releaseCandidate || relayRuntimeChanged || importChanged || dockerLabChanged || !complete;
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

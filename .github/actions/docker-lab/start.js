const { spawnSync } = require("node:child_process");
const path = require("node:path");
const result = spawnSync("python3", [path.resolve(__dirname, "../../../scripts/ci_docker.py"), "start"], { stdio: "inherit" });
process.exit(result.status ?? 1);

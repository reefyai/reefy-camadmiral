const assert = require("node:assert/strict");
const { releaseSource, successfulGate, releaseTag } = require("./release-source.cjs");
const source = "a".repeat(40), other = "b".repeat(40);
const passed = { conclusion: "success", steps: [{ name: "Run isolated E2E lab", conclusion: "success" }] };

function client(runs, jobs) {
  const actions = { listWorkflowRuns: "runs", listJobsForWorkflowRun: "jobs" };
  return { rest: { actions }, paginate: async (method, args) => {
    if (method === "runs") {
      assert.equal(args.workflow_id, "release-gate.yml");
      assert.equal(args.status, "success");
      return runs;
    }
    assert.equal(args.filter, "latest");
    return jobs[args.run_id];
  }};
}

async function main() {
  assert.equal(releaseSource("", source), source);
  assert.equal(releaseSource(other, source), other);
  for (const value of ["main", "a".repeat(7), "", "A".repeat(40), "a".repeat(41)]) {
    assert.throws(() => releaseSource(value, "invalid"));
  }
  await assert.rejects(successfulGate(client([{ id: 1, head_sha: other }], { 1: [passed] }), "synthetic", "application", source));
  for (const jobs of [[], [{ conclusion: "success", steps: [] }], [{ ...passed, conclusion: "failure" }],
    [{ conclusion: "success", steps: [{ name: "Run isolated E2E lab", conclusion: "skipped" }] }]]) {
    await assert.rejects(successfulGate(client([{ id: 1, head_sha: source }], { 1: jobs }), "synthetic", "application", source));
  }
  const match = await successfulGate(client([{ id: 1, head_sha: source }, { id: 2, head_sha: source }],
    { 1: [], 2: [passed] }), "synthetic", "application", source);
  assert.equal(match.id, 2);
  const writes = [];
  // GitHub's commits endpoint returns 422 for an unknown ref. Tag absence is
  // instead a 404 from git.getRef; keep the real API boundaries distinct.
  const tags = { rest: { repos: { getCommit: async () => { throw { status: 422 }; } },
    git: { getRef: async () => { throw { status: 404 }; }, createRef: async args => writes.push(args) } } };
  await releaseTag(tags, "synthetic", "application", "v2000.01.01-00", source);
  assert.equal(writes.length, 0, "Preflight must never create tags");
  await releaseTag(tags, "synthetic", "application", "v2000.01.01-00", source, true);
  assert.deepEqual(writes, [{ owner: "synthetic", repo: "application", ref: "refs/tags/v2000.01.01-00", sha: source }]);
  tags.rest.git.getRef = async () => ({ data: { object: { type: "commit", sha: source } } });
  await releaseTag(tags, "synthetic", "application", "v2000.01.01-00", source, true);
  assert.equal(writes.length, 1, "An existing correct tag is retained");
  tags.rest.git.getRef = async () => ({ data: { object: { type: "tag", sha: "c".repeat(40) } } });
  tags.rest.git.getTag = async () => ({ data: { object: { type: "commit", sha: source } } });
  await releaseTag(tags, "synthetic", "application", "v2000.01.01-00", source, true);
  assert.equal(writes.length, 1, "Matching annotated tags are retained");
  tags.rest.git.getRef = async () => ({ data: { object: { type: "commit", sha: other } } });
  await assert.rejects(releaseTag(tags, "synthetic", "application", "v2000.01.01-00", source, true));
  assert.equal(writes.length, 1, "A conflicting tag is never moved");
  tags.rest.git.getRef = async () => { throw { status: 403 }; };
  await assert.rejects(releaseTag(tags, "synthetic", "application", "v2000.01.01-00", source, true));
  assert.equal(writes.length, 1, "Permission and network errors must not create tags");
  await assert.rejects(releaseTag(tags, "synthetic", "application", "latest", source, true));
  console.log("Exact-source release gate checks passed");
}
main().catch(error => { console.error(error); process.exitCode = 1; });

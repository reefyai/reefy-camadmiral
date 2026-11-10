const SHA = /^[0-9a-f]{40}$/;

function releaseSource(input, current) {
  const source = input || current;
  if (!SHA.test(source)) throw new Error("Release source must be an exact full commit SHA");
  return source;
}

async function successfulGate(github, owner, repo, source) {
  const runs = await github.paginate(github.rest.actions.listWorkflowRuns, {
    owner, repo, workflow_id: "release-gate.yml", status: "success", per_page: 100,
  });
  for (const run of runs.filter(run => run.head_sha === source)) {
    const jobs = await github.paginate(github.rest.actions.listJobsForWorkflowRun, {
      owner, repo, run_id: run.id, filter: "latest", per_page: 100,
    });
    if (jobs.some(job => job.conclusion === "success" && (job.steps || []).some(step =>
      step.name === "Run isolated E2E lab" && step.conclusion === "success"
    ))) return run;
  }
  throw new Error(`Commit ${source} has not passed the complete CamAdmiral E2E gate`);
}

async function releaseTag(github, owner, repo, version, source, create = false) {
  if (!/^v\d{4}\.\d{2}\.\d{2}-\d{2}$/.test(version)) throw new Error("Invalid first-party release version");
  let existing;
  try {
    existing = await github.rest.git.getRef({ owner, repo, ref: `tags/${version}` });
  } catch (error) {
    if (error.status !== 404) throw error;
    if (create) await github.rest.git.createRef({ owner, repo, ref: `refs/tags/${version}`, sha: source });
    return;
  }
  let object = existing.data.object;
  for (let depth = 0; object.type === "tag" && depth < 5; depth++) {
    object = (await github.rest.git.getTag({ owner, repo, tag_sha: object.sha })).data.object;
  }
  if (object.type !== "commit" || object.sha !== source) {
    throw new Error("Existing release tag belongs to another source; refusing to move it");
  }
}

module.exports = { releaseSource, successfulGate, releaseTag };

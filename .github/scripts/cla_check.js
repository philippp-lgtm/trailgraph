// The CLA check of .github/workflows/cla.yml: sets the commit status `cla` on the head of a pull request.
// Success when the author is exempt (a bot or the repository owner) or has signed the current CLA.md version:
// either recorded in signatures.json on the cla-signatures branch, or by a comment on this pull request that
// holds exactly SIGN_SENTENCE (the signature is then recorded). Otherwise a failure, and one comment that
// explains how to sign. Runs from the base branch only; it never checks out or runs code of the pull request.
const CLA_VERSION = 1;
const SIGN_SENTENCE = `I have read the Contributor License Agreement (CLA.md, version ${CLA_VERSION}) and I agree to it.`;
const BRANCH = "cla-signatures";
const FILE = "signatures.json";
const MARKER = "<!-- cla-check -->";

module.exports = async ({ github, context }) => {
  const { owner, repo } = context.repo;
  const pr =
    context.eventName === "pull_request_target"
      ? context.payload.pull_request
      : (await github.rest.pulls.get({ owner, repo, pull_number: context.payload.issue.number })).data;
  if (pr.state !== "open") return "closed";

  const author = pr.user;
  const status = (state, description) =>
    github.rest.repos.createCommitStatus({
      owner,
      repo,
      sha: pr.head.sha,
      state,
      context: "cla",
      description,
      target_url: `${context.serverUrl}/${owner}/${repo}/blob/${pr.base.ref}/CLA.md`,
    });
  if (author.type === "Bot" || author.login === owner) {
    await status("success", "No CLA needed");
    return "exempt";
  }

  const file = await github.rest.repos.getContent({ owner, repo, path: FILE, ref: BRANCH });
  const signatures = JSON.parse(Buffer.from(file.data.content, "base64").toString("utf8"));
  if (signatures.some((s) => s.id === author.id && s.cla_version === CLA_VERSION)) {
    await status("success", `CLA version ${CLA_VERSION} signed`);
    return "signed";
  }

  const comments = await github.paginate(github.rest.issues.listComments, {
    owner,
    repo,
    issue_number: pr.number,
    per_page: 100,
  });
  const signing = comments.find((c) => c.user.id === author.id && (c.body || "").trim() === SIGN_SENTENCE);
  if (signing) {
    signatures.push({
      login: author.login,
      id: author.id,
      cla_version: CLA_VERSION,
      signed_at: signing.created_at,
      pull_request: pr.number,
      comment_url: signing.html_url,
      comment_body: signing.body.trim(),
    });
    await github.rest.repos.createOrUpdateFileContents({
      owner,
      repo,
      path: FILE,
      branch: BRANCH,
      sha: file.data.sha,
      message: `Record the CLA version ${CLA_VERSION} signature of ${author.login} (pull request ${pr.number})`,
      content: Buffer.from(JSON.stringify(signatures, null, 2) + "\n").toString("base64"),
    });
    await status("success", `CLA version ${CLA_VERSION} signed`);
    return "recorded";
  }

  await status("failure", `Sign CLA version ${CLA_VERSION}: see the comment`);
  if (!comments.some((c) => (c.body || "").includes(MARKER))) {
    await github.rest.issues.createComment({
      owner,
      repo,
      issue_number: pr.number,
      body: [
        MARKER,
        `Thank you for your contribution, ${author.login}. Before it can be merged, please read the`,
        `[Contributor License Agreement](${context.serverUrl}/${owner}/${repo}/blob/${pr.base.ref}/CLA.md)`,
        "and agree to it by posting this exact sentence as a comment on this pull request:",
        "",
        "```text",
        SIGN_SENTENCE,
        "```",
        "",
        "You sign once; it covers all your contributions. Comment `recheck` to run the check again.",
      ].join("\n"),
    });
  }
  return "unsigned";
};

module.exports.SIGN_SENTENCE = SIGN_SENTENCE;

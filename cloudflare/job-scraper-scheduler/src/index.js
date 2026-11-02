const REQUIRED_ENVIRONMENT_KEYS = [
  "GITHUB_OWNER",
  "GITHUB_REPO",
  "GITHUB_WORKFLOW_ID",
  "GITHUB_REF",
  "GITHUB_ACTIONS_TOKEN",
];

const GITHUB_API_VERSION = "2022-11-28";
const USER_AGENT = "djinni-scraper-cloudflare-scheduler";

export function getWorkflowDispatchUrl(env) {
  return [
    "https://api.github.com/repos",
    encodeURIComponent(env.GITHUB_OWNER),
    encodeURIComponent(env.GITHUB_REPO),
    "actions/workflows",
    encodeURIComponent(env.GITHUB_WORKFLOW_ID),
    "dispatches",
  ].join("/");
}

export function validateEnvironment(env) {
  for (const key of REQUIRED_ENVIRONMENT_KEYS) {
    if (typeof env[key] !== "string" || env[key].trim() === "") {
      throw new Error(`Missing required Worker binding: ${key}`);
    }
  }
}

export async function dispatchGitHubWorkflow(
  env,
  { fetchImpl = fetch, log = console.log } = {},
) {
  validateEnvironment(env);

  const response = await fetchImpl(getWorkflowDispatchUrl(env), {
    method: "POST",
    headers: {
      Accept: "application/vnd.github+json",
      Authorization: `Bearer ${env.GITHUB_ACTIONS_TOKEN}`,
      "Content-Type": "application/json",
      "User-Agent": USER_AGENT,
      "X-GitHub-Api-Version": GITHUB_API_VERSION,
    },
    body: JSON.stringify({ ref: env.GITHUB_REF }),
  });

  if (response.status !== 204) {
    throw new Error(`GitHub workflow dispatch failed with HTTP ${response.status}`);
  }

  log("GitHub workflow dispatch succeeded", {
    owner: env.GITHUB_OWNER,
    repository: env.GITHUB_REPO,
    workflowId: env.GITHUB_WORKFLOW_ID,
    ref: env.GITHUB_REF,
    status: response.status,
  });
}

export default {
  async scheduled(_controller, env) {
    await dispatchGitHubWorkflow(env);
  },
};

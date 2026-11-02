import assert from "node:assert/strict";
import test from "node:test";

import worker, {
  dispatchGitHubWorkflow,
  getWorkflowDispatchUrl,
  validateEnvironment,
} from "../src/index.js";

const token = "github-secret-value-that-must-not-appear-in-logs";
const environment = {
  GITHUB_OWNER: "sqanatoliy",
  GITHUB_REPO: "djinni_scraper",
  GITHUB_WORKFLOW_ID: "145842968",
  GITHUB_REF: "main",
  GITHUB_ACTIONS_TOKEN: token,
};

function successfulFetch(calls) {
  return async (url, options) => {
    calls.push({ url, options });
    return { status: 204 };
  };
}

test("builds the configured GitHub workflow dispatch URL", () => {
  assert.equal(
    getWorkflowDispatchUrl(environment),
    "https://api.github.com/repos/sqanatoliy/djinni_scraper/actions/workflows/145842968/dispatches",
  );
});

test("dispatches the configured workflow with the required request contract", async () => {
  const calls = [];
  const logs = [];

  await dispatchGitHubWorkflow(environment, {
    fetchImpl: successfulFetch(calls),
    log: (...args) => logs.push(args),
  });

  assert.equal(calls.length, 1);
  assert.deepEqual(calls[0], {
    url: "https://api.github.com/repos/sqanatoliy/djinni_scraper/actions/workflows/145842968/dispatches",
    options: {
      method: "POST",
      headers: {
        Accept: "application/vnd.github+json",
        Authorization: `Bearer ${token}`,
        "Content-Type": "application/json",
        "User-Agent": "djinni-scraper-cloudflare-scheduler",
        "X-GitHub-Api-Version": "2022-11-28",
      },
      body: '{"ref":"main"}',
    },
  });
  assert.equal(logs.length, 1);
  assert.equal(JSON.stringify(logs).includes(token), false);
});

for (const status of [401, 403, 404, 422]) {
  test(`fails loudly for GitHub HTTP ${status} without exposing the token`, async () => {
    await assert.rejects(
      dispatchGitHubWorkflow(environment, {
        fetchImpl: async () => ({ status }),
      }),
      (error) =>
        error.message === `GitHub workflow dispatch failed with HTTP ${status}` &&
        !error.message.includes(token),
    );
  });
}

test("propagates network failures without logging the token", async () => {
  const logs = [];
  const networkError = new Error("network unavailable");

  await assert.rejects(
    dispatchGitHubWorkflow(environment, {
      fetchImpl: async () => {
        throw networkError;
      },
      log: (...args) => logs.push(args),
    }),
    networkError,
  );
  assert.equal(JSON.stringify(logs).includes(token), false);
});

for (const key of Object.keys(environment)) {
  test(`requires ${key}`, () => {
    const incompleteEnvironment = { ...environment, [key]: "" };

    assert.throws(
      () => validateEnvironment(incompleteEnvironment),
      new RegExp(`Missing required Worker binding: ${key}`),
    );
  });
}

test("scheduled handler invokes the shared dispatch routine", async () => {
  const calls = [];
  const originalFetch = globalThis.fetch;
  globalThis.fetch = successfulFetch(calls);

  try {
    await worker.scheduled({}, environment);
  } finally {
    globalThis.fetch = originalFetch;
  }

  assert.equal(calls.length, 1);
});

test("does not expose an HTTP fetch handler", () => {
  assert.equal(worker.fetch, undefined);
});

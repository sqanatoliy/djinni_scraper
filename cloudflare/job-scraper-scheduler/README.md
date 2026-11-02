# Cloudflare scheduler for the job scraper

This Worker has one responsibility: its `scheduled()` handler dispatches the
existing `Run Job Scrapers Hourly` GitHub Actions workflow on the `main` ref.
It does not expose a `fetch()` handler or any other public trigger.

## Schedule

The Cloudflare Cron Trigger is `37 * * * *` in UTC, which runs once per hour
at minute 37.

## Configuration

Non-secret workflow metadata is committed in `wrangler.toml`:

- repository: `sqanatoliy/djinni_scraper`
- workflow ID: `145842968`
- ref: `main`

`GITHUB_ACTIONS_TOKEN` is intentionally absent from source control and
`wrangler.toml`. Provision it only as a Worker secret:

```bash
gh auth token --hostname github.com --user sqanatoliy \
  | npx --yes wrangler@4.156.1 secret put GITHUB_ACTIONS_TOKEN --config cloudflare/job-scraper-scheduler/wrangler.toml
```

The token must authorize `POST /repos/{owner}/{repo}/actions/workflows/{workflow_id}/dispatches`.
The Wrangler version is pinned for this token-handling command.

## Local validation

```bash
npm test
npx --yes wrangler@4.156.1 deploy --dry-run --config wrangler.toml
```

To invoke the scheduled handler locally after supplying a temporary local
secret through an ignored `.dev.vars` file, run `wrangler dev --test-scheduled`
and request its scheduled endpoint. Do not commit `.dev.vars`.

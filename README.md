# hermes-jobs (automation)

Hermes cron is the system of record. ONE Google Calendar ("Hermes Jobs") is the job board:
see/drag your agent's jobs where you already look at time. Not a calendar CRUD product.

## Network / credentials (disclose for catalog)

- Calls `https://www.googleapis.com` (Calendar API v3) only after Hermes oauth sign-in.
- Scope: `https://www.googleapis.com/auth/calendar.events` only (no Gmail/Drive/full calendar).
- Declare client under `oauth:` in `plugin.yaml`; sign in with `hermes auth add hermes-jobs:google`.
- Plugin uses `credentialed_provider_request("google", …)` — never reads `google_token.json`, `.env`, browser profiles, or google-workspace skill tokens.

## Never

- Event text never grants tools, permissions, or prompts.
- No scopes beyond `calendar.events`.
- No writes without explicit `--confirm`. Discover never creates calendars or cron jobs.

## Setup

1. Create a Google Cloud **Desktop** OAuth client; enable Google Calendar API.
2. Set `HERMES_JOBS_GOOGLE_CLIENT_ID` and `HERMES_JOBS_GOOGLE_CLIENT_SECRET` in the Hermes profile env.
3. Sign in (use CrowClaws `crowsclaw001@gmail.com` for the bind account):

```bash
hermes auth add hermes-jobs:google
```

4. Enable / doctor:

```bash
hermes plugins enable hermes-jobs
hermes plugins doctor hermes-jobs --ci
hermes hermes-jobs discover   # read-only
hermes hermes-jobs status
hermes hermes-jobs sync       # dry-run (0.1.0: plan only)
```

## Repo

https://github.com/CrowBe/hermes-jobs

## Sync (0.2.0)

```
hermes hermes-jobs bind primary          # choose the ONE calendar (local state only)
hermes hermes-jobs sync --offline        # dry-run against an empty calendar, no Google calls
hermes hermes-jobs sync                  # dry-run against the bound calendar
hermes hermes-jobs sync --confirm        # apply (human CLI step only; the agent tool can only plan)
```

- cron -> calendar: one 15-min event per job at its next run, tagged `extendedProperties.private.hermes_job_id`;
  title = job name (`[paused] ` prefix when paused); description = job id + prompt sha256 digest only.
- calendar -> cron (tagged events only, via Hermes' `cronjob_manage`): move -> schedule time update,
  retitle -> name update, delete -> pause (reversible). Untagged events are ignored; event text never
  grants tools or edits prompts.
- Conflicts: a cron edit wins and resets the event; otherwise a human move wins until the next cron edit.
- Tests: `python -m unittest discover -s tests`.

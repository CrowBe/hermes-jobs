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
hermes hermes-jobs sync       # dry-run by default
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
- calendar -> cron (tagged events only, via Hermes' `cronjob_manage`): retitle -> name update;
  deleting an event **pauses**, never removes, its job. Untagged events are ignored and event text
  never grants tools or edits prompts.
- Conflicts: a cron edit wins and resets the event. A move never rewrites a recurring schedule.
- Tests: `python -m unittest discover -s tests -v` (planner + mocked runtime; no Google or real cron writes).

### Deleted events: pause and recovery

Every deletion reports `job <id> paused because its calendar event was deleted; resume with hermes cron resume <id>`.
The CLI JSON `ops`/`messages` and `status` show this information. In a dry-run it is a proposed action,
not a claim that the job is already paused. Applied sync state keeps `paused_by_delete` and the reason,
including for a job that was already paused. **Its deleted event is not recreated while paused.**
Resume explicitly in Hermes, for example `hermes cron resume 28c2169b1b51`; the next confirmed sync
recreates its event and clears the tombstone. Calendar cannot resume it because its event is gone.

### Moving one occurrence (important limitation)

Moving the board's next occurrence of a recurring cron/interval job plans an absolute ISO one-shot
with `repeat=1`, named `<name> (moved <date>)`. It copies the source's prompt, delivery, skills,
script/monitor, tool allowlist (including `[]`), context, workdir and other supported execution fields.
Only trusted cron configuration is copied; the Calendar description is never used as instructions.
The moved event is retagged to the new job; `source_job_id` links it locally. On the **following sync**
the source gets a fresh event previewing its following recurring slot. Completion deletes the copy's
event (not its cron history); deleting that event before it fires pauses just the copy. Completed
copies do not respawn. Rearming/resuming remains an explicit Hermes action.

**Hermes currently has no public skip-occurrence action. The original occurrence WILL still run.**
Creating a copy is therefore an **additional run**, not an exact reschedule. Both the plan and CLI
return this warning. Ordinary `--confirm` **refuses** this case: only after reviewing the dry-run,
use `hermes hermes-jobs sync --confirm --allow-additional-run` to explicitly accept both executions.
The repeating schedule is never rewritten and the source is never paused to fake a skip. The source
calendar event's preview does **not** mean the still-pending original Hermes slot was suppressed.
If duplicate execution is unacceptable, do not opt in; change/cancel the move in Calendar instead.

Installed-source verification: `tools/cronjob_tools.py` defines actions create/list/update/pause/resume/
remove/run/cancel, but no skip. `_HANDLER_FORWARDED_ARGS` is the copy-field contract; the handler also
accepts monitor_script/monitor_url. `cron/jobs.py:parse_schedule` accepts offset-aware ISO timestamps
as `kind=once`. Its `advance_next_run(s)` is ticker bookkeeping calculated from **now**, not a future
slot-specific skip (calling it before a future slot leaves that slot unchanged), so it is not used.
`hermes cron resume --at` only re-arms one-shots; it cannot skip one recurring occurrence.

Pinned inference settings/interpreter overrides that dispatch cannot preserve cause the move to be
refused, not silently dropped. Source `origin` delivery is resolved from its recorded origin, not the
current CLI session; unresolved origin targets are refused. Pending ownership transfers are journaled
and retried without another create. An uncertain create result blocks further creation with a
`move_intent` notice: inspect cron jobs and reconcile the state manually before trying again.
Sync is not a transaction across cron and Google; run a fresh dry-run after any partial error.

Offline mode projects against an empty calendar **and empty sync history**, so it never interprets
unfetched events as deletions. It does not bind a calendar, call Google, or change cron/state.

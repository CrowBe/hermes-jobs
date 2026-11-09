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
hermes hermes-jobs sync --skip-original=experimental            # dry-run incl. the experimental skip steps
hermes hermes-jobs skip-tick             # advance pending experimental skips (run by their wake-up jobs)
```

- cron -> calendar: one 15-min event per job at its next run, tagged `extendedProperties.private.hermes_job_id`;
  title = job name (`[paused] ` prefix when paused); description = job id + prompt sha256 digest only.
- calendar -> cron (tagged events only, via Hermes' `cronjob_manage`): retitle -> name update;
  deleting an event **pauses**, never removes, its job. Untagged events are ignored and event text
  never grants tools or edits prompts.
- Conflicts: a cron edit wins and resets the event. A move never rewrites a recurring schedule.
- Tests: `python -m unittest discover -s tests -v` (planner + mocked runtime; no Google or real cron writes).

### Derived ids (reference stability)

Every scheduled occurrence has a stable **occurrence key**:
`uuid5(3ac1ead0-9909-5599-8d64-da65c43647dc, "<job_id>@<instant as ISO UTC>")`, written as 32 lowercase
hex. The namespace is a fixed literal and must never change. The instant is canonicalised to UTC, so
`09:00+11:00` and `22:00Z` give the same key. Hex is a subset of Google's event-id alphabet (base32hex
`a-v0-9`, 5-1024 chars), so the key doubles as a client-chosen Google event id.

- **Inserts** use the key of the occurrence the event shows as its Google event id. A retried insert
  gets `409`, and that means "already exists, adopt it": the plugin reads the event and patches it to
  the desired body. `status: confirmed` brings back a cancelled event that reused the id. An id owned
  by a *different* job's tag is refused, never taken over.
- **A recurring job's event shows its next occurrence and is patched forward, not recreated.** The id
  is fixed when the event is inserted. Each advance patches `start` and the private
  `hermes_occurrence` tag (the key of the occurrence it now shows) on the same event. That is one
  write per advance, never a delete plus insert, and Google keeps no trail of dead event ids. Insert
  retries stay idempotent through the derived id. Finding "what is tied to slot X" means matching
  `hermes_occurrence`, or the copy's `hermes_original_slot`, not the event id.
- **Every write sends the full private tag set** (`hermes_job_id`, `hermes_occurrence`, and for
  copies `hermes_source_job_id` + `hermes_original_slot`), so a PATCH can never strip the job tag.
- **One-shot copies and skips** are recorded in sync state under `occurrences[<key of the original
  slot>]`. Each record holds the source job, child job, event, mode, status, and the source's
  fingerprint: a digest of schedule + copied execution config, without name, enabled or grace.
- **Rebuilding after a lost state file:** calendar events are found by `hermes_job_id` and adopted,
  never deleted or recreated. Copy links come back from the event tags. A pause caused by deleting
  its event is recognised from Hermes' own `paused_reason` (`hermes-jobs: calendar event deleted`),
  so the deleted event is still not recreated. An armed experimental skip comes back from its pause
  marker (see below). A copy whose create succeeded but whose result was lost is adopted on retry,
  matched by name + one-shot instant; it is never created twice and never gets its own duplicate event.
- **Existing events keep their Google-assigned ids** (the two live probe events). They are adopted
  by tag, and they pick up `hermes_occurrence` on their next patch-forward. Nothing is deleted or
  recreated to migrate them.

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
return this warning. Ordinary `--confirm` **refuses** this case. After reviewing the dry-run, choose
one of two options:
`hermes hermes-jobs sync --confirm --allow-additional-run` explicitly accepts both executions (the
default and recommended choice), or `--skip-original=experimental` (below). Without the experimental
flag, the repeating schedule is never rewritten and the source is never paused. The source calendar
event's preview does **not** mean the still-pending original Hermes slot was suppressed. If duplicate
execution is unacceptable and you do not opt in to the experiment, cancel the move in Calendar instead.

**Series edit while a move is pending:** if the source's schedule, prompt or copied configuration
changes before the copy fires, the next confirmed sync drops the move. It removes the copy job and
its event, undoes any experimental skip, and re-plans the source from cron (cron wins). Renaming the
job is not a series edit.

### EXPERIMENTAL: `--skip-original=experimental` (true skip, off by default)

Hermes has no skip action, so this combines only public `cronjob_manage` actions. Slot `S` is the
original occurrence:

1. **At `--confirm`** (`S` must be at least 5 min away, the schedule must be a cron expression, and
   the job must be enabled with `next_run_at == S`), in this order:
   - Journal the job's original `grace_minutes`/`catch_up` in state.
   - Pause the job with reason `hermes-jobs skip {"k":<key>,"slot":...,"grace":...,"catch_up":...}`.
   - Set `grace_minutes=0` (Hermes floors this at 120 s). Set `catch_up=false` **only** if it was
     explicitly `true`; an absent value is left absent, because an explicit grace already disables
     catch-up, and `cronjob_manage` cannot clear it back to absent.
   - Create two one-shot **`no_agent` script jobs** (no LLM, `deliver: local`):
     `hermes-jobs skip-tick <key8> #1` at `S+10 min` and `#2` at `S+25 min`. Both run the installed
     `~/.hermes/scripts/hermes-jobs-skip-tick.py`, which runs `hermes hermes-jobs skip-tick --quiet`
     and prints nothing on success. These jobs never appear on the board.
   - Create the one-shot copy at the moved time, as in the default mode.

   We pause right away instead of just before `S`, because `S` is the board's *next* occurrence, so
   no other slot falls in that window. The job shows as paused in Hermes until `S+10 min`, but stays
   active on the board.
2. **`S+10 min` (wake-up #1, or any later sync/skip-tick):** resume. Hermes keeps an elapsed slot
   due on resume (#113603). It is past the 120 s grace and catch-up is off, so the next due scan
   **skips `S`, books one `missed ×1` row in the cron execution ledger** ("skipped, catch-up off"),
   and emits a `missed` metric. It sends no chat notification.
3. **`S+25 min` (wake-up #2, or any later sync/skip-tick):** once `next_run_at` is past `S`, restore
   the original `grace_minutes` (cleared if it was absent) and `catch_up`.

Before acting, every step checks that the job is unchanged (same fingerprint), is still paused **by
this marker**, and still has the same slot. Otherwise it aborts with a warning and restores the
grace/catch_up settings:
- A job that was paused manually is left paused.
- A job that was resumed manually is not touched; its original slot may run.
- An edited job is resumed (the pause was ours).
- An interrupted arming is undone.
- A slot that ran anyway is reported (`skip failed`).
The copy is independent of the skip and runs regardless. If sync state is lost while the job is
paused, the skip is rebuilt from the pause marker. If state is lost *after* the resume, nothing marks
the job anymore. The `grace_minutes=0` override (and `catch_up=false`, if it was set) then stays and
has to be restored by hand with `hermes cron edit`.

Limitations: it is experimental, cron expressions only, and depends on the gateway ticker scanning
within ~15 min of `S+10`. The skip shows up in Hermes run history as a **miss**, not as a deliberate
skip. A clean fix needs a Hermes `skip` action (see below).

Installed-source verification: `tools/cronjob_tools.py` defines actions create/list/update/pause/resume/
remove/run/cancel, but no skip. `_HANDLER_FORWARDED_ARGS` is the copy-field contract; the handler also
accepts monitor_script/monitor_url. `cron/jobs.py:parse_schedule` accepts offset-aware ISO timestamps
as `kind=once`. Its `advance_next_run(s)` is ticker bookkeeping calculated from **now**, not a future
slot-specific skip (calling it before a future slot leaves that slot unchanged), so it is not used.
`hermes cron resume --at` only re-arms one-shots; it cannot skip one recurring occurrence.

Pinned inference settings/interpreter overrides that dispatch cannot preserve cause the move to be
refused, not silently dropped. Source `origin` delivery is resolved from its recorded origin, not the
current CLI session; unresolved origin targets are refused. Pending ownership transfers are journaled
and retried without another create. After an uncertain create result, the retry first adopts an
existing copy with the same name and one-shot instant, and only creates one if none is found.
Sync is not a transaction across cron and Google; run a fresh dry-run after any partial error.

Offline mode projects against an empty calendar **and empty sync history**, so it never interprets
unfetched events as deletions. It does not bind a calendar, call Google, or change cron/state.

# Security

hermes-jobs keeps Hermes cron (the system of record) and ONE Google Calendar in sync. This page says
exactly what leaves your machine, what is stored where, and how to switch it off.

## What leaves the machine

Only HTTPS calls to `https://www.googleapis.com/calendar/v3/calendars/<bound calendar>/events...`,
made through Hermes' `credentialed_provider_request("google", ...)` with the OAuth scope
`https://www.googleapis.com/auth/calendar.events` and nothing else. No other host, no telemetry.

- **Reads** (every `sync`, also dry-runs): the bound calendar's events (`singleEvents=false`,
  `showDeleted=true`) and, for each of our recurring series, its instances for the next 60 days.
  Untagged events are read by Google's list call but ignored, except a "this and following" split
  whose id starts with one of our own series ids.
- **Writes** (only `sync --confirm`, a human CLI step; the agent tool can only plan): insert, patch
  or delete events on the bound calendar. `capture-fixtures` performs GETs only and writes sanitised
  JSON locally.
- **Fields sent per event**: title = the cron job **name** (with a `[paused] ` prefix when paused);
  description = the job id, a **sha256 digest (12 hex) of the prompt**, and a fixed note; start/end
  (15 minutes) with time zone; for recurring jobs the RRULE derived from the cron schedule; private
  extended properties: `hermes_job_id`, `hermes_occurrence` (an opaque uuid5 hex), and for one-shot
  copies `hermes_source_job_id` + `hermes_original_slot`. Event ids are derived uuid5 hex.
- **Never sent**: the prompt text, skills, scripts, tool lists, delivery targets, model settings,
  workdir, or anything else from the job's execution config.

## What comes back, and what it can change

Calendar content is untrusted. It can only change a job's **name** (from a title: NFKC-normalised,
control/format/bidi/zero-width characters removed, single line, at most 120 characters) and its
**schedule** (a validated timezone-aware ISO instant for one-shots and moved copies, or a canonical
cron built from integers when an RRULE converts exactly). Descriptions are compared, never parsed.
The dispatch gate (`_safe_updates`) drops every other field before `cronjob_manage` is called, so a
calendar edit can never set a prompt, tools, a script, delivery, or reach a shell. Deleting events
only *pauses* jobs. Untagged events never create jobs.

## Credentials

- The plugin never holds, reads, writes, refreshes or logs a token. `hermes auth add hermes-jobs:google`
  runs Hermes' PKCE sign-in; Hermes keeps the grant in the active profile's `auth.json` under
  `plugin_oauth` (key `hermes-jobs:google`, file mode 0600) and attaches the bearer only to
  `www.googleapis.com` requests.
- The OAuth client id/secret are read by Hermes from `HERMES_JOBS_GOOGLE_CLIENT_ID` /
  `HERMES_JOBS_GOOGLE_CLIENT_SECRET`, not by the plugin.
- Error output carries only Google's error message, capped and passed through a redactor that masks
  bearer tokens and Google token shapes. Tests enforce that the plugin source never names a credential
  store and that no request sets headers.

## Local state and files

- Sync state: `<HERMES_HOME>/plugin-data/hermes-jobs/sync-state.json` (via Hermes `PluginState`):
  calendar id, event ids, job hashes, event titles/start times, occurrence records. No prompts, no tokens.
- Reads (read-only): `<HERMES_HOME>/cron/jobs.json` and Hermes config (time zone).
- Cron changes go only through Hermes' own `cronjob_manage` tool.
- Experimental skip only (`--skip-original=experimental`, off by default): writes the fixed script
  `<HERMES_HOME>/scripts/hermes-jobs-skip-tick.py` and creates two one-shot `no_agent` cron jobs that
  run `hermes hermes-jobs skip-tick --quiet` (fixed argument list, no shell, no input from Calendar).

## Revoking access

1. `hermes auth logout hermes-jobs:google` (deletes the grant from Hermes).
2. Remove the app's access at <https://myaccount.google.com/permissions>.
3. Optionally `hermes plugins remove hermes-jobs` (also drops its grants) and delete the state file.

Events already on the calendar stay until you delete them; deleting them while the plugin is
removed changes nothing in cron.

## Reporting a vulnerability

Please report privately via GitHub's "Report a vulnerability" (Security advisories) on
<https://github.com/CrowBe/hermes-jobs>, or open an issue asking for a private contact if that is
unavailable. Do not include tokens or calendar contents in public issues. Expect a first response
within 7 days.

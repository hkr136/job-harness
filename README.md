# Job Agent

Local, human-supervised CLI for finding, ranking and drafting responses to vacancies.

## Install

```bash
cd job-agent
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
playwright install chromium
job-agent init
```

For an environment that requires `requirements.txt`, use `pip install -r requirements-dev.txt` (or `requirements.txt` for runtime dependencies only), then run `pip install -e .`.

Without an LLM the deterministic, profile-aware analyzer still works. LLM selections live in the user-owned `config.yaml`; API keys stay in `~/.job-harness/.env`. Built in providers are signed-in Codex CLI, OpenAI-compatible APIs (including OpenRouter), and Ollama. They all implement the same provider contract, so switching a role does not change scanning, drafting or recovery behavior.

```bash
job-agent providers list
job-agent providers test codex
job-agent providers models openai  # only fetches a catalogue when you ask
job-agent providers select analysis codex
job-agent providers select writing ollama --model qwen2.5:7b
```

Set `llm.roles.analysis`, `writing` and `recovery` to an enabled provider. A role's `automated: true` flag is provider-neutral: it controls whether the scheduler may use it. Codex uses the CLI's existing ChatGPT sign-in; the project never reads, copies or stores ChatGPT session tokens. The model may be left blank to use Codex's account-managed default. API usage accounting is saved with each analysis; scans fall back to deterministic matching once `limits.llm_budget_per_day_usd` is reached.

## User-data boundary

The repository is generic. Personal data lives outside it in `$JOB_HARNESS_HOME` (default: `~/.job-harness`): profile, configuration, SQLite database, browser sessions, screenshots and logs. Run `job-agent init` to create neutral templates, then edit `~/.job-harness/profile.yaml` and `~/.job-harness/config.yaml`.

For an older installation, `job-agent migrate-user-data` copies legacy local data only when its destination does not already exist; it never overwrites or deletes the source.

## Safe first use

```bash
job-agent doctor
job-agent status
job-agent site login hh
job-agent site login geekjob
job-agent site login habr
job-agent site login kwork
job-agent scan --site hh
job-agent jobs list --min-score 60
job-agent jobs show 1
job-agent apply 1 --draft
pytest
```

All default commands are read-only or draft-only. A site-side submission always
requires `--auto --confirm` plus that site's explicit success state. Kwork also
requires price, duration and visible rich-text checks; Habr Career and GeekJob
use their native response forms and refuse to duplicate an existing response.

`job-agent status` reports the local funnel and each configured site's session state: `logged in`, `login required` or an actionable browser/site error.

The local database now also has commands for viewing application records, collected messages and automation runs:

```bash
job-agent applications list
job-agent applications show APPLICATION_ID
job-agent applications record-confirmed SITE VACANCY_ID "final text" --confirm
job-agent applications import-statuses --site habr
job-agent messages list --unread
job-agent runs list
job-agent queue list
```

When a required application-form fact is absent from the user profile, the
vacancy is placed in `needs_clarification`; no partial form is submitted. Use
`job-agent clarifications list`, `clarifications show JOB_ID`, then
`clarifications answer REQUEST_ID "answer" --scope profile|vacancy` and
`clarifications resolve JOB_ID`. Profile-scoped answers are written only to the
user-owned `profile.yaml`; vacancy-scoped answers are never reused.

The durable queue supports safe scan and message-check tasks. `job-agent queue scan hh` adds an idempotent scan and `job-agent queue run-once` executes one due task with authentication checks and retry backoff. Use `job-agent messages check --site kwork` to read only explicitly unread Kwork inbox rows; it never opens a thread or sends a reply.

Message collection, status synchronisation and external submission remain adapter-specific. An adapter cannot claim a capability until its browser flow and confirmation state have been implemented and tested.

## Candidate profile and safeguards

The user-owned profile records core skills, additional experience, learning technologies and skills that must not be claimed. Matching treats those categories differently and never upgrades a learning-only item into a production claim.

If the profile does not declare `candidate.experience_years`, vacancies with an explicit minimum tenure requirement are conservatively marked for review and cannot be auto-submitted.

Pre-authorized answers belong in the same user profile. For example,
`candidate.compensation.monthly_target` and `currency` let a future form adapter
answer a salary-expectation question; absent values remain a manual stop rather
than an invented number.

## Browser profiles

`site login hh` opens Playwright's isolated `$JOB_HARNESS_HOME/browser-profiles/hh` profile for manual login. It never reuses or modifies the primary Chrome profile. Scans reuse that exact profile, run headlessly by default and first stop with a login instruction if the session is unavailable. The current adapter needs a local Playwright Chromium install; `doctor` checks the application setup but does not require authentication.

To avoid bursty browsing, HH actions are serial and pause for a random 2.5–5 seconds after each page navigation. Set `JOB_AGENT_BROWSER_MIN_ACTION_DELAY_SECONDS` and `JOB_AGENT_BROWSER_MAX_ACTION_DELAY_SECONDS` in `.env` only if a slower pace is needed.

Browser navigation has a 30-second upper bound. A failed page transition saves a screenshot under `$JOB_HARNESS_HOME/logs/browser-errors/`; any failed adapter context also saves a Playwright trace under `$JOB_HARNESS_HOME/artifacts/playwright-traces/` for recovery analysis.

When a stable adapter hits a recoverable selector/editor failure, the workflow first tries the saved runtime selector and records diagnostics. If that fails, the selected `recovery` role may propose an adapter-only diff; it is accepted only after an allowlist check, backup, target tests and rollback protection. A site confirmation is still required for any submission.

```bash
job-agent site recover hh --reason "Search card selector returned zero results"
```

## Review and reporting

```bash
job-agent jobs reanalyze JOB_ID
job-agent messages show MESSAGE_ID
job-agent stats funnel
job-agent stats export --format csv
job-agent tui
```

`tui` is the keyboard-first amethyst operator station over the same SQLite state
as the CLI. Press `Tab` to open its navigation menu and `/` for the command
line. It supports local
draft editing (`edit APPLICATION_ID` or `edit-reply MESSAGE_ID`, then
`Ctrl+Enter`), clarification resolution, queueing scans, and schedule/model
configuration.

The TUI starts in **SAFE** mode every time. In SAFE, `send JOB_ID` and
`reply-send MESSAGE_ID` only create or retain local drafts. A site-side action
is possible only during the current TUI session after entering the exact command
`arm I AUTHORIZE SENDING`; each adapter must still return an explicit site-side
delivery confirmation before the local record is marked sent. Kwork replies are
always restricted to its internal chat—never external contacts.

For unattended local scheduling, run `job-agent scheduler serve`; it remains a
single local process and can be installed as a macOS LaunchAgent as described in
[launchd.md](docs/launchd.md).

To create and load that service deliberately, first enable at least one
scheduler task and then run:

```bash
job-agent scheduler install-launchd --confirm
```

The command writes only the current user's LaunchAgent and local log paths. It
does not enable any application or message sending.

For the Kwork submission path, a typical intentional command is:

```bash
job-agent apply JOB_ID --auto --price 10000 --duration 3 --title "Scoped work title"
# review the dry-run result; only then repeat with --confirm if it is appropriate
```

The system will refuse if the score policy, daily limit, required fields, on-platform-contact rule or final Kwork confirmation is missing.

Incoming employer messages use the same separation between local intent and
remote delivery. `job-agent messages reply MESSAGE_ID` saves a profile-backed
draft only when every needed fact is known; tests, meeting commitments and
unknown facts become local review items. `job-agent messages send MESSAGE_ID
--confirm` is available only for adapters that can prove a message was posted
inside the platform's own chat. A local draft is never counted as a sent reply.

## Adapter coverage

Kwork has guarded offer submission and unread-message collection. Habr Career
has native response submission and read-only response-status import. GeekJob
has native response submission and response-status import. hh.ru has vacancy
discovery, response-status import, and guarded form submission: mandatory
questions are read first, unknown facts become clarification requests, and a
personalized cover letter must persist in HH's visible editor before submission.
HH also reads only chats with an explicit unread badge and can send a prepared
reply only through the native HH chat after explicit confirmation and visible
delivery confirmation. All Kwork communication stays inside Kwork and never
includes external contacts.

## Adding a site

Known sites run as stable adapters with fixed selectors. A custom adapter can be
kept in a separately installed Python package and selected in the user-owned
configuration as `package.module:AdapterClass`; it must subclass
`BaseSiteAdapter`. Use discovery to inspect a new site's UI, implement and
manually smoke-test that adapter, then keep the LLM out of normal production
runs. If a stable adapter later breaks, `job-agent site recover SITE` writes a
reviewable recovery proposal from saved diagnostics; the proposed change must
still be applied and verified with `job-agent site test SITE`.

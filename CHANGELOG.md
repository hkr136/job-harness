# Changelog

## Unreleased

- Added a plugin-capable, role-based LLM provider layer: signed-in Codex CLI, OpenAI-compatible APIs, OpenRouter and Ollama now share the same analysis, writing and recovery contract.
- Added provider catalogue, connection-test and non-destructive setup CLI commands; provider API keys remain in the user-owned dotenv file.

- Separated generic source code from user-owned profile, state, browser profiles and artifacts via `JOB_HARNESS_HOME`.
- Added neutral initialization and non-destructive user-data migration commands.
- Added durable, idempotent harness task queue and CLI inspection command.
- Added a single-task worker with run audit records, session checks and retry backoff for queued scans.
- Added in-process APScheduler integration and the native primary-buffer terminal workbench.
- Added and live-verified the Habr Career read-only adapter for suitable vacancies.
- Added and live-verified the Kwork read-only project-feed adapter.
- Added and live-verified GeekJob vacancy discovery with commit-based navigation recovery.
- Added persistent, headless-by-default browser profiles with serial randomized navigation delays.
- Added explicit session state to `job-agent status` and stopped scans before unauthenticated access.
- Added database support and read-only CLI views for application-status history, messages and automation runs.
- Added conservative Kwork unread-message collection: only an explicit unread UI marker can create a new-message alert.
- Fixed profile normalization for both list and grouped project histories, restoring durable per-listing analysis.
- Added Playwright trace capture on adapter failures, adapter lifecycle documentation and a read-only `site test` smoke command.
- Added single-listing reanalysis, message inspection and CSV/JSON funnel export.
- Added conservative incoming-message classification and manual-action guidance for tests, invitations and external-contact requests.
- Added guarded Kwork offer submission: required visible fields, character bounds, on-platform contact policy, explicit CLI confirmation and exact site confirmation before persistence.
- Added a generic confirmed-status sync service and durable status-check queue task; corrected application rate limiting to use the current day only.
- Added configurable LLM token/cost accounting and a daily cost budget fallback to deterministic analysis.
- Added conservative handling for explicit years-of-experience requirements when the user profile does not confirm them.
- Added local scheduler service mode, launchd deployment instructions, batch detail collection and profile/config/site CLI inspection commands.
- Added user-profile-backed answers for compensation questions; empty expectations safely require manual review.
- Added native confirmed submission adapters for Habr Career and GeekJob, including duplicate guards and persisted screenshots.
- Added read-only response/status import for Habr Career and hh.ru negotiations.
- Added `applications record-confirmed` for externally confirmed responses on adapters without a status endpoint; it never contacts a site.
- Added profile-backed salary-range and city answers while keeping undeclared personal facts as manual stops.
- Added a durable `needs_clarification` queue for unknown required form facts, with per-vacancy/profile answer scope, CLI/TUI review, submission blocking and additive SQLite migration.
- Added HH form handling for visible mandatory questions and the native cover-letter editor; it refuses blank or unpersisted cover letters before any response attempt.
- Added GeekJob read-only response-status import and synchronized the derived vacancy funnel state with confirmed applications, including legacy records.
- Added weekday/working-hour gates to background polling while preserving deliberate manual checks outside the window.
- Added durable local message-reply drafts and delivery states, with deterministic profile-backed answers for declared compensation, location, work preferences and Kwork's on-platform-contact rule.
- Added HH unread-chat collection plus guarded native HH chat delivery; only an explicit unread badge enters the local inbox and delivery requires the posted message to appear in the conversation.
- Made recurring scheduler tasks reusable after completion and recoverable after a stale running browser task, so a single cycle cannot permanently stop future polling.

## 0.1.0 — 2026-07-28

- Created isolated Job Agent MVP.
- Added candidate profile sourced from the provided document.
- Added SQLite persistence, deterministic matching, draft generation, CLI and hh.ru read adapter.
- Added dry-run-first safety boundary, documentation and unit tests.

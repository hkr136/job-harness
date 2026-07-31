# Existing browser workflows audit

Source: verified work in the current Codex task, 21–28 July 2026. This is an implementation audit, not a credential store.

| Site | Verified read workflow | Verified action | Current disposition |
|---|---|---|---|
| hh.ru | resume session, remote vacancy search and vacancy pages | — | Read/search adapter. Submission and status adapters remain unavailable. |
| GeekJob | account dashboard and public vacancy list | — | Read/search adapter. Submission, messages and statuses remain unavailable. |
| Habr Career | suitable vacancy list and vacancy pages | — | Read/search adapter. Submission, messages and statuses remain unavailable. |
| Kwork | seller feed, project pages and inbox | Visible rich-text offer fields and exact success confirmation | Read/search, conservative unread-message reader and guarded offer flow. Never use hidden fields or external contacts. |

## Reusable reliability rules

- Use persistent but isolated Playwright profiles; never copy the primary Chrome profile.
- Prefer roles, stable URLs and visible text. Capture a trace and screenshot on scenario failure.
- Deduplicate by site/external ID before preparation; only mark submitted after the site's explicit confirmation.
- Development and tests use dry-run only. The production Kwork command still requires an intentional `--confirm` and exact site success state before it persists a submission.

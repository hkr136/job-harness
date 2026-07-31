# CLI

- `job-agent doctor`, `status`
- `job-agent site login hh`
- `job-agent scan --site hh`
- `job-agent jobs list|show|open`
- `job-agent apply JOB_ID --draft`

`apply` currently persists a draft only. `--auto` reports that submission is blocked, even when dry-run is disabled.
# CLI specification

Implemented commands are deliberately scoped to local review and durable queue processing:

- `job-agent doctor` — verify local configuration and database access.
- `job-agent config show`, `profile show|edit` — review and edit only user-owned data.
- `job-agent site list` — show configured adapters and isolated browser profiles.
- `job-agent site login hh` — open the isolated HH profile for manual login.
- `job-agent scan --site hh` — search and analyse; no side effects on a site.
- `job-agent jobs list|show|open` — review the local shortlist.
- `job-agent apply JOB_ID --draft` — save and print a draft; never submit it.
- `job-agent status` — print local funnel counters.
- `job-agent jobs reanalyze JOB_ID` — refresh and re-score one listing.
- `job-agent apply JOB_ID --interactive` — locally refine a draft with explicit user text before saving it.
- `job-agent messages check|list|show|reply` — inspect supported adapter inboxes and surface safe manual next actions; `reply` never sends.
- `job-agent queue scan|list|run-once` — durable, idempotent scan/message work.
- `job-agent scheduler status|run-now` — inspect configured intervals or enqueue an immediate safe pass.
- `job-agent scheduler serve` — keep the in-process scheduler alive for local service use.
- `job-agent applications check-statuses` — sync only externally identified and adapter-confirmed status changes.
- `job-agent runs list|show` and `job-agent logs --errors` — inspect durable run records and safe diagnostic artifacts.
- `job-agent stats funnel|export` — compute and export local funnel values.
- `job-agent stats skills` — show recurring matched and missing requirements from saved analyses.
- `job-agent tui` — read-only terminal dashboard.

Employer-message replies and status mutation remain deferred until their individual site flows have explicit confirmation checks and user authorization. Kwork offers have a guarded confirmation flow; other sites remain read-only.

# Database schema

- `jobs`: normalized discovered vacancies, identity and raw normalized text.
- `analyses`: one structured analysis per job, including score and model accounting.
- `applications`: drafts and later confirmed submissions.
- `runs`: audit trail for scans and maintenance jobs.

The schema is created idempotently by SQLAlchemy. Status-history and message tables are the next adapter milestone; no external side effect is modeled as successful without a site confirmation.

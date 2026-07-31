# Site adapter interface

Every adapter implements `check_auth`, `search_jobs` and `get_job_details`. Optional preparation/submission methods are deliberately safe by default: base submission always returns an unconfirmed failure. This keeps an incomplete adapter from accidentally sending an application.

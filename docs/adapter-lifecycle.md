# Adapter lifecycle

Each website adapter evolves through three deliberately separate modes.

1. **Stable adapter** is the production path. It uses fixed Playwright locators,
   bounded retries and state checks. Normal scans do not need an LLM.
2. **Discovery mode** is a developer workflow for a new site: inspect the page
   with Playwright, identify semantic/stable selectors, implement an adapter,
   then run `job-agent site test SITE`. It must remain read-only until manually
   verified.
3. **Recovery mode** starts after a stable adapter fails. The browser manager
   writes a screenshot and Playwright trace into the user-data directory. An LLM
   may inspect a sanitized page/trace and propose a selector change, but cannot
   make an irreversible site action. The changed adapter needs a repeatable test
   before it becomes stable again.

Capabilities are an explicit contract. An adapter may advertise submission,
messages or status synchronization only after its deterministic flow has an
explicit confirmation check. This prevents the generic harness from treating a
partially implemented workflow as safe to run.

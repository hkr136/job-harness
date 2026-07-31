# Architecture

`job_agent` is a layered local application: CLI/TUI → services → adapters/analysis → SQLite. Site adapters never call the LLM; the analyzer only receives normalized vacancy text and the runtime user profile. Browser profiles live in `$JOB_HARNESS_HOME/browser-profiles/<site>`.

The adapters demonstrate the plug-in boundary. Add a site by implementing `BaseSiteAdapter`, registering it in `sites.registry`, configuring it in the user-owned `config.yaml`, and adding a mocked smoke test. Stable adapters use fixed Playwright locators; discovery and recovery may inspect traces, but no LLM is required in the normal scan path.

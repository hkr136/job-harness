# Safety model

The project does not store passwords, cookies or API keys in source control. `.env`, SQLite state, browser profiles, traces and screenshots are ignored. Browser automation never solves CAPTCHAs. The only implemented irreversible flow is the guarded Kwork offer command, which needs explicit CLI confirmation, validates visible fields and requires the exact site confirmation before local state changes. The candidate profile distinguishes core, additional, learning and not-claimed skills; only core skills are treated as full evidence.

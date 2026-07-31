# Run as a local macOS service

The scheduler is intentionally local: it uses the isolated browser profiles and
user-owned SQLite database of the account that starts it. It does not require
Codex after installation.

Create `~/Library/LaunchAgents/local.job-agent.plist` with paths adapted to the
local checkout and Python virtual environment:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>local.job-agent</string>
  <key>ProgramArguments</key><array>
    <string>/absolute/path/to/job-agent/.venv/bin/job-agent</string>
    <string>scheduler</string><string>serve</string>
  </array>
  <key>WorkingDirectory</key><string>/absolute/path/to/job-agent</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/Users/USER/.job-harness/logs/service.out.log</string>
  <key>StandardErrorPath</key><string>/Users/USER/.job-harness/logs/service.err.log</string>
</dict></plist>
```

Set `scheduler.enabled: true` in the user-owned `config.yaml` before loading the
service. Load it with `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/local.job-agent.plist`.
Stop it with `launchctl bootout gui/$(id -u)/local.job-agent`. Start only after
logging in to each desired site through `job-agent site login SITE`.

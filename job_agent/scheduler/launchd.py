from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from pathlib import Path

LAUNCH_AGENT_LABEL = "local.job-agent"


def launch_agent_path(home: Path | None = None) -> Path:
    base = home or Path.home()
    return base / "Library" / "LaunchAgents" / f"{LAUNCH_AGENT_LABEL}.plist"


def launch_agent_payload(executable: Path, workdir: Path, user_home: Path) -> dict[str, object]:
    """Return a generic, user-local LaunchAgent definition.

    Browser sessions and all personal data remain under ``user_home``.  The
    plist contains no candidate profile fields, credentials, or tokens.
    """
    logs = user_home / "logs"
    return {
        "Label": LAUNCH_AGENT_LABEL,
        "ProgramArguments": [str(executable), "scheduler", "serve"],
        "WorkingDirectory": str(workdir),
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": str(logs / "service.out.log"),
        "StandardErrorPath": str(logs / "service.err.log"),
    }


def write_launch_agent(executable: Path, workdir: Path, user_home: Path, destination: Path | None = None) -> Path:
    """Create/update the local plist without loading it into launchd."""
    path = destination or launch_agent_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    (user_home / "logs").mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        plistlib.dump(launch_agent_payload(executable, workdir, user_home), handle, sort_keys=False)
    return path


def bootstrap_launch_agent(path: Path) -> None:
    """Register a previously written plist for the current GUI user."""
    primary = ["launchctl", "bootstrap", f"gui/{os.getuid()}", str(path)]
    result = subprocess.run(
        primary,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        return
    # Some macOS GUI sessions reject `bootstrap` after a service reload even
    # though the plist is valid. The legacy load command is a documented local
    # fallback and preserves the same per-user scope.
    subprocess.run(["launchctl", "load", "-w", str(path)], check=True, capture_output=True, text=True)


def launch_agent_status() -> tuple[bool | None, str]:
    """Return the real per-user service state without starting a process.

    ``None`` means this platform has no launchd status surface.  This keeps the
    CLI honest instead of saying that an in-process scheduler is stopped while
    the installed LaunchAgent is actively polling in the background.
    """
    if sys.platform != "darwin":
        return None, "launchd status is available on macOS only"
    command = ["launchctl", "print", f"gui/{os.getuid()}/{LAUNCH_AGENT_LABEL}"]
    try:
        result = subprocess.run(command, check=False, capture_output=True, text=True, timeout=5)
    except (FileNotFoundError, subprocess.TimeoutExpired) as error:
        return False, f"launchctl unavailable: {error}"
    if result.returncode != 0:
        return False, "not loaded"
    state = next((line.strip() for line in result.stdout.splitlines() if "state =" in line), "state unknown")
    pid = next((line.strip() for line in result.stdout.splitlines() if "pid =" in line), "")
    return True, " · ".join(item for item in (state, pid) if item)

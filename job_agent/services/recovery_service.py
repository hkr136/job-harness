from __future__ import annotations

import inspect
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from job_agent.config.settings import USER_HOME
from job_agent.llm.providers import LLMProvider


def is_recoverable_adapter_error(detail: str) -> bool:
    """Limit autonomous repair to UI/selector failures, never business rejects."""
    value = detail.casefold()
    markers = (
        "locator", "timeout", "not uniquely", "did not persist", "rich-text",
        "form did not open", "composer", "button", "editor", "selector",
    )
    return any(marker in value for marker in markers)


def adapter_test_target(site: str) -> str:
    return {
        "kwork": "tests/test_kwork_offer.py",
        "habr": "tests/test_habr_statuses.py",
    }.get(site, "tests")


async def propose_adapter_recovery(provider: LLMProvider, adapter: object, site: str, evidence: str) -> Path:
    """Save a reviewable selector-recovery proposal; never apply it to source code."""
    source = inspect.getsource(type(adapter))
    system = (
        "You repair a Playwright site adapter. Propose the smallest robust code change based only on "
        "the supplied adapter source and failure evidence. Do not claim to have inspected files, traces "
        "or live pages. Return a unified diff followed by a concise verification checklist. "
        "Never suggest external site actions or weakened confirmation checks."
    )
    user = f"Site: {site}\nFailure evidence: {evidence}\n\nCurrent adapter source:\n```python\n{source}\n```"
    proposal = await provider.complete(system, user)
    directory = USER_HOME / "artifacts" / "recovery"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{site}-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.md"
    path.write_text(f"# Recovery proposal: {site}\n\n{proposal}\n", encoding="utf-8")
    return path


async def apply_verified_adapter_recovery(
    provider: LLMProvider, adapter: object, site: str, evidence: str, *, test_target: str,
) -> tuple[bool, Path, str]:
    """Propose, narrowly apply, test and roll back an adapter-only patch.

    This is deliberately a closed loop for selector drift, never a way for an
    LLM to broaden browser permissions.  It only touches the inspected adapter
    source, keeps a backup and restores it on any failed test.  A caller may
    retry the *failed form stage* after a successful return; this function does
    not submit an application itself.
    """
    proposal_path = await propose_adapter_recovery(provider, adapter, site, evidence)
    proposal = proposal_path.read_text(encoding="utf-8")
    if "diff --git" not in proposal and "--- " not in proposal:
        return False, proposal_path, "Recovery model did not return a unified diff."
    source_path = Path(inspect.getfile(type(adapter))).resolve()
    root = Path(__file__).resolve().parents[2]
    allowed = (root / "job_agent" / "sites" / site / "adapter.py").resolve()
    if source_path != allowed:
        return False, proposal_path, "Recovery target is outside the approved site adapter."
    diff_start = proposal.find("diff --git")
    if diff_start < 0:
        diff_start = proposal.find("--- ")
    diff = proposal[diff_start:]
    relative_target = str(allowed.relative_to(root))
    file_headers = [line[4:].strip() for line in diff.splitlines() if line.startswith(("+++ ", "--- "))]
    if any(path not in {f"a/{relative_target}", f"b/{relative_target}", relative_target, "/dev/null"} for path in file_headers):
        return False, proposal_path, "Recovery diff tries to modify a file outside the approved adapter."
    backup = proposal_path.with_suffix(".adapter.backup.py")
    manifest = proposal_path.with_suffix(".rollback.json")
    shutil.copy2(source_path, backup)
    manifest.write_text(
        '{"target": %r, "backup": %r, "state": "pending"}' % (str(source_path), str(backup)), encoding="utf-8"
    )
    try:
        result = subprocess.run(["patch", "-p1", "--forward"], cwd=root, input=diff, text=True, capture_output=True, timeout=30, check=False)
        if result.returncode != 0:
            return False, proposal_path, f"Patch rejected: {(result.stderr or result.stdout)[:300]}"
        tests = subprocess.run([sys.executable, "-m", "pytest", "-q", test_target], cwd=root, text=True, capture_output=True, timeout=120, check=False)
        if tests.returncode != 0:
            shutil.copy2(backup, source_path)
            manifest.write_text(manifest.read_text(encoding="utf-8").replace('"pending"', '"rolled_back"'), encoding="utf-8")
            return False, proposal_path, f"Patch tests failed and were rolled back: {(tests.stdout + tests.stderr)[-500:]}"
        manifest.write_text(manifest.read_text(encoding="utf-8").replace('"pending"', '"active"'), encoding="utf-8")
        return True, proposal_path, "Patch passed adapter tests; recovery is active."
    except Exception as error:
        shutil.copy2(backup, source_path)
        manifest.write_text(manifest.read_text(encoding="utf-8").replace('"pending"', '"rolled_back"'), encoding="utf-8")
        return False, proposal_path, f"Recovery failed and was rolled back: {type(error).__name__}: {error}"

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

import yaml

from job_agent.config.settings import USER_HOME, compensation_answer


def classify_question(question: str) -> str:
    """Map form wording to a stable user-owned answer key."""
    normalized = question.lower()
    if any(word in normalized for word in ("зарплат", "доход", "оплат", "вилк")):
        return "compensation"
    if any(word in normalized for word in ("город", "место проживания", "локац")):
        return "location"
    if any(word in normalized for word in ("воинск", "военн", "приписн", "билет")):
        return "military_status"
    if any(word in normalized for word in ("юридическ", "разрешени", "гражданств", "налогов")):
        return "legal_status"
    if any(word in normalized for word in ("опыт", "стаж", "лет работы")):
        return "experience"
    if any(word in normalized for word in ("тестов", "test task")):
        return "test_task"
    return "other"


def profile_answer_key(question: str, kind: str | None = None) -> str:
    """Avoid reusing an arbitrary answer for unrelated free-form questions."""
    resolved_kind = kind or classify_question(question)
    if resolved_kind != "other":
        return resolved_kind
    normalized = re.sub(r"\s+", " ", question.lower()).strip()
    return f"other:{hashlib.sha256(normalized.encode()).hexdigest()[:16]}"


def answer_known_application_question(question: str, profile: dict) -> str | None:
    """Answer only pre-authorized facts from the user profile, never infer them."""
    normalized = question.lower()
    key = profile_answer_key(question)
    saved = profile.get("candidate", {}).get("form_answers", {})
    if isinstance(saved, dict) and isinstance(saved.get(key), str) and saved[key].strip():
        return saved[key].strip()
    if any(word in normalized for word in ("зарплат", "доход", "оплат", "вилк")):
        return compensation_answer(profile)
    if any(word in normalized for word in ("город", "место проживания", "локац")):
        location = profile.get("candidate", {}).get("location", {})
        city = location.get("city") if isinstance(location, dict) else None
        return str(city) if city else None
    return None


def save_profile_answer(kind: str, answer: str, question: str = "", profile_path: Path | None = None) -> None:
    """Persist an explicitly approved reusable answer in user-owned profile data only."""
    path = profile_path or USER_HOME / "profile.yaml"
    profile: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    candidate = profile.setdefault("candidate", {})
    answers = candidate.setdefault("form_answers", {})
    answers[profile_answer_key(question, kind)] = answer.strip()
    path.write_text(yaml.safe_dump(profile, allow_unicode=True, sort_keys=False), encoding="utf-8")

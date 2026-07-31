import asyncio

import yaml

from job_agent.database.repositories import Store
from job_agent.models import (
    AnalysisResult,
    ClarificationInput,
    PreparedApplication,
    RawJobDetails,
    SubmissionResult,
)
from job_agent.services.application_service import ApplicationService
from job_agent.services.form_answers import answer_known_application_question, save_profile_answer
from job_agent.sites.hh.adapter import HHAdapter


def make_job(store: Store) -> int:
    job, _ = store.upsert_job(RawJobDetails(external_job_id="vacancy-1", site="sample", url="https://example.test/1", title="Role"))
    return job.id


def test_multiple_required_questions_block_then_resolve_job(tmp_path) -> None:
    store = Store(f"sqlite:///{tmp_path / 'state.sqlite3'}")
    job_id = make_job(store)
    requests = store.create_clarifications(job_id, "sample", [
        ClarificationInput(question="Укажите воинский учет", kind="military_status", field_name="military"),
        ClarificationInput(question="Укажите юридический статус", kind="legal_status", field_name="legal"),
    ])
    assert len(requests) == 2
    assert store.get_job(job_id)[0].status == "needs_clarification"
    analysis = AnalysisResult(summary="", match_score=80, confidence=1, recommendation="review", reasoning="")
    store.save_analysis(job_id, analysis)
    store.save_draft(job_id, "sample", "Draft can be prepared without changing the blocked state")
    assert store.get_job(job_id)[0].status == "needs_clarification"
    assert store.has_open_required_clarifications(job_id)
    assert not store.resolve_clarifications(job_id)[0]

    store.answer_clarification(requests[0].id, "Уточнено", "vacancy")
    assert not store.resolve_clarifications(job_id)[0]
    store.answer_clarification(requests[1].id, "Уточнено", "vacancy")
    assert store.resolve_clarifications(job_id)[0]
    assert store.get_job(job_id)[0].status == "ready_to_apply"
    assert not store.has_open_required_clarifications(job_id)


def test_profile_scope_is_reusable_but_vacancy_scope_is_not(tmp_path) -> None:
    profile_path = tmp_path / "profile.yaml"
    profile_path.write_text(yaml.safe_dump({"candidate": {}}, allow_unicode=True), encoding="utf-8")
    save_profile_answer("military_status", "Воинский учет: вариант подтвержден пользователем", "Укажите сведения о воинском учете", profile_path)
    profile = yaml.safe_load(profile_path.read_text(encoding="utf-8"))
    assert answer_known_application_question("Укажите сведения о воинском учете", profile) == "Воинский учет: вариант подтвержден пользователем"

    store = Store(f"sqlite:///{tmp_path / 'state.sqlite3'}")
    job_id = make_job(store)
    request = store.create_clarifications(job_id, "sample", [ClarificationInput(question="Нужен ответ", kind="other")])[0]
    store.answer_clarification(request.id, "Only this vacancy", "vacancy")
    assert answer_known_application_question("Нужен ответ", profile) is None


class SubmitAdapter:
    class capabilities:
        submit_application = True

    async def submit_application(self, *_args, **_kwargs) -> SubmissionResult:
        raise AssertionError("Adapter must not be called while clarification is open")


def test_open_clarification_blocks_submit_before_adapter_call(tmp_path) -> None:
    store = Store(f"sqlite:///{tmp_path / 'state.sqlite3'}")
    job_id = make_job(store)
    store.create_clarifications(job_id, "sample", [ClarificationInput(question="Required", kind="other")])
    analysis = AnalysisResult(summary="", match_score=100, confidence=1, recommendation="apply", reasoning="")
    result = asyncio.run(ApplicationService(store, SubmitAdapter(), 15, 88).submit(
        PreparedApplication(job_id=job_id, site="sample", body="Response"), analysis, dry_run=False
    ))
    assert not result.confirmed
    assert "clarifications" in result.detail


class ClarifyingAdapter:
    class capabilities:
        submit_application = True

    async def submit_application(self, _prepared, **_kwargs) -> SubmissionResult:
        return SubmissionResult(
            success=False,
            confirmed=False,
            detail="Need facts",
            clarifications=[ClarificationInput(question="Required fact", kind="legal_status", field_name="legal")],
        )


def test_adapter_clarification_result_is_persisted_and_never_counted_as_submit(tmp_path) -> None:
    store = Store(f"sqlite:///{tmp_path / 'state.sqlite3'}")
    job_id = make_job(store)
    analysis = AnalysisResult(summary="", match_score=100, confidence=1, recommendation="apply", reasoning="")
    result = asyncio.run(ApplicationService(store, ClarifyingAdapter(), 15, 88).submit(
        PreparedApplication(job_id=job_id, site="sample", body="Response"), analysis, dry_run=False
    ))
    assert not result.confirmed
    assert store.get_job(job_id)[0].status == "needs_clarification"
    assert len(store.list_clarifications(job_id)) == 1
    assert store.submitted_today() == 0


def test_hh_unknown_required_fields_are_queued_only_when_profile_has_no_answer() -> None:
    fields = [
        {"question": "Укажите воинский учет", "field_name": "military", "required": "true"},
        {"question": "Укажите город", "field_name": "city", "required": "true"},
    ]
    profile = {"candidate": {"location": {"city": "Moscow"}}}
    requests = HHAdapter.required_form_clarifications(fields, profile)
    assert len(requests) == 1
    assert requests[0].kind == "military_status"

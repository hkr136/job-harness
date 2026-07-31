from job_agent.models import PreparedApplication
from job_agent.sites.kwork.adapter import KworkAdapter


def test_kwork_offer_requires_complete_platform_safe_fields() -> None:
    prepared = PreparedApplication(
        job_id=1,
        external_job_id="123",
        site="kwork",
        title="A scoped offer",
        body="x" * 150,
        price="10000",
        duration="3",
    )
    assert KworkAdapter.validate_offer(prepared) is None


def test_kwork_offer_blocks_external_contacts() -> None:
    prepared = PreparedApplication(
        job_id=1,
        external_job_id="123",
        site="kwork",
        title="A scoped offer",
        body=("Детали по ссылке https://example.test " + "x" * 150),
        price="10000",
        duration="3",
    )
    assert "external contact" in (KworkAdapter.validate_offer(prepared) or "")

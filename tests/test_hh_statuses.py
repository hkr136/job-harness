from job_agent.sites.hh.adapter import HHAdapter


def test_hh_negotiation_status_mapping_is_conservative() -> None:
    assert HHAdapter.negotiation_status("Не просмотрен\nRole") == "submitted"
    assert HHAdapter.negotiation_status("Просмотрен\nRole") == "viewed"
    assert HHAdapter.negotiation_status("Собеседование\nRole") == "interview"
    assert HHAdapter.negotiation_status("Отказ\nRole") == "rejected"
    assert HHAdapter.negotiation_status("Role without a label") == "submitted"

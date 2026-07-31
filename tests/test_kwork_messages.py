from job_agent.sites.kwork.adapter import KworkAdapter


def test_kwork_only_reports_explicitly_unread_inbound_rows() -> None:
    messages = KworkAdapter.parse_unread_rows(
        [
            {"classes": ["chat__list-item"], "conversation_id": "old", "sender": "Buyer", "body": "Old preview"},
            {"classes": ["chat__list-item", "chat__list-item--unread"], "conversation_id": "new", "sender": "Buyer", "body": "New question"},
            {"classes": ["chat__list-item", "is-new"], "conversation_id": "own", "sender": "Buyer", "body": "Вы: My reply"},
        ]
    )
    assert len(messages) == 1
    assert messages[0].conversation_id == "new"
    assert messages[0].body == "New question"

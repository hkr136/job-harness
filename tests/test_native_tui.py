import os
from types import SimpleNamespace

from job_agent.tui.native import MENU_VIEWS, AnimationClock, NativeHarnessApp, UiEvent


def test_animation_clock_has_distinct_light_and_heavy_cadence() -> None:
    heavy = AnimationClock("heavy")
    light = AnimationClock("light")

    assert heavy.interval == 0.10
    assert light.interval == 0.25
    assert heavy.due(1.0)
    assert not light.due(1.0)
    assert light.due(1.0, active=True)
    light.advance(1.0)
    assert not light.due(1.1, active=True)


def test_ui_events_update_chat_only_in_primary_thread() -> None:
    app = NativeHarnessApp(writer=lambda _: None)
    app.chat_thinking = True
    app.busy_work.add("chat")
    app.events.put(UiEvent("chat_delta", "Первые токены ответа"))
    app.events.put(UiEvent("chat_done", payload=(12, "Готовый ответ")))

    app.consume_events()

    assert app.chat_stream_response == "Первые токены ответа"
    assert app.chat_session_id == 12
    assert app.chat_thinking is False
    assert "chat" not in app.busy_work


def test_options_screen_exposes_two_motion_profiles() -> None:
    app = NativeHarnessApp(writer=lambda _: None)
    app.view = "options"
    rendered = "\n".join(app.lines(120))

    assert "LIGHT" in rendered
    assert "HEAVY" in rendered


def test_native_tui_uses_primary_buffer_and_arrow_navigation() -> None:
    output: list[str] = []
    app = NativeHarnessApp(writer=output.append)

    app.handle_key("\t")
    app.menu_index = MENU_VIEWS.index("vacancies")
    app.handle_key("\r")
    assert app.view == "vacancies"
    app.handle_key("\x1b[B")
    app.handle_key("\x1b[A")
    assert app.view == "vacancies"

    app.render()
    rendered = "".join(output)
    assert "\x1b[?1049" not in rendered
    assert "\x1b[48;" not in rendered
    assert "\x1b[H\x1b[2J" in rendered
    assert "\r\n" in rendered


def test_native_tui_command_mode_keeps_safe_default() -> None:
    app = NativeHarnessApp(writer=lambda _: None)
    app.handle_key("/")
    for character in "arm nope":
        app.handle_key(character)
    app.handle_key("\r")
    assert not app.armed

    app.execute_command("arm I AUTHORIZE SENDING")
    assert app.armed


def test_shift_tab_arms_with_short_confirm_and_disarms_immediately() -> None:
    app = NativeHarnessApp(writer=lambda _: None)

    app.handle_key("\x1b[Z")
    assert app.arm_pending and not app.armed
    app.handle_key("\r")
    assert app.armed and not app.arm_pending
    app.handle_key("\x1b[Z")
    assert not app.armed


def test_native_tui_keeps_selected_row_inside_viewport() -> None:
    rows = list(range(1, 101))
    visible, start, selected_index = NativeHarnessApp.visible_window(rows, 80, lambda row: row, 12)

    assert 80 in visible
    assert selected_index == 79
    assert start > 0
    assert len(visible) == 12


def test_native_tui_reads_one_utf8_character_from_raw_bytes() -> None:
    read_fd, write_fd = os.pipe()
    try:
        os.write(write_fd, "Привет".encode("utf-8"))
        assert NativeHarnessApp.read_key(read_fd) == "П"
        assert NativeHarnessApp.read_key(read_fd) == "р"
    finally:
        os.close(read_fd)
        os.close(write_fd)


def test_chat_composer_queues_follow_up_without_losing_russian_text() -> None:
    app = NativeHarnessApp(writer=lambda _: None)
    app.chat_thinking = True
    for character in "Потом проверь сообщения":
        app.handle_key(character)
    app.handle_key("\r")

    assert app.chat_queued_input == "Потом проверь сообщения"
    assert app.chat_input == ""
    assert "QUEUED" in "\n".join(app.render_chat(100))


def test_chat_composer_supports_history_search_and_tool_toggle() -> None:
    app = NativeHarnessApp(writer=lambda _: None)
    app.chat_input_history = ["проверь hh", "проверь GeekJob"]
    app.handle_key("\x1b[A")
    assert app.chat_input == "проверь GeekJob"
    app.chat_input = ""
    app.handle_key("\x12")
    for character in "hh":
        app.handle_key(character)
    app.handle_key("\r")
    assert app.chat_input == "проверь hh"
    app.handle_key("\x0f")
    assert app.chat_show_tools


def test_menu_focus_opens_settings_and_prompt_screen() -> None:
    app = NativeHarnessApp(writer=lambda _: None)
    app.handle_key("\t")
    app.menu_index = MENU_VIEWS.index("settings")
    app.handle_key("\r")
    assert app.view == "settings"
    app.settings_index = 2
    app.handle_key("\r")
    assert app.view == "settings_prompts"
    rendered = "\n".join(app.lines(100, 30))
    assert "SETTINGS // PROMPTS" in rendered


def test_primary_renderer_clips_ansi_by_visible_width() -> None:
    app = NativeHarnessApp(writer=lambda _: None)
    value = "\x1b[38;5;80mОчень длинная строка для проверки\x1b[0m"
    assert app.visible_width(app.clip_ansi(value, 12)) <= 12


def test_transcript_labels_only_the_first_wrapped_line() -> None:
    app = NativeHarnessApp(writer=lambda _: None)
    rows = app.transcript_message("AGENT", "", "одно два три четыре пять шесть", 18)

    assert app.visible_width(rows[0]) > 0
    assert all("AGENT" not in row for row in rows[1:])


def test_transcript_preserves_distinct_sender_colours() -> None:
    app = NativeHarnessApp(writer=lambda _: None)
    assert "\x1b[38;5;80mYOU" in app.transcript_message("YOU", "\x1b[38;5;80m", "Привет", 40)[0]
    assert "\x1b[38;5;221mAGENT" in app.transcript_message("AGENT", "\x1b[38;5;221m", "Ответ", 40)[0]


def test_empty_composer_uses_a_soft_grey_placeholder() -> None:
    app = NativeHarnessApp(writer=lambda _: None)
    app.chat_editing = False
    rendered = "\n".join(app.render_chat(100))

    assert "\x1b[38;5;245m> Type a message…" in rendered


def test_empty_composer_hides_placeholder_as_soon_as_it_is_focused() -> None:
    app = NativeHarnessApp(writer=lambda _: None)
    app.chat_editing = True

    rendered = "\n".join(app.render_chat(100))
    assert "Type a message…" not in rendered
    assert "> \x1b[0m\x1b[38;5;80m▍" in rendered


def test_streaming_agent_message_labels_only_its_first_line(monkeypatch) -> None:
    app = NativeHarnessApp(writer=lambda _: None)
    app.chat_thinking = True
    app.chat_stream_response = "одно два три четыре пять шесть семь восемь девять"
    monkeypatch.setattr("job_agent.tui.native.shutil.get_terminal_size", lambda _: type("Size", (), {"lines": 20})())

    rendered = "\n".join(app.render_chat(36))
    assert rendered.count("AGENT") == 1


def test_composer_supports_left_right_home_end_and_middle_insertion() -> None:
    app = NativeHarnessApp(writer=lambda _: None)
    for character in "абв":
        app.handle_key(character)
    app.handle_key("\x1b[D")
    app.handle_key("X")
    assert app.chat_input == "абXв"
    app.handle_key("\x1b[3~")
    assert app.chat_input == "абX"
    app.handle_key("\x1b[H")
    app.handle_key("Y")
    app.handle_key("\x1b[F")
    app.handle_key("Z")
    assert app.chat_input == "YабXZ"


def test_vacancy_filters_and_sort_are_available_from_commands() -> None:
    app = NativeHarnessApp(writer=lambda _: None)

    app.execute_command("vacancies filter site kwork")
    app.execute_command("vacancies filter min 80")
    app.execute_command("vacancies sort newest")

    assert app.view == "vacancies"
    assert app.job_site_filter == "kwork"
    assert app.job_min_score == 80
    assert app.job_sort == "newest"

    app.execute_command("vacancies filter clear")
    assert app.job_site_filter is None
    assert app.job_min_score == 0


def test_vacancy_and_message_details_expose_direct_actions(monkeypatch) -> None:
    app = NativeHarnessApp(writer=lambda _: None)
    calls: list[tuple[str, int]] = []

    monkeypatch.setattr(app, "create_draft", lambda item_id: calls.append(("draft", item_id)))
    monkeypatch.setattr(app, "prepare_reply", lambda item_id: calls.append(("reply", item_id)))
    monkeypatch.setattr(app, "send_reply", lambda item_id: calls.append(("send", item_id)))
    monkeypatch.setattr(app, "run_agent", lambda kind, item_id: calls.append((kind, item_id)))

    app.view, app.selected_job_id = "vacancy_detail", 9
    app.handle_key("d")
    app.handle_key("r")
    app.view, app.selected_message_id = "message_detail", 4
    app.handle_key("r")
    app.handle_key("s")
    app.handle_key("g")

    assert calls == [("draft", 9), ("job", 9), ("reply", 4), ("send", 4), ("message", 4)]


def test_prepare_reply_uses_a_local_only_message_service(monkeypatch) -> None:
    app = NativeHarnessApp(writer=lambda _: None)
    received: list[object] = []

    class LocalReplyService:
        def __init__(self, store, adapter) -> None:  # type: ignore[no-untyped-def]
            received.extend((store, adapter))

        def prepare_reply(self, message_id, profile):  # type: ignore[no-untyped-def]
            return type("Reply", (), {"status": "draft"})()

    monkeypatch.setattr(app, "store", lambda: "store")
    monkeypatch.setattr("job_agent.tui.native.MessageService", LocalReplyService)
    monkeypatch.setattr("job_agent.tui.native.load_profile", lambda: {"candidate": {}})

    app.prepare_reply(7)

    assert received == ["store", None]
    assert "DRAFT READY — NOT SENT" in app.notice


def test_armed_send_enters_a_visible_non_blocking_sending_state(monkeypatch) -> None:
    app = NativeHarnessApp(writer=lambda _: None)
    app.armed = True
    started: list[str] = []
    monkeypatch.setattr(app, "start_background", lambda name, _work: started.append(name))

    app.send_reply(12)

    assert app.sending_message_id == 12
    assert started == ["send-reply-12"]
    assert "SENDING" in app.notice


def test_launcher_menu_navigates_with_arrows_and_opens_a_workspace() -> None:
    app = NativeHarnessApp(writer=lambda _: None)
    app.launcher_open = True
    app.menu_focused = True
    app.menu_index = 0

    app.handle_key("\x1b[B")
    assert MENU_VIEWS[app.menu_index] == "profile"
    app.handle_key("\x1b[B")
    assert MENU_VIEWS[app.menu_index] == "vacancies"
    app.handle_key("\r")

    assert app.view == "vacancies"
    assert not app.launcher_open
    assert "Opened vacancies" in app.notice


def test_create_draft_builds_pydantic_job_with_named_fields(monkeypatch) -> None:
    app = NativeHarnessApp(writer=lambda _: None)
    saved: list[object] = []
    job = SimpleNamespace(
        id=7,
        external_job_id="external-7",
        site="sample",
        url="https://example.test/7",
        title="Automation role",
        normalized_text="[normalized]\nNormalized description",
        description="Raw description",
    )

    class DraftStore:
        def get_job(self, item_id):  # type: ignore[no-untyped-def]
            assert item_id == 7
            return job, object()

        def save_draft(self, *args):  # type: ignore[no-untyped-def]
            saved.extend(args)

    monkeypatch.setattr(app, "store", lambda: DraftStore())
    monkeypatch.setattr("job_agent.tui.native.build_draft", lambda raw, _analysis: raw.description)

    app.create_draft(7)

    assert saved == [7, "sample", "Normalized description"]
    assert "Draft for vacancy #7 saved locally" in app.notice

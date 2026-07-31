"""OMP-style terminal workbench.

Unlike a full-screen UI framework this renderer deliberately stays in the
terminal's *primary* buffer.  It emits foreground-only ANSI sequences, so the
terminal application remains responsible for its background and transparency.
The implementation is small on purpose: terminal input and the workbench
state are owned here, while the database remains the single source of truth.
"""

from __future__ import annotations

import asyncio
import json
import os
import queue
import re
import select
import shutil
import sys
import termios
import textwrap
import threading
import time
import traceback
import tty
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TypeVar

import yaml

from job_agent.analysis.kwork_offer import build_kwork_offer, build_kwork_offer_with_provider
from job_agent.analysis.response_generator import build_draft, build_draft_with_provider
from job_agent.browser.manager import BrowserManager
from job_agent.config.settings import (
    USER_HOME,
    get_settings,
    load_profile,
    save_application_automation,
    save_llm_role,
    save_provider_enabled,
    save_scheduler_settings,
    save_site_enabled,
    save_tui_motion,
    save_tui_vacancy_sort,
)
from job_agent.database.repositories import Store
from job_agent.llm.factory import provider_for_role
from job_agent.llm.prompts import (
    DEFAULT_PROMPTS,
    get_system_prompt,
    reset_system_prompt,
    save_system_prompt,
)
from job_agent.llm.registry import ProviderDescriptor, discover_registry
from job_agent.models import PreparedApplication, RawJobDetails
from job_agent.services.application_service import ApplicationService
from job_agent.services.form_answers import save_profile_answer
from job_agent.services.harness import HarnessWorker
from job_agent.services.message_service import MessageService
from job_agent.services.orchestration import OrchestrationService, ToolCall
from job_agent.services.recovery_service import (
    adapter_test_target,
    apply_verified_adapter_recovery,
    is_recoverable_adapter_error,
)
from job_agent.sites.kwork.adapter import KworkAdapter
from job_agent.sites.registry import build_adapter

MENU_VIEWS = ("dashboard", "profile", "vacancies", "applications", "messages", "clarifications", "queue", "scheduler", "settings", "agent_activity", "chat")
COMMAND_VIEWS = {view: view for view in MENU_VIEWS}
COMMAND_VIEWS["models"] = "settings_models"
SETTINGS_SECTIONS = ("providers", "models", "prompts", "automation", "visual")
LLM_ROLES = ("normalization", "analysis", "writing", "application_review", "orchestration", "recovery")
PROFILE_SECTIONS = (
    ("target_roles", "Target roles", "Roles and directions to pursue."),
    ("skill_levels", "Skills", "Confirmed, learning and excluded technologies."),
    ("experience", "Experience & portfolio", "Projects and verified experience used in applications."),
    ("work_preferences", "Work preferences", "Availability, location, compensation, languages and exclusions."),
    ("application_rules", "Application rules", "Pricing, reusable form answers and application policy."),
)
ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")

RESET = "\x1b[0m"
DIM = "\x1b[2m"
PURPLE = "\x1b[38;5;141m"
CYAN = "\x1b[38;5;80m"
YELLOW = "\x1b[38;5;221m"
RED = "\x1b[38;5;210m"
GRAY = "\x1b[38;5;245m"
BOLD = "\x1b[1m"
Row = TypeVar("Row")

VIOLET = "\x1b[38;5;135m"
LILAC = "\x1b[38;5;183m"
CYAN_BRIGHT = "\x1b[38;5;123m"
SPINNER = ("◐", "◓", "◑", "◒")


@dataclass(frozen=True)
class UiEvent:
    kind: str
    detail: str = ""
    payload: object | None = None


class AnimationClock:
    """Small deterministic frame clock; never writes to the terminal itself."""

    def __init__(self, motion: str = "heavy") -> None:
        self.motion = motion if motion in {"light", "heavy"} else "heavy"
        self.frame = 0
        self.last_frame = 0.0

    @property
    def interval(self) -> float:
        return 0.10 if self.motion == "heavy" else 0.25

    def due(self, now: float, *, active: bool = False, stream: bool = False, dirty: bool = False) -> bool:
        if dirty:
            return True
        if self.motion == "heavy" and self.last_frame == 0.0:
            return True
        if stream or active:
            return now - self.last_frame >= 0.25  # 4 FPS only while work is visible.
        return self.motion == "heavy" and now - self.last_frame >= 1.5

    def advance(self, now: float) -> None:
        self.last_frame = now
        self.frame += 1


class NativeHarnessApp:
    """Keyboard-first primary-buffer UI modelled after OMP's normal mode."""

    def __init__(self, *, writer: Callable[[str], object] | None = None) -> None:
        self.view = "chat"
        self.menu_index = MENU_VIEWS.index("chat")
        self.menu_focused = False
        self.launcher_open = False
        self.settings_index = 0
        self.settings_role_index = 0
        self.model_picker = False
        self.model_picker_index = 0
        self.settings_prompt_role = "orchestration"
        self.settings_prompt_role_index = LLM_ROLES.index(self.settings_prompt_role)
        self.visual_index = 1  # heavy is the default
        self.scheduler_index = 0
        self.automation_index = 0
        self.prompt_editing = False
        self.prompt_buffer = ""
        self.prompt_original = ""
        self.prompt_cursor = 0
        self.profile_section_index = 0
        self.profile_editing = False
        self.profile_buffer = ""
        self.profile_original = ""
        self.profile_cursor = 0
        self.detail_origin: str | None = None
        self.selected_job_id: int | None = None
        self.selected_application_id: int | None = None
        self.submitting_application_id: int | None = None
        self.application_editing = False
        self.application_buffer = ""
        self.application_original = ""
        self.application_cursor = 0
        self.selected_message_id: int | None = None
        self.sending_message_id: int | None = None
        self.job_site_filter: str | None = None
        self.job_status_filter: str | None = None
        self.job_min_score = 0
        self.job_sort = get_settings().tui.vacancy_sort
        self.message_filter = "all"
        self.chat_session_id: int | None = None
        self.chat_input = ""
        self.chat_cursor = 0
        self.chat_editing = True
        self.chat_thinking = False
        self.chat_pulse = False
        self.chat_stream_response = ""
        self.chat_input_history: list[str] = []
        self.chat_history_index: int | None = None
        self.chat_queued_input = ""
        self.chat_show_tools = False
        self.chat_scroll_offset = 0
        self.chat_search_mode = False
        self.chat_search = ""
        self.registry: list[ProviderDescriptor] = []
        self.registry_loading = False
        self.selected_provider_id: str | None = None
        self.selected_site_id: str | None = None
        self.armed = False
        self.arm_pending = False
        self.command_mode = False
        self.command = ""
        self.notice = "Press V to start exploring vacancies.  / opens commands."
        self.running = True
        self._writer = writer or sys.stdout.write
        self._stdout_writer = writer is None
        self.motion = get_settings().tui.motion
        self.visual_index = 0 if self.motion == "light" else 1
        self.clock = AnimationClock(self.motion)
        self.events: queue.SimpleQueue[UiEvent] = queue.SimpleQueue()
        self.dirty = True
        self.busy_work: set[str] = set()
        self.success_until = 0.0

    @staticmethod
    def visible_width(value: str) -> int:
        """Terminal-cell width without ANSI escapes (good enough for Cyrillic)."""
        plain = ANSI_RE.sub("", value)
        return sum(0 if unicodedata.combining(char) else 2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1 for char in plain)

    @classmethod
    def clip_ansi(cls, value: str, width: int) -> str:
        """Clip a styled line by cells without ever slicing escape sequences."""
        if width <= 0:
            return ""
        result: list[str] = []
        cells = 0
        index = 0
        while index < len(value):
            if value[index] == "\x1b":
                match = ANSI_RE.match(value, index)
                if match:
                    result.append(match.group(0))
                    index = match.end()
                    continue
            char = value[index]
            char_width = 0 if unicodedata.combining(char) else 2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1
            if cells + char_width > width:
                break
            result.append(char)
            cells += char_width
            index += 1
        return "".join(result) + RESET

    @classmethod
    def wrap_ansi(cls, value: str, width: int) -> list[str]:
        """Wrap visible text safely; status styling is retained on every row."""
        width = max(8, width)
        plain = ANSI_RE.sub("", value)
        rows: list[str] = []
        for paragraph in plain.splitlines() or [""]:
            rows.extend(textwrap.wrap(paragraph, width=width, replace_whitespace=False, break_long_words=True) or [""])
        return rows

    def store(self) -> Store:
        return Store(get_settings().env.database_url)

    def run(self) -> None:
        """Run in the primary terminal buffer; never enter ``?1049``."""
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            raise RuntimeError("job-agent tui needs an interactive terminal")
        fd = sys.stdin.fileno()
        original = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            # Open a real start menu for interactive sessions.  Keeping this
            # out of __init__ preserves the reusable UI model used by tests
            # and by command-driven callers.
            self.launcher_open = True
            self.menu_focused = True
            self.menu_index = 0
            self.notice = "Choose a workspace with ↑/↓ and Enter."
            self.write("\x1b[?25l")  # cursor only; no alternate screen
            self.render()
            while self.running:
                self.consume_events()
                key = self.read_key(fd, timeout=self.clock.interval)
                if key is not None:
                    # A bad local database state or an adapter failure must
                    # never tear down the primary-buffer workbench.  Keep the
                    # terminal usable and leave a durable diagnostic instead.
                    try:
                        self.handle_key(key)
                    except Exception as error:
                        self.record_ui_error("keyboard action", error)
                        self.notice = f"Action failed safely: {type(error).__name__}: {error}"
                    self.dirty = True
                now = time.monotonic()
                active = bool(self.busy_work) or self.chat_thinking
                if self.clock.due(now, active=active, stream=bool(self.chat_stream_response and self.chat_thinking), dirty=self.dirty):
                    self.clock.advance(now)
                    self.render()
                    self.dirty = False
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, original)
            self.write(f"{RESET}\x1b[?25h\n")

    @staticmethod
    def read_key(fd: int, timeout: float | None = None) -> str | None:
        if timeout is not None and not select.select([fd], [], [], timeout)[0]:
            return None
        first = os.read(fd, 1)
        if first != b"\x1b":
            # Raw terminal input arrives byte-by-byte. UTF-8 Cyrillic and emoji
            # must be assembled before decoding; ``errors=ignore`` here would
            # silently discard every non-ASCII character.
            data = first
            for _ in range(3):
                try:
                    return data.decode("utf-8")
                except UnicodeDecodeError as error:
                    if error.reason != "unexpected end of data":
                        return ""
                    data += os.read(fd, 1)
            return data.decode("utf-8", "replace")
        if not select.select([fd], [], [], 0.035)[0]:
            return "escape"
        rest = os.read(fd, 8)
        return (first + rest).decode("utf-8", "ignore")

    def consume_events(self) -> None:
        while True:
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                return
            if event.kind == "chat_delta":
                self.chat_stream_response = event.detail
            elif event.kind == "chat_done":
                session_id, reply = event.payload  # type: ignore[misc]
                self.chat_session_id = int(session_id)
                self.chat_input = ""
                self.chat_cursor = 0
                self.chat_editing = True
                self.chat_thinking = False
                self.busy_work.discard("chat")
                self.notice = str(reply).splitlines()[0][:240]
                self.success_until = time.monotonic() + 1.2
                if self.chat_queued_input:
                    self.chat_input, self.chat_queued_input = self.chat_queued_input, ""
                    self.notice = "Processing queued follow-up…"
                    self.send_chat()
            elif event.kind == "registry_done":
                self.registry = list(event.payload or [])  # type: ignore[arg-type]
                if self.registry and self.selected_provider_id not in {item.provider_id for item in self.registry}:
                    self.selected_provider_id = self.registry[0].provider_id
                self.registry_loading = False
                self.busy_work.discard("registry")
                self.notice = "Provider registry refreshed."
            elif event.kind == "work_done":
                self.busy_work.discard(event.detail)
                if event.detail.startswith("send-reply-"):
                    self.sending_message_id = None
                if event.detail.startswith("submit-application-"):
                    self.submitting_application_id = None
                self.notice = str(event.payload or "Completed.")
                self.success_until = time.monotonic() + 1.2
            elif event.kind == "work_failed":
                self.busy_work.discard(event.detail)
                if event.detail.startswith("send-reply-"):
                    self.sending_message_id = None
                if event.detail.startswith("submit-application-"):
                    self.submitting_application_id = None
                if event.detail == "chat":
                    self.chat_thinking = False
                if event.detail == "registry":
                    self.registry_loading = False
                self.notice = f"{event.detail} failed: {event.payload}"
            self.dirty = True

    def write(self, value: str) -> None:
        self._writer(value)
        if self._stdout_writer:
            sys.stdout.flush()

    @staticmethod
    def record_ui_error(action: str, error: Exception) -> None:
        """Persist a UI traceback without mixing it into user-facing data."""
        try:
            path = Path.home() / ".job-harness" / "logs" / "tui.log"
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as stream:
                stream.write(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] {action}: {type(error).__name__}: {error}\n")
                stream.write("".join(traceback.format_exception(error)))
        except OSError:
            # Reporting must not become a second terminal failure.
            pass

    def handle_key(self, key: str) -> None:
        # Match the familiar Plan/Agent gesture in terminal LLM clients. The
        # pending stop prevents an accidental external-action permission while
        # still avoiding an awkward typed confirmation phrase.
        if key == "\x1b[Z":  # Shift+Tab
            if self.armed:
                self.armed, self.arm_pending = False, False
                self.notice = "SAFE mode restored."
            else:
                self.arm_pending = not self.arm_pending
                self.notice = "ARM READY · press Enter to enable external actions." if self.arm_pending else "SAFE mode retained."
            return

        if self.arm_pending:
            if key in {"\r", "\n"}:
                self.armed, self.arm_pending = True, False
                self.notice = "ARMED for this TUI session. Shift+Tab returns to SAFE."
                return
            if key in {"escape", "\x03"}:
                self.arm_pending = False
                self.notice = "ARM cancelled; SAFE mode retained."
                return

        # Tab is the single global way to enter the visible navigation menu.
        # Handle it before a focused chat composer so it cannot be swallowed
        # as an ordinary input character.
        if key == "\t" and not self.launcher_open:
            self.menu_focused = True
            self.launcher_open = True
            self.notice = f"Menu: {MENU_VIEWS[self.menu_index]}. Use arrows and Enter."
            return

        if self.launcher_open:
            if key in {"\x1b[B", "\t", "\x1b[C"}:
                self.menu_index = (self.menu_index + 1) % len(MENU_VIEWS)
            elif key in {"\x1b[A", "\x1b[D"}:
                self.menu_index = (self.menu_index - 1) % len(MENU_VIEWS)
            elif key in {"\r", "\n"}:
                self.view = MENU_VIEWS[self.menu_index]
                self.launcher_open = self.menu_focused = False
                if self.view == "chat":
                    self.chat_editing = True
                self.notice = f"Opened {self.view}. Tab opens the menu again."
            elif key in {"\x03", "\x11"}:
                self.running = False
            elif key in {"escape", "\x1b"}:
                self.launcher_open = self.menu_focused = False
                self.notice = "Start menu closed. Tab opens it again."
            return

        if self.command_mode:
            if key in {"\r", "\n"}:
                self.execute_command(self.command.strip())
                self.command_mode, self.command = False, ""
            elif key in {"escape", "\x03"}:
                self.command_mode, self.command = False, ""
                self.notice = "Command cancelled."
            elif key in {"\x7f", "\b"}:
                self.command = self.command[:-1]
            elif key.isprintable():
                self.command += key
            return

        if self.prompt_editing:
            if key == "\x13":  # Ctrl+S
                try:
                    save_system_prompt(self.settings_prompt_role, self.prompt_buffer)
                except ValueError as error:
                    self.notice = str(error)
                else:
                    self.prompt_editing = False
                    self.notice = f"Saved {self.settings_prompt_role} prompt in ~/.job-harness/prompts/."
                return
            if key in {"escape", "\x03"}:
                self.prompt_editing = False
                self.prompt_buffer = self.prompt_original
                self.prompt_cursor = len(self.prompt_buffer)
                self.notice = "Prompt edit cancelled."
                return
            if key == "\x1b[D":
                self.prompt_cursor = max(0, self.prompt_cursor - 1)
            elif key == "\x1b[C":
                self.prompt_cursor = min(len(self.prompt_buffer), self.prompt_cursor + 1)
            elif key in {"\x1b[H", "\x1bOH"}:
                self.prompt_cursor = 0
            elif key in {"\x1b[F", "\x1bOF"}:
                self.prompt_cursor = len(self.prompt_buffer)
            elif key == "\x1b[3~":
                self.prompt_buffer = self.prompt_buffer[: self.prompt_cursor] + self.prompt_buffer[self.prompt_cursor + 1 :]
            elif key in {"\x7f", "\b"} and self.prompt_cursor:
                self.prompt_buffer = self.prompt_buffer[: self.prompt_cursor - 1] + self.prompt_buffer[self.prompt_cursor :]
                self.prompt_cursor -= 1
            elif key == "\x1b\r":
                self.insert_prompt_text("\n")
            elif key in {"\r", "\n"}:
                self.insert_prompt_text("\n")
            elif key.isprintable():
                self.insert_prompt_text(key)
            return

        if self.profile_editing:
            if key == "\x13":  # Ctrl+S
                self.save_profile_section()
                return
            if key in {"escape", "\x03"}:
                self.profile_editing = False
                self.profile_buffer = self.profile_original
                self.profile_cursor = len(self.profile_buffer)
                self.notice = "Profile edit cancelled; user data was not changed."
                return
            if key == "\x1b[D":
                self.profile_cursor = max(0, self.profile_cursor - 1)
            elif key == "\x1b[C":
                self.profile_cursor = min(len(self.profile_buffer), self.profile_cursor + 1)
            elif key in {"\x1b[H", "\x1bOH"}:
                self.profile_cursor = 0
            elif key in {"\x1b[F", "\x1bOF"}:
                self.profile_cursor = len(self.profile_buffer)
            elif key == "\x1b[3~":
                self.profile_buffer = self.profile_buffer[: self.profile_cursor] + self.profile_buffer[self.profile_cursor + 1 :]
            elif key in {"\x7f", "\b"} and self.profile_cursor:
                self.profile_buffer = self.profile_buffer[: self.profile_cursor - 1] + self.profile_buffer[self.profile_cursor :]
                self.profile_cursor -= 1
            elif key in {"\x1b\r", "\r", "\n"}:
                self.insert_profile_text("\n")
            elif key.isprintable():
                self.insert_profile_text(key)
            return

        if self.application_editing:
            if key == "\x13":  # Ctrl+S
                self.save_application_edit()
                return
            if key in {"escape", "\x03"}:
                self.application_editing = False
                self.application_buffer = self.application_original
                self.application_cursor = len(self.application_buffer)
                self.notice = "Application edit cancelled; saved draft was not changed."
                return
            if key == "\x1b\r":
                self.insert_application_text("\n")
            elif key in {"\r", "\n"}:
                self.insert_application_text("\n")
            elif key == "\x1b[D":
                self.application_cursor = max(0, self.application_cursor - 1)
            elif key == "\x1b[C":
                self.application_cursor = min(len(self.application_buffer), self.application_cursor + 1)
            elif key in {"\x1b[H", "\x1bOH"}:
                self.application_cursor = 0
            elif key in {"\x1b[F", "\x1bOF"}:
                self.application_cursor = len(self.application_buffer)
            elif key == "\x1b[3~":
                self.application_buffer = self.application_buffer[: self.application_cursor] + self.application_buffer[self.application_cursor + 1 :]
            elif key in {"\x7f", "\b"} and self.application_cursor:
                self.application_buffer = self.application_buffer[: self.application_cursor - 1] + self.application_buffer[self.application_cursor :]
                self.application_cursor -= 1
            elif key.isprintable():
                self.insert_application_text(key)
            return

        if self.view == "chat" and key == "\x0f":  # Ctrl+O
            self.chat_show_tools = not self.chat_show_tools
            self.notice = "Tool transcript expanded." if self.chat_show_tools else "Tool transcript collapsed."
            return

        if self.view == "vacancy_detail" and self.selected_job_id:
            if key.lower() == "d":
                self.create_draft(self.selected_job_id)
                return
            if key.lower() == "f":
                self.store().set_job_status(self.selected_job_id, "favorite")
                self.notice = f"Vacancy #{self.selected_job_id} saved to favorites."
                return
            if key.lower() == "i":
                self.store().set_job_status(self.selected_job_id, "ignored")
                self.notice = f"Vacancy #{self.selected_job_id} ignored."
                return
            if key.lower() == "r":
                self.run_agent("job", self.selected_job_id)
                return

        if self.view == "message_detail" and self.selected_message_id:
            if key.lower() == "r":
                self.prepare_reply(self.selected_message_id)
                return
            if key.lower() == "s":
                self.send_reply(self.selected_message_id)
                return
            if key.lower() == "g":
                self.run_agent("message", self.selected_message_id)
                return

        if self.view == "application_detail" and self.selected_application_id:
            if key.lower() == "e":
                self.open_application_editor(self.selected_application_id)
                return
            if key.lower() == "s":
                try:
                    application = self.store().get_application(self.selected_application_id)
                except ValueError as error:
                    self.notice = str(error)
                else:
                    self.submit_application(application.job_id)
                return
            if key.lower() == "r":
                try:
                    application = self.store().get_application(self.selected_application_id)
                except ValueError as error:
                    self.notice = str(error)
                else:
                    self.regenerate_application(application.job_id)
                return

        if self.view == "profile_detail":
            if key.lower() == "e":
                self.open_profile_editor()
                return
        if self.view == "chat" and self.chat_editing:
            if self.chat_search_mode:
                if key in {"escape", "\x03"}:
                    self.chat_search_mode, self.chat_search = False, ""
                elif key in {"\r", "\n"}:
                    match = next((item for item in reversed(self.chat_input_history) if self.chat_search.casefold() in item.casefold()), "")
                    self.chat_input = match
                    self.chat_search_mode, self.chat_search = False, ""
                elif key in {"\x7f", "\b"}:
                    self.chat_search = self.chat_search[:-1]
                elif key.isprintable():
                    self.chat_search += key
                return
            if key == "\x12":  # Ctrl+R
                self.chat_search_mode = True
            elif key == "\x1b\r":  # Alt+Enter in terminal raw mode
                self.insert_chat_text("\n")
            elif key in {"\r", "\n"}:
                self.queue_or_send_chat()
            elif key in {"\x1b[D"}:
                self.chat_cursor = max(0, self.chat_cursor - 1)
            elif key in {"\x1b[C"}:
                self.chat_cursor = min(len(self.chat_input), self.chat_cursor + 1)
            elif key in {"\x1b[H", "\x1bOH"}:
                self.chat_cursor = 0
            elif key in {"\x1b[F", "\x1bOF"}:
                self.chat_cursor = len(self.chat_input)
            elif key == "\x1b[3~":  # Delete removes the character to the right.
                self.chat_input = self.chat_input[: self.chat_cursor] + self.chat_input[self.chat_cursor + 1 :]
            elif key in {"\x1b[A", "\x1b[B"} and not self.chat_input:
                self.recall_chat_input(-1 if key == "\x1b[A" else 1)
            elif key in {"escape", "\x03"}:
                self.chat_editing = False
                self.notice = "Composer unfocused; draft is preserved."
            elif key in {"\x7f", "\b"}:
                if self.chat_cursor:
                    self.chat_input = self.chat_input[: self.chat_cursor - 1] + self.chat_input[self.chat_cursor :]
                    self.chat_cursor -= 1
            elif key == "/" and not self.chat_input:
                self.chat_editing, self.command_mode, self.command = False, True, ""
            elif key.isprintable():
                self.insert_chat_text(key)
            return

        if self.view == "chat" and not self.chat_editing:
            if key in {"\r", "\n", "/"}:
                self.chat_editing = True
                self.notice = "Composer focused."
                return
            if key in {"\x1b[A", "\x1b[D", "\x1b[5~", "\x1b[H", "\x1bOH"}:
                step = 1 if key in {"\x1b[A", "\x1b[D"} else 8 if key == "\x1b[5~" else 9999
                self.chat_scroll_offset += step
                return
            if key in {"\x1b[B", "\x1b[C", "\x1b[6~", "\x1b[F", "\x1bOF"}:
                step = 1 if key in {"\x1b[B", "\x1b[C"} else 8 if key == "\x1b[6~" else 9999
                self.chat_scroll_offset = max(0, self.chat_scroll_offset - step)
                return

        if self.view.startswith("settings"):
            if self.model_picker:
                choices = self.model_choices()
                if key in {"\x1b[B", "\x1b[A"} and choices:
                    direction = -1 if key == "\x1b[A" else 1
                    self.model_picker_index = (self.model_picker_index + direction) % len(choices)
                    return
                if key in {"\r", "\n"} and choices:
                    role = LLM_ROLES[self.settings_role_index]
                    save_llm_role(role, choices[self.model_picker_index])
                    self.model_picker = False
                    self.notice = f"{role} now uses {choices[self.model_picker_index]}."
                    return
                if key in {"escape", "\x03"}:
                    self.model_picker = False
                    self.notice = "Model selection cancelled."
                    return
            if self.view == "settings_providers" and key.lower() in {"e", "x"}:
                provider_id = self.selected_provider_id or (self.registry[0].provider_id if self.registry else None)
                if not provider_id:
                    self.notice = "Refresh provider discovery first."
                else:
                    try:
                        save_provider_enabled(provider_id, key.lower() == "e")
                    except ValueError as error:
                        self.notice = str(error)
                    else:
                        self.notice = f"Provider {provider_id} {'enabled' if key.lower() == 'e' else 'disabled'}."
                        self.refresh_registry()
                return
            if self.view == "settings_providers" and key in {"\x1b[B", "\x1b[A"}:
                ids = [item.provider_id for item in self.registry]
                if ids:
                    current = ids.index(self.selected_provider_id) if self.selected_provider_id in ids else 0
                    direction = -1 if key == "\x1b[A" else 1
                    self.selected_provider_id = ids[(current + direction) % len(ids)]
                return
            if self.view == "settings_automation" and key in {"\x1b[B", "\x1b[A"}:
                actions = self.automation_actions()
                if actions:
                    direction = -1 if key == "\x1b[A" else 1
                    self.automation_index = (self.automation_index + direction) % len(actions)
                return
            if self.view == "settings_automation" and key in {"\r", "\n"}:
                self.run_automation_action()
                return
            if self.view == "settings_models" and key in {"\x1b[B", "\x1b[A"}:
                direction = -1 if key == "\x1b[A" else 1
                self.settings_role_index = (self.settings_role_index + direction) % len(LLM_ROLES)
                return
            if self.view == "settings_models" and key in {"\r", "\n", "e"}:
                if not self.registry:
                    self.refresh_registry()
                    self.notice = "Discovering models; open selection again when it completes."
                elif not self.model_choices():
                    self.notice = "No available models. Configure or enable a provider first."
                else:
                    self.model_picker = True
                    self.model_picker_index = 0
                    self.notice = "Choose a model with ↑/↓ and Enter."
                return
            if self.view == "settings" and key == "\x1b[B":
                self.settings_index = (self.settings_index + 1) % len(SETTINGS_SECTIONS)
                return
            if self.view == "settings" and key == "\x1b[A":
                self.settings_index = (self.settings_index - 1) % len(SETTINGS_SECTIONS)
                return
            if self.view == "settings" and key in {"\r", "\n"}:
                self.open_settings_section()
                return
            if self.view == "settings_prompts" and key in {"\x1b[B", "\x1b[A"}:
                direction = -1 if key == "\x1b[A" else 1
                self.settings_prompt_role_index = (self.settings_prompt_role_index + direction) % len(LLM_ROLES)
                self.settings_prompt_role = LLM_ROLES[self.settings_prompt_role_index]
                return
            if self.view == "settings_prompts" and key in {"\r", "\n"}:
                self.open_prompt_editor()
                return
            if self.view == "settings_prompts" and key.lower() == "e":
                self.open_prompt_editor()
                return
            if self.view == "settings_prompts" and key.lower() == "x":
                reset_system_prompt(self.settings_prompt_role)
                self.notice = f"{self.settings_prompt_role} prompt reset to the built-in template."
                return

        if self.view in {"options", "settings_visual"} and key in {"\x1b[B", "\x1b[A"}:
            direction = -1 if key == "\x1b[A" else 1
            self.visual_index = (self.visual_index + direction) % 2
            return

        if self.view == "profile":
            if key in {"\x1b[B", "\x1b[A"}:
                direction = -1 if key == "\x1b[A" else 1
                self.profile_section_index = (self.profile_section_index + direction) % len(PROFILE_SECTIONS)
                return
            if key in {"\r", "\n"}:
                self.detail_origin, self.view = "profile", "profile_detail"
                return
        if self.view in {"options", "settings_visual"} and key in {"\r", "\n"}:
            self.set_motion(("light", "heavy")[self.visual_index])
            return

        if self.view == "vacancies":
            if key.lower() == "o":
                sorts = ("fresh", "analyzed", "score", "status", "site")
                self.job_sort = sorts[(sorts.index(self.job_sort) + 1) % len(sorts)]
                save_tui_vacancy_sort(self.job_sort)
                self.notice = f"Vacancies sorted by {self.job_sort}."
                return
            if key.lower() == "f":
                thresholds = (0, 45, 60, 72, 85)
                self.job_min_score = thresholds[(thresholds.index(self.job_min_score) + 1) % len(thresholds)] if self.job_min_score in thresholds else 0
                self.notice = f"Minimum score filter: {self.job_min_score}."
                return
            if key.lower() == "p":
                sites = (None, *get_settings().sites.keys())
                current = sites.index(self.job_site_filter) if self.job_site_filter in sites else 0
                self.job_site_filter = sites[(current + 1) % len(sites)]
                self.notice = f"Site filter: {self.job_site_filter or 'all'}."
                return
            if key.lower() == "t":
                states = (None, "new", "analyzed", "favorite", "draft_created", "applied", "needs_clarification", "ignored")
                current = states.index(self.job_status_filter) if self.job_status_filter in states else 0
                self.job_status_filter = states[(current + 1) % len(states)]
                self.notice = f"Status filter: {self.job_status_filter or 'all'}."
                return
            if key.lower() == "x":
                self.job_site_filter, self.job_status_filter, self.job_min_score = None, None, 0
                self.notice = "Vacancy filters cleared."
                return

        if self.view == "queue" and key.lower() in {"1", "2", "3"}:
            self.queue_work({"1": "scan", "2": "messages", "3": "statuses"}[key.lower()])
            return

        if self.view == "scheduler":
            jobs = list(get_settings().scheduler.get("jobs", {}))
            if key in {"\x1b[B", "\x1b[A"} and jobs:
                direction = -1 if key == "\x1b[A" else 1
                self.scheduler_index = (self.scheduler_index + direction) % len(jobs)
                return
            if key == " ":
                settings = get_settings()
                save_scheduler_settings(enabled=not bool(settings.scheduler.get("enabled", False)))
                self.notice = f"Scheduler {'enabled' if not bool(settings.scheduler.get('enabled', False)) else 'disabled'} in user settings."
                return
            if key.lower() == "e" and jobs:
                job = jobs[self.scheduler_index % len(jobs)]
                entry = get_settings().scheduler.get("jobs", {}).get(job, {})
                save_scheduler_settings(job_name=job, job_enabled=not bool(entry.get("enabled", False)))
                self.notice = f"Scheduled {job} {'enabled' if not bool(entry.get('enabled', False)) else 'disabled'}."
                return
            if key in {"+", "-"} and jobs:
                job = jobs[self.scheduler_index % len(jobs)]
                entry = get_settings().scheduler.get("jobs", {}).get(job, {})
                interval = int(entry.get("interval_minutes", 180)) + (15 if key == "+" else -15)
                save_scheduler_settings(job_name=job, interval_minutes=interval)
                self.notice = f"{job} interval set to {max(15, interval)} minutes."
                return

        if key in {"\t", "\x1b[C", "\x1b[D"}:
            direction = -1 if key == "\x1b[D" else 1
            self.menu_index = (self.menu_index + direction) % len(MENU_VIEWS)
            self.menu_focused = True
            self.launcher_open = True
            self.notice = f"Menu: {MENU_VIEWS[self.menu_index]}. Press Enter to open."
            return

        if self.view in {"options", "settings_visual"} and key in {"1", "2"}:
            self.set_motion("light" if key == "1" else "heavy")
            return

        if key in {"\x03", "\x11"}:  # Ctrl+C / Ctrl+Q
            self.running = False
        elif key == "/":
            self.command_mode = True
            self.command = ""
        elif key == "\x1b[B":
            self.move_selection(1)
        elif key == "\x1b[A":
            self.move_selection(-1)
        elif key in {"\r", "\n"}:
            if self.menu_focused:
                self.view = MENU_VIEWS[self.menu_index]
                self.menu_focused = False
                if self.view == "chat":
                    self.chat_editing = True
            elif self.view == "chat":
                self.chat_editing = True
            else:
                self.open_selected()
        elif key in {"escape", "\x1b"}:
            self.back()
        elif key == "?":
            self.notice = "↑/↓ select · Enter opens · Esc goes back · / commands · Ctrl+Q exits"
        elif key.lower() == "r":
            if self.view in {"settings_providers", "settings_models"}:
                self.refresh_registry()
            else:
                self.notice = "Data refreshed."

    def back(self) -> None:
        if self.detail_origin:
            self.view, self.detail_origin = self.detail_origin, None
        else:
            self.view = "settings" if self.view.startswith("settings_") else "dashboard"

    def open_settings_section(self) -> None:
        section = SETTINGS_SECTIONS[self.settings_index]
        self.view = f"settings_{section}"
        if section in {"providers", "models"} and not self.registry:
            self.refresh_registry()

    def automation_actions(self) -> list[tuple[str, str | None]]:
        """Visible Automation controls, ordered exactly as the user navigates."""
        return [
            ("auto_toggle", None),
            ("match_down", None),
            ("match_up", None),
            ("review_down", None),
            ("review_up", None),
            ("reply_toggle", None),
            *(("site_toggle", site) for site in get_settings().sites),
        ]

    def run_automation_action(self) -> None:
        actions = self.automation_actions()
        if not actions:
            self.notice = "No automation settings are available."
            return
        action, site = actions[self.automation_index % len(actions)]
        settings = get_settings()
        if action == "auto_toggle":
            enabled = not (settings.applications.auto_mode or settings.applications.unattended_submission)
            save_application_automation(auto_mode=enabled)
            self.notice = f"Auto workflow {'enabled' if enabled else 'disabled'}. Manual ARMED sends are unaffected."
        elif action in {"match_down", "match_up"}:
            threshold = settings.applications.auto_match_threshold + (-1 if action == "match_down" else 1)
            save_application_automation(auto_match_threshold=threshold)
            self.notice = f"Auto match threshold set to {max(0, min(100, threshold))}."
        elif action in {"review_down", "review_up"}:
            threshold = settings.applications.auto_review_threshold + (-1 if action == "review_down" else 1)
            save_application_automation(auto_review_threshold=threshold)
            self.notice = f"Application review threshold set to {max(0, min(100, threshold))}."
        elif action == "reply_toggle":
            enabled = not settings.applications.auto_reply_messages
            save_application_automation(auto_reply_messages=enabled)
            self.notice = f"Automatic factual message replies {'enabled' if enabled else 'disabled'}."
        elif action == "site_toggle" and site:
            config = settings.sites[site]
            enabled = not config.enabled
            save_site_enabled(site, enabled)
            self.notice = f"Site {site} {'enabled' if enabled else 'disabled'}."

    def model_choices(self) -> list[str]:
        return [model.qualified_id for provider in self.registry if provider.availability == "available" for model in provider.models]

    def open_prompt_editor(self) -> None:
        self.prompt_original = get_system_prompt(self.settings_prompt_role)
        self.prompt_buffer = self.prompt_original
        self.prompt_cursor = len(self.prompt_buffer)
        self.prompt_editing = True
        self.notice = "Prompt editor: Ctrl+S saves · Esc cancels · Enter adds a line."

    def selected_profile_section(self) -> tuple[str, str, str]:
        return PROFILE_SECTIONS[self.profile_section_index % len(PROFILE_SECTIONS)]

    def profile_section_value(self, profile: dict[str, object] | None = None, section_key: str | None = None) -> object:
        """Return a candidate-owned fragment without exposing app configuration."""
        data = profile if profile is not None else load_profile()
        candidate = data.get("candidate", {}) if isinstance(data.get("candidate"), dict) else {}
        key = section_key or self.selected_profile_section()[0]
        if key == "work_preferences":
            return {
                field: candidate[field]
                for field in ("availability", "location", "compensation", "languages", "preferred_work", "excluded_work")
                if field in candidate
            }
        if key == "application_rules":
            return {
                field: candidate[field]
                for field in ("application_rules", "form_answers")
                if field in candidate
            }
        return candidate.get(key, {})

    def open_profile_editor(self) -> None:
        key, label, _ = self.selected_profile_section()
        value = self.profile_section_value()
        self.profile_original = yaml.safe_dump(value, allow_unicode=True, sort_keys=False).rstrip() + "\n"
        self.profile_buffer = self.profile_original
        self.profile_cursor = len(self.profile_buffer)
        self.profile_editing = True
        self.notice = f"Editing {label}: Ctrl+S saves only this profile section · Esc cancels."

    def insert_profile_text(self, value: str) -> None:
        self.profile_buffer = self.profile_buffer[: self.profile_cursor] + value + self.profile_buffer[self.profile_cursor :]
        self.profile_cursor += len(value)

    def save_profile_section(self) -> None:
        key, label, _ = self.selected_profile_section()
        try:
            value = yaml.safe_load(self.profile_buffer)
        except yaml.YAMLError as error:
            self.notice = f"Profile was not saved: invalid YAML ({error})."
            return
        if value is None:
            value = {} if key not in {"target_roles"} else []
        try:
            path = USER_HOME / "profile.yaml"
            profile = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            if not isinstance(profile, dict):
                raise ValueError("profile root must be a mapping")
            candidate = profile.setdefault("candidate", {})
            if not isinstance(candidate, dict):
                raise ValueError("candidate must be a mapping")
            if key == "work_preferences":
                if not isinstance(value, dict):
                    raise ValueError("work preferences must be a YAML mapping")
                for field in ("availability", "location", "compensation", "languages", "preferred_work", "excluded_work"):
                    if field in value:
                        candidate[field] = value[field]
                    else:
                        candidate.pop(field, None)
            elif key == "application_rules":
                if not isinstance(value, dict):
                    raise ValueError("application rules must be a YAML mapping")
                for field in ("application_rules", "form_answers"):
                    if field in value:
                        candidate[field] = value[field]
                    else:
                        candidate.pop(field, None)
            else:
                candidate[key] = value
            path.write_text(yaml.safe_dump(profile, allow_unicode=True, sort_keys=False), encoding="utf-8")
        except (OSError, ValueError) as error:
            self.notice = f"Profile was not saved: {error}."
            return
        self.profile_editing = False
        self.notice = f"Saved {label} in ~/.job-harness/profile.yaml. It will be used in future analysis and drafts."

    def move_selection(self, offset: int) -> None:
        if self.view == "vacancies":
            ids = [job.id for job, _ in self.filtered_jobs()]
            attribute = "selected_job_id"
        elif self.view == "applications":
            ids = [item.id for item in self.store().list_applications()]
            attribute = "selected_application_id"
        elif self.view == "messages":
            ids = [item.id for item in self.filtered_messages()]
            attribute = "selected_message_id"
        else:
            self.notice = "This screen has no selectable rows."
            return
        if not ids:
            self.notice = "Nothing to select."
            return
        current = getattr(self, attribute)
        index = ids.index(current) if current in ids else (-1 if offset > 0 else 0)
        setattr(self, attribute, ids[(index + offset) % len(ids)])

    def open_selected(self) -> None:
        if self.view == "vacancies" and self.selected_job_id:
            self.detail_origin, self.view = "vacancies", "vacancy_detail"
        elif self.view == "applications" and self.selected_application_id:
            self.detail_origin, self.view = "applications", "application_detail"
        elif self.view == "messages" and self.selected_message_id:
            self.detail_origin, self.view = "messages", "message_detail"
        else:
            self.move_selection(1)

    def execute_command(self, command: str) -> None:
        parts = command.split(maxsplit=2)
        if not parts:
            return
        action = parts[0].lower()
        if action in COMMAND_VIEWS and not (action == "vacancies" and len(parts) > 1):
            self.view, self.detail_origin = COMMAND_VIEWS[action], None
            if self.view == "settings_models" and not self.registry:
                self.refresh_registry()
            return
        if action == "settings":
            section = parts[1].lower() if len(parts) > 1 else ""
            if section in SETTINGS_SECTIONS:
                self.settings_index = SETTINGS_SECTIONS.index(section)
                self.open_settings_section()
            else:
                self.view = "settings"
            return
        if action == "prompt" and len(parts) > 2 and parts[1] == "reset":
            try:
                reset_system_prompt(parts[2])
            except ValueError as error:
                self.notice = str(error)
            else:
                self.notice = f"{parts[2]} prompt reset to the built-in template."
            return
        if action == "safe":
            self.armed, self.arm_pending = False, False
            self.notice = "SAFE mode restored."
            return
        if action == "motion" and len(parts) > 1:
            self.set_motion(parts[1].lower())
            return
        if action == "models" and len(parts) > 1 and parts[1] == "refresh":
            self.refresh_registry()
            return
        if action == "provider" and len(parts) > 2 and parts[1] in {"enable", "disable"}:
            try:
                save_provider_enabled(parts[2], parts[1] == "enable")
            except ValueError as error:
                self.notice = str(error)
            else:
                self.notice = f"Provider {parts[2]} {parts[1]}d."
                self.refresh_registry()
            return
        if action == "site" and len(parts) > 2 and parts[1] in {"enable", "disable"}:
            try:
                save_site_enabled(parts[2], parts[1] == "enable")
            except ValueError as error:
                self.notice = str(error)
            else:
                self.notice = f"Site {parts[2]} {parts[1]}d."
            return
        if action == "model" and len(parts) > 2:
            try:
                selection = parts[2].split()
                model_ref = selection[0]
                fallbacks = selection[2:] if len(selection) > 1 and selection[1] == "fallback" else None
                if len(selection) > 1 and selection[1] != "fallback":
                    raise ValueError("Use: model ROLE PROVIDER/MODEL [fallback PROVIDER/MODEL ...]")
                save_llm_role(parts[1], model_ref, fallbacks=fallbacks)
            except ValueError as error:
                self.notice = str(error)
            else:
                self.notice = f"{parts[1]} now uses {model_ref}."
                self.refresh_registry()
            return
        if action == "arm":
            self.armed = len(parts) > 1 and " ".join(parts[1:]) == "I AUTHORIZE SENDING"
            self.arm_pending = False
            self.notice = "ARMED for this session." if self.armed else "Exact phrase required; still SAFE."
            return
        if action == "draft" and len(parts) > 1 and parts[1].isdigit():
            self.create_draft(int(parts[1]))
            return
        if action == "scan":
            self.scan_now(parts[1] if len(parts) > 1 else None)
            return
        if action == "chat":
            self.view = "chat"
            self.chat_editing = True
            self.chat_input = " ".join(parts[1:]) if len(parts) > 1 else ""
            if self.chat_input:
                self.send_chat()
            return
        if action == "vacancies" and len(parts) > 2:
            setting, value = parts[1].lower(), parts[2].strip()
            if setting == "sort" and value in {"score", "newest", "title"}:
                self.job_sort, self.view = value, "vacancies"
                self.notice = f"Vacancies sorted by {value}."
                return
            if setting == "filter":
                field, _, field_value = value.partition(" ")
                if field == "site":
                    self.job_site_filter = None if field_value in {"", "all"} else field_value
                elif field == "status":
                    self.job_status_filter = None if field_value in {"", "all"} else field_value
                elif field == "min" and field_value.isdigit():
                    self.job_min_score = int(field_value)
                elif field == "clear":
                    self.job_site_filter, self.job_status_filter, self.job_min_score = None, None, 0
                else:
                    self.notice = "Use: vacancies filter site SITE|status STATUS|min SCORE|clear"
                    return
                self.view = "vacancies"
                self.notice = "Vacancy filters updated."
                return
        if action == "messages" and len(parts) > 2 and parts[1] == "filter":
            value = parts[2].lower()
            if value not in {"all", "action", "drafts", "clarifications", "sent", "errors"}:
                self.notice = "Use: messages filter all|action|drafts|clarifications|sent|errors"
            else:
                self.message_filter, self.view = value, "messages"
                self.notice = f"Message filter: {value}."
            return
        if action == "agent" and len(parts) > 2 and parts[1] in {"message", "job"} and parts[2].isdigit():
            self.run_agent(parts[1], int(parts[2]))
            return
        if action == "send" and len(parts) > 1 and parts[1].isdigit():
            self.submit_application(int(parts[1]))
            return
        if action in {"favorite", "ignore"} and len(parts) > 1 and parts[1].isdigit():
            self.store().set_job_status(int(parts[1]), "favorite" if action == "favorite" else "ignored")
            self.notice = f"Vacancy #{parts[1]} marked {action}."
            return
        if action == "clarify" and len(parts) == 3 and parts[1].isdigit():
            payload = parts[2].split(maxsplit=1)
            if len(payload) == 2 and payload[0] in {"profile", "vacancy"}:
                record = self.store().answer_clarification(int(parts[1]), payload[1], payload[0])
                if payload[0] == "profile":
                    save_profile_answer(record.kind, payload[1], record.question)
                self.notice = f"Clarification #{parts[1]} saved for {payload[0]}."
                return
        if action == "resolve" and len(parts) > 1 and parts[1].isdigit():
            ready, open_items = self.store().resolve_clarifications(int(parts[1]))
            self.notice = f"Vacancy #{parts[1]} is ready." if ready else f"Still open: {', '.join(str(x.id) for x in open_items)}"
            return
        if action == "reply" and len(parts) > 1 and parts[1].isdigit():
            self.prepare_reply(int(parts[1]))
            return
        if action == "reply-send" and len(parts) > 1 and parts[1].isdigit():
            self.send_reply(int(parts[1]))
            return
        if action == "queue" and len(parts) > 1 and parts[1] in {"scan", "messages", "statuses"}:
            self.queue_work(parts[1])
            return
        self.notice = "Unknown: scan [SITE] · models refresh · model ROLE PROVIDER/MODEL · motion light|heavy · agent message|job ID · safe"

    def set_motion(self, motion: str) -> None:
        if motion not in {"light", "heavy"}:
            self.notice = "Motion must be light or heavy."
            return
        save_tui_motion(motion)
        self.motion = motion
        self.clock.motion = motion
        self.visual_index = 0 if motion == "light" else 1
        self.notice = f"Motion set to {motion.upper()}."
        self.success_until = time.monotonic() + 1.2

    def insert_application_text(self, value: str) -> None:
        self.application_buffer = (
            self.application_buffer[: self.application_cursor] + value + self.application_buffer[self.application_cursor :]
        )
        self.application_cursor += len(value)

    def open_application_editor(self, application_id: int) -> None:
        try:
            item = self.store().get_application(application_id)
        except ValueError as error:
            self.notice = str(error)
            return
        self.application_original = item.final_text or item.draft
        self.application_buffer = self.application_original
        self.application_cursor = len(self.application_buffer)
        self.application_editing = True
        self.notice = "Editing local application text. Ctrl+S saves locally; Esc cancels."

    def save_application_edit(self) -> None:
        if not self.selected_application_id:
            self.notice = "No application selected."
            return
        try:
            item = self.store().get_application(self.selected_application_id)
            self.store().save_draft(item.job_id, item.site, self.application_buffer)
            self.store().set_application_final_text(item.id, self.application_buffer)
        except Exception as error:
            self.record_ui_error("save application draft", error)
            self.notice = f"Application draft was not saved: {type(error).__name__}: {error}"
            return
        self.application_editing = False
        self.application_original = self.application_buffer
        self.notice = "DRAFT SAVED LOCALLY — NOT SENT. Press S only in ARMED to submit through the site."

    def insert_prompt_text(self, value: str) -> None:
        self.prompt_buffer = self.prompt_buffer[: self.prompt_cursor] + value + self.prompt_buffer[self.prompt_cursor :]
        self.prompt_cursor += len(value)

    def create_draft(self, job_id: int, *, show_applications: bool = True) -> None:
        try:
            job, analysis = self.store().get_job(job_id)
            if not analysis:
                raise ValueError("Analyze the vacancy first.")
            raw = RawJobDetails(
                external_job_id=job.external_job_id,
                site=job.site,
                url=job.url,
                title=job.title,
                description=job.normalized_text.removeprefix("[normalized]\n") or job.description,
            )
            self.store().save_draft(job.id, job.site, build_draft(raw, analysis))
            if show_applications:
                self.view = "applications"
                self.notice = f"Draft for vacancy #{job_id} saved locally."
            else:
                self.notice = f"DRAFT REGENERATED LOCALLY for vacancy #{job_id} — NOT SENT. Review or edit it before submitting."
        except Exception as error:
            self.record_ui_error(f"create vacancy draft #{job_id}", error)
            self.notice = f"Draft was not created: {type(error).__name__}: {error}"

    def regenerate_application(self, job_id: int) -> None:
        """Rewrite a draft with the configured writing model, never send it.

        Re-running the local template was misleading because it commonly
        produced byte-for-byte identical text. Regeneration is deliberately a
        writing-role operation, so a prompt override is actually respected.
        """
        if f"regenerate-{job_id}" in self.busy_work:
            self.notice = "REGENERATING — writing model is still working on this draft."
            return
        self.notice = "REGENERATING — asking the configured writing model for a new local draft…"

        def work() -> str:
            job, analysis = self.store().get_job(job_id)
            if not analysis:
                raise ValueError("Analyze the vacancy first.")
            writer = provider_for_role(get_settings(), "writing")
            if writer is None:
                raise ValueError("No writing model is available. Configure one in Settings → Models & Roles; the existing draft was kept unchanged.")
            raw = RawJobDetails(
                external_job_id=job.external_job_id,
                site=job.site,
                url=job.url,
                title=job.title,
                company=job.company,
                budget=job.budget,
                work_format=job.work_format,
                published_at=job.published_at,
                description=job.normalized_text.removeprefix("[normalized]\n") or job.description,
                normalized_text=job.normalized_text,
            )
            text = asyncio.run(build_draft_with_provider(writer, raw, analysis, load_profile()))
            if not text:
                raise ValueError("Writing model returned an empty draft; the existing text was kept unchanged.")
            self.store().save_draft(job.id, job.site, text)
            application = self.store().get_application_for_job(job.id)
            self.store().set_application_final_text(application.id, text)
            used = f"{getattr(writer, 'provider_id', 'writing')}/{getattr(writer, 'model', 'default') or 'default'}"
            return f"DRAFT REGENERATED LOCALLY with {used} — NOT SENT. Review, edit with E, or submit with S in ARMED."

        self.start_background(f"regenerate-{job_id}", work)

    def queue_work(self, kind: str) -> None:
        settings = get_settings()
        worker = HarnessWorker(self.store(), settings)
        for site, config in settings.sites.items():
            if not config.enabled:
                continue
            if kind == "scan":
                worker.enqueue_scan(site)
            elif kind == "messages":
                worker.enqueue_message_check(site)
            else:
                worker.enqueue_status_check(site)
        self.view = "queue"
        self.notice = f"Queued {kind} checks for enabled sites."

    def scan_now(self, requested_site: str | None) -> None:
        """Run a fresh vacancy scan now, without waiting for the scheduler."""
        settings = get_settings()
        if requested_site and requested_site not in settings.sites:
            self.notice = f"Unknown site: {requested_site}."
            return
        sites = [requested_site] if requested_site else [
            name for name, config in settings.sites.items() if config.enabled and config.automation.get("scan_enabled", True)
        ]
        if not sites:
            self.notice = "No enabled sites have vacancy scanning enabled."
            return
        self.notice = f"Scanning {', '.join(sites)} now…"
        self.view = "vacancies"
        self.start_background("scan", lambda: " · ".join(f"{site}: {detail}" for site, detail in asyncio.run(self._scan_now(sites))))

    def run_agent(self, subject_type: str, subject_id: int) -> None:
        self.notice = f"Agent is planning {subject_type} #{subject_id}…"
        self.view = "messages" if subject_type == "message" else "vacancies"

        def work() -> str:
            service = OrchestrationService(
                self.store(), get_settings(), mode="armed" if self.armed else "safe", external_executor=self.execute_agent_external
            )
            return asyncio.run(service.handle_message(subject_id) if subject_type == "message" else service.handle_job(subject_id))

        self.start_background(f"agent-{subject_type}-{subject_id}", work)

    def send_chat(self) -> None:
        text = self.chat_input.strip()
        if not text:
            self.notice = "Write a message first."
            return
        if text not in self.chat_input_history:
            self.chat_input_history.append(text)
        self.chat_history_index = None
        self.chat_input = ""
        self.chat_cursor = 0
        self.notice = "Agent is thinking…"
        self.chat_thinking = True
        self.chat_pulse = True
        self.chat_stream_response = ""
        self.busy_work.add("chat")

        def work() -> None:
            try:
                service = OrchestrationService(
                    self.store(), get_settings(), mode="armed" if self.armed else "safe", external_executor=self.execute_agent_external
                )
                session_id, reply = asyncio.run(service.handle_chat(text, self.chat_session_id, self.on_chat_delta))
                self.events.put(UiEvent("chat_done", payload=(session_id, reply)))
            except Exception as error:
                self.events.put(UiEvent("work_failed", "chat", f"{type(error).__name__}: {error}"))

        threading.Thread(target=work, name="job-harness-chat", daemon=True).start()

    def queue_or_send_chat(self) -> None:
        if self.chat_thinking:
            queued = self.chat_input.strip()
            if not queued:
                self.notice = "Agent is working; write a follow-up to queue it."
                return
            self.chat_queued_input = queued
            self.chat_input = ""
            self.chat_cursor = 0
            self.notice = "QUEUED · the follow-up will run after this turn."
            return
        self.send_chat()

    def recall_chat_input(self, offset: int) -> None:
        if not self.chat_input_history:
            self.notice = "No composer history yet."
            return
        if self.chat_history_index is None:
            self.chat_history_index = len(self.chat_input_history)
        self.chat_history_index = max(0, min(len(self.chat_input_history) - 1, self.chat_history_index + offset))
        self.chat_input = self.chat_input_history[self.chat_history_index]
        self.chat_cursor = len(self.chat_input)

    def insert_chat_text(self, value: str) -> None:
        self.chat_input = self.chat_input[: self.chat_cursor] + value + self.chat_input[self.chat_cursor :]
        self.chat_cursor += len(value)

    async def on_chat_delta(self, reply: str) -> None:
        """Queue streaming text for the primary terminal thread."""
        self.events.put(UiEvent("chat_delta", reply))

    def refresh_registry(self) -> None:
        """Probe configured providers off the terminal thread."""
        if self.registry_loading:
            self.notice = "Provider discovery is already running."
            return
        self.registry_loading = True
        self.busy_work.add("registry")
        self.notice = "Discovering configured providers…"

        def work() -> None:
            try:
                self.events.put(UiEvent("registry_done", payload=asyncio.run(discover_registry(get_settings()))))
            except Exception as error:
                self.events.put(UiEvent("work_failed", "registry", f"{type(error).__name__}: {error}"))

        threading.Thread(target=work, name="job-harness-registry", daemon=True).start()

    def start_background(self, name: str, work: Callable[[], str]) -> None:
        """Run slow local/browser work while the terminal remains responsive."""
        if name in self.busy_work:
            self.notice = f"{name} is already running."
            return
        self.busy_work.add(name)

        def runner() -> None:
            try:
                self.events.put(UiEvent("work_done", name, work()))
            except Exception as error:
                self.events.put(UiEvent("work_failed", name, f"{type(error).__name__}: {error}"))

        threading.Thread(target=runner, name=f"job-harness-{name}", daemon=True).start()

    async def execute_agent_external(self, call: ToolCall) -> str:
        """Bind approved agent tools to the same confirmed adapter paths as TUI commands."""
        if call.name == "scan_site":
            site = str(call.args.get("site", ""))
            if not site:
                raise ValueError("scan_site requires site")
            result = await self._scan_now([site])
            return " · ".join(f"{name}: {detail}" for name, detail in result)
        if call.name == "check_messages":
            site = str(call.args.get("site", ""))
            if not site:
                raise ValueError("check_messages requires site")
            return await HarnessWorker(self.store(), get_settings()).run_now("messages", site)
        if call.name == "submit_application":
            job_id = int(call.args["id"])
            await self._submit_application(job_id)
            return self.notice
        if call.name == "send_internal_message":
            message_id = int(call.args["id"])
            return await self._send_reply(message_id)
        if call.name == "sync_statuses":
            site = str(call.args.get("site", ""))
            if not site:
                raise ValueError("sync_statuses requires site")
            return await HarnessWorker(self.store(), get_settings()).run_now("statuses", site)
        raise ValueError(f"Unsupported external agent tool: {call.name}")

    async def _scan_now(self, sites: list[str]) -> list[tuple[str, str]]:
        worker = HarnessWorker(self.store(), get_settings())
        results: list[tuple[str, str]] = []
        for site in sites:
            results.append((site, await worker.run_now("scan", site)))
        return results

    def submit_application(self, job_id: int) -> None:
        if not self.armed:
            self.create_draft(job_id)
            self.notice = "SAFE: draft saved; no site action was attempted."
            return
        task_name = f"submit-application-{job_id}"
        if task_name in self.busy_work:
            self.notice = "SUBMITTING — the site form is still open; waiting for its confirmation."
            return
        self.submitting_application_id = job_id
        self.notice = "SUBMITTING — opening the site form in the background; the interface remains usable."
        timeout = get_settings().limits.remote_task_timeout_seconds
        self.start_background(
            task_name,
            lambda: asyncio.run(asyncio.wait_for(self._submit_application(job_id), timeout=timeout)),
        )

    async def _submit_application(self, job_id: int) -> str:
        job, analysis = self.store().get_job(job_id)
        if not analysis:
            raise ValueError("Analyze the vacancy first.")
        application = self.store().get_application_for_job(job_id)
        settings = get_settings()
        config = settings.sites[job.site]
        adapter = build_adapter(
            config.adapter,
            BrowserManager(
                settings.env.headless,
                settings.env.browser_min_action_delay_seconds,
                settings.env.browser_max_action_delay_seconds,
            ),
            config.browser_profile,
        )
        body = application.final_text or application.draft
        offer_kwargs: dict[str, str] = {}
        if job.site == "kwork":
            profile = load_profile()
            writer = provider_for_role(settings, "writing")
            raw = RawJobDetails(
                external_job_id=job.external_job_id,
                site=job.site,
                url=job.url,
                title=job.title,
                company=job.company,
                budget=job.budget,
                work_format=job.work_format,
                published_at=job.published_at,
                description=job.normalized_text.removeprefix("[normalized]\n") or job.description,
                normalized_text=job.normalized_text,
            )
            offer = await build_kwork_offer_with_provider(writer, raw, analysis, profile, body) if writer else build_kwork_offer(raw, analysis, profile, body)
            body = offer.body
            offer_kwargs = {"title": offer.title, "price": offer.price, "duration": offer.duration}
            self.store().set_offer_details(application.id, offer.title, offer.price, offer.duration)
        prepared = PreparedApplication(
            job_id=job.id,
            external_job_id=job.external_job_id,
            site=job.site,
            body=body,
            title=offer_kwargs.get("title", job.title[:100]),
            price=offer_kwargs.get("price"),
            duration=offer_kwargs.get("duration"),
        )
        result = await ApplicationService(
            self.store(), adapter, settings.limits.applications_per_day, settings.applications.auto_apply_threshold
        ).submit(prepared, analysis, dry_run=False, manual=True)
        recovery_detail = ""
        if not result.confirmed and is_recoverable_adapter_error(result.detail):
            recovery_model = provider_for_role(settings, "recovery") or provider_for_role(settings, "writing")
            if recovery_model:
                fixed, artifact, recovery_detail = await apply_verified_adapter_recovery(
                    recovery_model, adapter, job.site, result.detail, test_target=adapter_test_target(job.site)
                )
                self.store().save_llm_usage(
                    role="recovery", action="repair_adapter", provider=str(getattr(recovery_model, "provider_id", "unknown")),
                    model=getattr(recovery_model, "model", None), subject_type="job", subject_id=job.id,
                    total_tokens=None, cost_usd=None, result="completed" if fixed else "failed",
                )
                recovery_detail = f" · recovery {'activated' if fixed else 'not applied'}: {artifact} · {recovery_detail}"
            else:
                recovery_detail = " · recovery skipped: no recovery model configured"
        if job.site == "kwork" and "project is no longer active" in result.detail:
            # Kwork exposes a definitive project state before the form opens.
            # Keep the draft for auditability, but remove the closed order from
            # the active vacancy workflow.
            self.store().set_job_status(job.id, "expired")
        # This runs in a worker thread. Returning the terminal state lets the
        # main UI present the actual site result instead of the misleading
        # generic ``Completed.`` message. A draft remains a draft unless the
        # adapter observed the platform's explicit confirmation.
        detail = result.detail if result.confirmed else f"NOT SENT: {result.detail}"
        if result.screenshot_path:
            detail += f" · diagnostic: {result.screenshot_path}"
        return detail + recovery_detail

    def prepare_reply(self, message_id: int) -> None:
        try:
            # Preparing a draft is local-only, so no site adapter is needed.
            reply = MessageService(self.store(), None).prepare_reply(message_id, load_profile())
            self.notice = f"DRAFT READY — NOT SENT ({reply.status}). Press S only in ARMED to attempt delivery."
        except Exception as error:
            self.record_ui_error(f"create reply draft #{message_id}", error)
            self.notice = f"Reply draft was not created: {type(error).__name__}: {error}"

    def send_reply(self, message_id: int) -> None:
        if not self.armed:
            self.prepare_reply(message_id)
            self.notice = "SAFE: reply draft saved; no site action was attempted."
            return
        task_name = f"send-reply-{message_id}"
        if task_name in self.busy_work:
            self.notice = "SENDING — the site confirmation is still pending."
            return
        self.sending_message_id = message_id
        self.notice = "SENDING — opening the internal site chat and awaiting site confirmation…"
        self.start_background(task_name, lambda: asyncio.run(self._send_reply(message_id)))

    async def _send_reply(self, message_id: int) -> str:
        message = self.store().get_message(message_id)
        settings = get_settings()
        config = settings.sites[message.site]
        adapter = build_adapter(
            config.adapter,
            BrowserManager(
                settings.env.headless,
                settings.env.browser_min_action_delay_seconds,
                settings.env.browser_max_action_delay_seconds,
            ),
            config.browser_profile,
        )
        result = await MessageService(self.store(), adapter).send_prepared_reply(message_id, confirm=True)
        return result.detail if result.confirmed else f"NOT SENT — {result.detail}"

    def flow_color(self, offset: int = 0) -> str:
        """Amethyst/cyan foreground flow; background stays owned by Ghostty."""
        palette = (PURPLE, VIOLET, LILAC, CYAN, CYAN_BRIGHT, CYAN)
        return palette[(self.clock.frame + offset) % len(palette)] if self.motion == "heavy" else PURPLE

    def spinner(self, offset: int = 0) -> str:
        return SPINNER[(self.clock.frame + offset) % len(SPINNER)]

    def chip(self, label: str, color: str, *, active: bool = False) -> str:
        edge = self.flow_color(len(label)) if active else PURPLE
        body = self.flow_color(2) if active and self.motion == "heavy" else color
        return f"{edge}╭{RESET}{body} {label} {RESET}{edge}╮{RESET}"

    def selection(self, selected: bool) -> str:
        if not selected:
            return "  "
        marker = self.spinner() if self.motion == "heavy" else "❯"
        return f"{self.flow_color()}{marker}›{RESET}"

    def status(self, value: str) -> str:
        normalized = value.upper()
        if "FAIL" in normalized or "ERROR" in normalized:
            return f"{RED}{normalized}{RESET}"
        if "RUN" in normalized or "PENDING" in normalized:
            return f"{CYAN}{self.spinner()} {normalized}{RESET}"
        if normalized in {"SENT", "COMPLETED", "DONE"} and time.monotonic() < self.success_until:
            return f"{CYAN_BRIGHT}{BOLD}{normalized}{RESET}"
        if "CLARIFICATION" in normalized:
            return f"{YELLOW}{normalized}{RESET}"
        return normalized

    def panel(self, title: str, body: list[str], max_rows: int, width: int, color: str = PURPLE, *, active: bool = False, prewrapped: bool = False) -> list[str]:
        inner_width = max(24, width - 6)
        edge = self.flow_color(len(title)) if active else color
        border = "─" * max(4, inner_width - len(title) - 1)
        output = [f"{edge}╭─ {BOLD}{title}{RESET}{edge} {border}╮{RESET}"]
        expanded = list(body) if prewrapped else []
        if not prewrapped:
            for row in body:
                expanded.extend(self.wrap_ansi(row, inner_width))
        visible = expanded[-max_rows:] or [f"{DIM}—{RESET}"]
        for row in visible:
            row = self.clip_ansi(row, inner_width)
            plain_length = self.visible_width(row)
            output.append(f"{edge}│{RESET} {row}{' ' * max(0, inner_width - plain_length)} {edge}│{RESET}")
        output.append(f"{edge}╰{'─' * (inner_width + 2)}╯{RESET}")
        return output

    def render(self) -> None:
        # ED / CUP only redraw the primary viewport. No background colour or
        # alternate-buffer escape sequence is ever sent.
        width, height = shutil.get_terminal_size((100, 32))
        lines = self.lines(width, height)
        # In a primary terminal buffer LF alone only moves down; it does not
        # necessarily reset the column. CRLF keeps every redraw row anchored
        # at the left edge (the behaviour expected by OMP-style renderers).
        viewport = max(3, height - 1)
        fitted = [self.clip_ansi(line, width) for line in lines[:viewport]]
        # Fill the rest of the viewport. This prevents the terminal's cursor
        # from crossing the lower margin and scrolling the fixed menu away.
        fitted.extend([""] * max(0, viewport - len(fitted)))
        content = "\r\n".join(fitted)
        # Reset before ED: terminal erase uses the *current* background. This
        # prevents a background style left by a prior app (for example OMP)
        # from being painted across the Job Harness viewport.
        self.write(f"{RESET}\x1b[H\x1b[2J{content}{RESET}\x1b[J")

    def lines(self, width: int, height: int | None = None) -> list[str]:
        self._render_height = height or shutil.get_terminal_size((100, 32)).lines
        if self.launcher_open:
            return self.render_launcher(width)
        mode = "ARM READY" if self.arm_pending else "ARMED" if self.armed else "SAFE"
        signal = f"{self.flow_color()}◈ SIGNAL FLOW {self.spinner()} // {self.motion.upper()} MOTION{RESET}" if self.motion == "heavy" else f"{DIM}MOTION LIGHT{RESET}"
        mode_color = YELLOW if self.armed or self.arm_pending else CYAN
        lines = [
            f"{self.flow_color()}{BOLD}JOB HARNESS // OPERATOR STATION{RESET}  {signal}",
            f"{mode_color}{mode}{RESET}  {DIM}Tab menu · ⇧Tab mode · / commands · Ctrl+Q exit{RESET}",
            "",
        ]
        renderer = getattr(self, f"render_{self.view}")
        lines.extend(renderer(width))
        lines.extend(["", f"{DIM}{self.notice}{RESET}"])
        if self.command_mode:
            lines.append(f"{PURPLE}{BOLD}COMMAND{RESET} > {self.command}{CYAN}█{RESET}")
        return lines

    def render_launcher(self, width: int) -> list[str]:
        """A discoverable start menu, not a memorisation test of shortcuts."""
        descriptions = {
            "dashboard": "Today’s funnel, new work and unread messages.",
            "profile": "Candidate facts, skills, portfolio and application preferences.",
            "vacancies": "Browse, filter, sort and prepare application drafts.",
            "applications": "Review drafts and confirmed application statuses.",
            "messages": "Read employer chats, draft replies and confirm delivery.",
            "clarifications": "Answer facts needed before an application can continue.",
            "queue": "See active scans, retries and recovery artifacts.",
            "scheduler": "Inspect the recurring job-search schedule.",
            "settings": "Providers, models, prompts, automation and visuals.",
            "agent_activity": "Inspect each tool decision and result.",
            "chat": "Ask the agent to plan and operate the harness.",
        }
        mode = "ARMED" if self.armed else "SAFE"
        rows = [
            f"{self.selection(index == self.menu_index)}{BOLD if index == self.menu_index else ''}{name.upper():<18}{RESET} {DIM}{descriptions[name]}{RESET}"
            for index, name in enumerate(MENU_VIEWS)
        ]
        body = [
            f"{CYAN}{BOLD}JOB HARNESS // START MENU{RESET}",
            f"Mode: {self.status(mode)}  {DIM}External actions stay blocked in SAFE.{RESET}",
            "",
            *rows,
            "",
            f"{DIM}↑/↓ choose · Enter open · Esc close · Ctrl+Q exit{RESET}",
        ]
        max_rows = max(8, getattr(self, "_render_height", 32) - 6)
        return self.panel("WORKSPACES", body, max_rows, width, active=True, prewrapped=True) + ["", f"{DIM}{self.notice}{RESET}"]

    @staticmethod
    def wrap(value: str, width: int) -> list[str]:
        # Never pass embedded newlines to the terminal as part of one logical
        # row: primary-buffer rendering would then lose its CR anchor and the
        # rest of the detail view appears scattered across the screen.
        rows: list[str] = []
        for paragraph in value.splitlines() or [""]:
            rows.extend(textwrap.wrap(paragraph, max(20, width), replace_whitespace=False) or [""])
        return rows

    @staticmethod
    def visible_window(rows: list[Row], selected_id: int | None, get_id: Callable[[Row], int], limit: int) -> tuple[list[Row], int, int]:
        """Return a stable viewport which always contains the selected row."""
        if not rows:
            return [], 0, 0
        selected_index = next((index for index, row in enumerate(rows) if get_id(row) == selected_id), 0)
        size = max(1, min(limit, len(rows)))
        start = max(0, min(selected_index - size // 2, len(rows) - size))
        return rows[start : start + size], start, selected_index

    def render_dashboard(self, width: int) -> list[str]:
        stats = self.store().stats()
        usage = self.store().llm_usage_summary(datetime.now(UTC) - timedelta(hours=24))
        lines = [f"{BOLD}TODAY'S CONSOLE{RESET}", ""] + [f"{name:<22} {CYAN}{value}{RESET}" for name, value in (("Found", stats["jobs"]), ("Analyzed", stats["analyzed"]), ("High match", stats["score_85"]), ("Drafts", stats["drafts"]), ("Submitted", stats["submitted"]), ("Needs clarification", stats["needs_clarification"]), ("Unread", stats["unread_messages"]))]
        lines.extend(["", f"{BOLD}LLM USAGE · 24H{RESET}"])
        if not usage:
            lines.append(f"{DIM}No recorded calls yet.{RESET}")
        for item in usage[:5]:
            tokens = str(item["tokens"]) if item["reported_calls"] else "not reported"
            lines.append(f"{item['provider']}/{item['model']:<22} {item['action']:<24} {tokens} tokens · ${float(item['cost_usd']):.4f}")
        return lines

    def render_profile(self, width: int) -> list[str]:
        """A readable map of user-owned candidate memory, not a hidden prompt."""
        try:
            profile = load_profile()
            candidate = profile.get("candidate", {}) if isinstance(profile.get("candidate"), dict) else {}
        except (FileNotFoundError, OSError, yaml.YAMLError) as error:
            return [f"{RED}PROFILE UNAVAILABLE{RESET}", str(error)]
        name = str(candidate.get("name", "Candidate"))
        lines = [
            f"{BOLD}PROFILE & CONTEXT{RESET}",
            f"{DIM}User-owned data at ~/.job-harness/profile.yaml. This is passed to analysis, writing, forms and the agent as needed.{RESET}",
            f"{CYAN}{name}{RESET}  {DIM}↑/↓ choose section · Enter open · [E] edit after opening{RESET}",
            "",
        ]
        for index, (key, label, description) in enumerate(PROFILE_SECTIONS):
            value = self.profile_section_value(profile, key)
            if key == "target_roles" and isinstance(value, list):
                summary = ", ".join(str(item) for item in value[:4]) or "not set"
            elif key == "experience" and isinstance(value, dict):
                projects = value.get("projects", [])
                summary = f"{len(projects)} project(s)" if isinstance(projects, list) else "configured"
            elif key == "skill_levels" and isinstance(value, dict):
                summary = ", ".join(str(section) for section in value) or "not set"
            elif isinstance(value, dict):
                summary = ", ".join(str(field) for field in value) or "not set"
            else:
                summary = str(value)[:70] if value else "not set"
            lines.append(f"{self.selection(index == self.profile_section_index)}{BOLD if index == self.profile_section_index else ''}{label:<24}{RESET} {summary}")
            lines.append(f"  {DIM}{description}{RESET}")
        return lines

    def render_profile_detail(self, width: int) -> list[str]:
        key, label, description = self.selected_profile_section()
        if self.profile_editing:
            before, after = self.profile_buffer[: self.profile_cursor], self.profile_buffer[self.profile_cursor :]
            editor = before + f"{CYAN}▍{RESET}" + after
            return [
                f"{BOLD}PROFILE EDITOR // {label.upper()}{RESET}",
                f"{YELLOW}User data only — Ctrl+S save · Esc cancel · arrows move cursor · Enter newline{RESET}",
                "",
                *self.wrap(editor, width),
            ]
        try:
            value = self.profile_section_value()
            detail = yaml.safe_dump(value, allow_unicode=True, sort_keys=False).rstrip() or "# not set"
        except (FileNotFoundError, OSError, yaml.YAMLError) as error:
            return [f"{RED}PROFILE UNAVAILABLE{RESET}", str(error)]
        return [
            f"{BOLD}PROFILE // {label.upper()}{RESET}",
            f"{DIM}{description}{RESET}",
            "",
            *self.wrap(detail, width),
            "",
            f"{CYAN}[E]{RESET} edit this section  {DIM}Esc back{RESET}",
        ]

    def render_vacancies(self, width: int) -> list[str]:
        filters = f"site={self.job_site_filter or 'all'} · status={self.job_status_filter or 'all'} · min={self.job_min_score} · sort={self.job_sort}"
        controls = f"{CYAN}[O]{RESET} sort:{self.job_sort}  {CYAN}[F]{RESET} min:{self.job_min_score}  {CYAN}[P]{RESET} site:{self.job_site_filter or 'all'}  {CYAN}[T]{RESET} status:{self.job_status_filter or 'all'}  {DIM}[X] clear{RESET}"
        lines = [f"{BOLD}VACANCIES{RESET}", f"{DIM}{filters}{RESET}", controls, f"{DIM}↑/↓ select · Enter open · filters cycle immediately{RESET}", ""]
        rows = self.filtered_jobs()
        visible, start, selected_index = self.visible_window(
            rows, self.selected_job_id, lambda row: row[0].id, shutil.get_terminal_size((100, 32)).lines - 9
        )
        if rows:
            lines.append(f"{DIM}{selected_index + 1}/{len(rows)} · showing {start + 1}–{start + len(visible)}{RESET}")
        for job, analysis in visible:
            score = analysis.score if analysis else 0
            selected = self.selection(job.id == self.selected_job_id)
            lines.append(f"{selected}#{job.id:<3} {job.site:<7} {score:>3}  {self.status(job.status):<20} {job.discovered_at:%d.%m %H:%M} {job.title[:max(18, width - 60)]}")
        return lines if rows else lines + [f"{DIM}No vacancies collected yet.{RESET}"]

    def filtered_jobs(self):  # type: ignore[no-untyped-def]
        rows = self.store().list_jobs(min_score=self.job_min_score, site=self.job_site_filter, status=self.job_status_filter)
        if self.job_sort == "fresh":
            return sorted(rows, key=lambda row: row[0].discovered_at, reverse=True)
        if self.job_sort == "analyzed":
            return sorted(rows, key=lambda row: row[1].created_at if row[1] else row[0].discovered_at, reverse=True)
        if self.job_sort == "status":
            return sorted(rows, key=lambda row: (row[0].status, row[0].discovered_at), reverse=True)
        if self.job_sort == "site":
            return sorted(rows, key=lambda row: (row[0].site, row[0].discovered_at))
        return sorted(rows, key=lambda row: ((row[1].score if row[1] else 0), row[0].discovered_at), reverse=True)

    def render_vacancy_detail(self, width: int) -> list[str]:
        if not self.selected_job_id:
            return ["No vacancy selected."]
        try:
            job, analysis = self.store().get_job(self.selected_job_id)
        except ValueError:
            return [f"{RED}Vacancy no longer exists.{RESET}"]
        matched = ", ".join(analysis.matched_skills) if analysis else "—"
        missing = ", ".join(analysis.missing_skills) if analysis else "—"
        description = job.normalized_text.removeprefix("[normalized]\n") if job.normalized_text.startswith("[normalized]\n") else job.description
        if job.site == "kwork" and not job.normalized_text.startswith("[normalized]\n"):
            description = KworkAdapter.extract_project_description(job.description, job.title)
        try:
            application = self.store().get_application_for_job(job.id)
            application_line = f"{CYAN}APPLICATION{RESET} #{application.id} · {self.status(application.status)} · {'sent confirmed' if application.status == 'submitted' else 'local draft'}"
        except ValueError:
            application_line = f"{DIM}APPLICATION  none yet{RESET}"
        actions = f"{CYAN}[D]{RESET} draft/edit  {CYAN}[F]{RESET} favorite  {CYAN}[I]{RESET} ignore  {CYAN}[R]{RESET} re-analyze  {DIM}Esc back{RESET}"
        record = self.store().get_analysis_record(job.id)
        meta = f"found: {job.discovered_at:%d.%m %H:%M} · analyzed: {record.created_at:%d.%m %H:%M} · model: {record.model}" if record else f"found: {job.discovered_at:%d.%m %H:%M} · not analyzed"
        return [f"{BOLD}VACANCY DETAIL{RESET}", "", f"{BOLD}{job.title}{RESET}", f"{job.company or 'Company unknown'} · {job.site}", job.url, f"Status: {job.status} · score: {analysis.match_score if analysis else '—'} · budget: {job.budget or '—'}", f"{DIM}{meta}{RESET}", application_line, f"{CYAN}Matched:{RESET} {matched}", f"{YELLOW}To learn:{RESET} {missing}", "", f"{BOLD}DESCRIPTION{RESET}", *self.wrap(description, width), "", actions]

    def render_applications(self, width: int) -> list[str]:
        rows = self.store().list_applications()
        lines = [f"{BOLD}APPLICATIONS{RESET}", f"{DIM}↑/↓ select · Enter open · each draft can be edited or submitted from its detail screen.{RESET}", ""]
        visible, start, selected_index = self.visible_window(
            rows, self.selected_application_id, lambda row: row.id, shutil.get_terminal_size((100, 32)).lines - 8
        )
        if rows:
            lines.append(f"{DIM}{selected_index + 1}/{len(rows)} · showing {start + 1}–{start + len(visible)}{RESET}")
        for item in visible:
            selected = self.selection(item.id == self.selected_application_id)
            lines.append(f"{selected}#{item.id:<3} job #{item.job_id:<3} {item.site:<7} {self.status(item.status):<22} {item.created_at:%d.%m %H:%M}")
        return lines if rows else lines + [f"{DIM}No drafts yet. In vacancies run: draft JOB_ID{RESET}"]

    def render_application_detail(self, width: int) -> list[str]:
        if not self.selected_application_id:
            return ["No application selected."]
        try:
            item = self.store().get_application(self.selected_application_id)
        except ValueError:
            return [f"{RED}Application no longer exists.{RESET}"]
        delivery = (
            f"{CYAN}{BOLD}DELIVERY: SENT — SITE CONFIRMED{RESET}"
            if item.status == "submitted"
            else f"{YELLOW}{BOLD}DELIVERY: NOT SENT — LOCAL DRAFT ONLY{RESET}"
        )
        offer_details = (
            f"{CYAN}KWORK OFFER{RESET}  {item.offer_price or '—'} ₽ · {item.offer_duration or '—'} days · {item.offer_title or 'will be proposed on send'}"
            if item.site == "kwork"
            else ""
        )
        review = self.store().latest_application_review(item.id)
        review_lines = (
            [
                f"{BOLD}APPLICATION REVIEW{RESET}  {review.score}/100 · {'approved' if review.approved else 'rewrite needed'} · v{review.version}",
                f"{DIM}{review.provider}/{review.model or 'default'} · {review.created_at:%d.%m %H:%M}{RESET}",
                *self.wrap("; ".join(json.loads(review.reasons_json)) or review.rewrite_notes, width),
            ]
            if review else [f"{DIM}APPLICATION REVIEW  not run yet{RESET}"]
        )
        submitting = f"{YELLOW}{self.spinner()} SUBMITTING — waiting for explicit site confirmation; Esc still returns safely.{RESET}" if self.submitting_application_id == item.job_id else ""
        regenerating = f"{YELLOW}{self.spinner()} REGENERATING WITH WRITING MODEL — existing draft remains visible until the new one is saved.{RESET}" if f"regenerate-{item.job_id}" in self.busy_work else ""
        actions = f"{CYAN}[E]{RESET} edit  {CYAN}[R]{RESET} regenerate  {YELLOW}[S]{RESET} submit in ARMED  {DIM}Esc back{RESET}"
        if self.application_editing:
            before, after = self.application_buffer[: self.application_cursor], self.application_buffer[self.application_cursor :]
            editor = before + f"{CYAN}▍{RESET}" + after
            return [
                f"{BOLD}APPLICATION EDITOR{RESET}", "", f"Job #{item.job_id} · {item.site} · {item.status}", delivery, "",
                f"{YELLOW}{BOLD}LOCAL EDIT — Ctrl+S save · Esc cancel · arrows move cursor · Enter newline{RESET}",
                *self.wrap(editor, width),
            ]
        return [
            f"{BOLD}APPLICATION DETAIL{RESET}", "", f"Job #{item.job_id} · {item.site} · {item.status}", delivery, offer_details, regenerating, submitting, "",
            *review_lines, "", f"{BOLD}TEXT{RESET}", *self.wrap(item.final_text or item.draft, width), "", actions,
        ]

    def render_messages(self, width: int) -> list[str]:
        rows = self.filtered_messages()
        unread = sum(item.is_unread for item in rows)
        lines = [f"{BOLD}MESSAGES{RESET}  {DIM}{len(rows)} conversations · {unread} unread · filter={self.message_filter} · / messages filter all|action|drafts|clarifications|sent|errors{RESET}", ""]
        visible, start, selected_index = self.visible_window(
            rows, self.selected_message_id, lambda row: row.id, shutil.get_terminal_size((100, 32)).lines - 8
        )
        if rows:
            lines.append(f"{DIM}{selected_index + 1}/{len(rows)} · showing {start + 1}–{start + len(visible)}{RESET}")
        for item in visible:
            selected = self.selection(item.id == self.selected_message_id)
            state, next_step = self.message_state(item.id, item.is_unread)
            beacon = f"{self.flow_color()}◈{RESET}" if item.is_unread and (self.motion == "light" or self.clock.frame % 2 == 0) else " "
            preview = re.sub(r"\s+", " ", item.body).strip()
            lines.append(f"{selected}{beacon}#{item.id:<3} {item.site:<7} {self.status(state):<19} {item.sender[:16]:<16} {preview[:max(12, width - 52)]}")
        return lines if rows else lines + [f"{DIM}No messages collected.{RESET}"]

    def render_message_detail(self, width: int) -> list[str]:
        if not self.selected_message_id:
            return ["No message selected."]
        try:
            item = self.store().get_message(self.selected_message_id)
        except ValueError:
            return [f"{RED}Message no longer exists.{RESET}"]
        warning = [f"{YELLOW}Kwork: communicate only in Kwork internal chat; do not share external contacts.{RESET}"] if item.site == "kwork" else []
        events = self.store().list_agent_events("message", item.id, limit=8)
        try:
            reply = self.store().get_message_reply(item.id)
            confirmation = [f"{CYAN}Site confirmation:{RESET} {reply.confirmation_detail}"] if reply.confirmation_detail else []
            if self.sending_message_id == item.id:
                delivery = f"{YELLOW}{BOLD}DELIVERY: SENDING — AWAITING SITE CONFIRMATION{RESET}"
            elif reply.status == "sent":
                delivery = f"{CYAN}{BOLD}DELIVERY: SENT — SITE CONFIRMED{RESET}"
            elif reply.status == "draft":
                delivery = f"{YELLOW}{BOLD}DELIVERY: NOT SENT — LOCAL DRAFT ONLY{RESET}"
            elif reply.status == "needs_clarification":
                delivery = f"{YELLOW}{BOLD}DELIVERY: NOT SENT — CANDIDATE CLARIFICATION REQUIRED{RESET}"
            elif reply.status == "not_needed":
                delivery = f"{DIM}DELIVERY: NOT SENT — NO REPLY NEEDED{RESET}"
            else:
                delivery = f"{YELLOW}{BOLD}DELIVERY: NOT SENT — {reply.status.upper()}{RESET}"
            next_step = (
                f"{DIM}To send: enable ARMED with Shift+Tab then Enter, then press S. A sent state appears only after site confirmation.{RESET}"
                if reply.status == "draft" and self.sending_message_id != item.id
                else ""
            )
            reply_lines = ["", f"{BOLD}REPLY STATUS: {reply.status.upper()}{RESET}", delivery, f"{DIM}{reply.reason}{RESET}" if reply.reason else "", *self.wrap(reply.final_text or reply.draft, width), *confirmation, next_step]
        except ValueError:
            reply_lines = ["", f"{YELLOW}REPLY STATUS: NEW — NO DRAFT / NOT SENT{RESET}", f"{DIM}Press R to prepare a local reply draft. Nothing is sent by this action.{RESET}"]
        activity = ["", f"{BOLD}AGENT ACTIVITY{RESET}"] + [f"{event.created_at:%H:%M:%S} {event.event_type:<20} {event.tool_name or 'agent':<24} {event.detail[:max(10, width - 58)]}" for event in reversed(events)]
        actions = f"{CYAN}[R]{RESET} create/recreate local draft  {YELLOW}[S]{RESET} send now (ARMED only)  {CYAN}[G]{RESET} let agent assess  {DIM}Esc back{RESET}"
        return [f"{BOLD}MESSAGE DETAIL{RESET}", "", f"{item.sender} · {item.site} · {item.received_at:%d.%m %H:%M}", "", *warning, f"{BOLD}MESSAGE{RESET}", *self.wrap(item.body, width), *reply_lines, "", actions, *activity]

    def message_state(self, message_id: int, unread: bool) -> tuple[str, str]:
        try:
            reply = self.store().get_message_reply(message_id)
        except ValueError:
            return ("NEW" if unread else "NO DECISION", "agent message " + str(message_id))
        states = {
            "draft": ("DRAFT READY", "review / send in ARMED"),
            "needs_clarification": ("NEEDS CLARIFICATION", reply.reason or "candidate fact required"),
            "sent": ("SENT", "site confirmed"),
            "not_needed": ("NOT NEEDED", reply.reason or "no reply required"),
            "failed": ("FAILED", reply.reason or "retry agent"),
        }
        return states.get(reply.status, (reply.status.upper(), reply.reason or "review"))

    def filtered_messages(self):  # type: ignore[no-untyped-def]
        rows = self.store().list_messages()
        if self.message_filter == "all":
            return rows
        selected: list[object] = []
        wanted = {
            "drafts": {"DRAFT READY"},
            "clarifications": {"NEEDS CLARIFICATION"},
            "sent": {"SENT"},
            "errors": {"FAILED"},
            "action": {"NEW", "NO DECISION", "DRAFT READY", "NEEDS CLARIFICATION", "FAILED"},
        }[self.message_filter]
        for item in rows:
            state, _ = self.message_state(item.id, item.is_unread)
            if state in wanted:
                selected.append(item)
        return selected

    def render_clarifications(self, width: int) -> list[str]:
        rows = self.store().list_clarifications()
        lines = [f"{BOLD}NEEDS CLARIFICATION{RESET}", f"{DIM}/ clarify REQUEST_ID profile|vacancy ANSWER · resolve JOB_ID{RESET}", ""]
        lines.extend(f"#{item.id:<3} job #{item.job_id:<3} {item.site:<7} {item.kind:<14} {item.state:<9} {item.question[:max(20, width - 48)]}" for item in rows)
        return lines if rows else lines + [f"{CYAN}No open clarification requests.{RESET}"]

    def render_queue(self, width: int) -> list[str]:
        rows = self.store().list_tasks()
        lines = [
            f"{BOLD}AUTOPILOT QUEUE{RESET}",
            f"{DIM}Durable requests: QUEUED waits for a worker · RUNNING is being processed · COMPLETED has a recorded result · FAILED retries up to its limit.{RESET}",
            f"{CYAN}[1]{RESET} queue vacancy scans  {CYAN}[2]{RESET} queue message checks  {CYAN}[3]{RESET} queue status sync  {DIM}R refresh{RESET}",
            "",
        ]
        if not rows:
            return lines + [f"{CYAN}Queue is empty — no work is waiting. Use 1, 2 or 3 to create a durable task.{RESET}"]
        for item in rows:
            stripe = "▰▱▱▱" if item.status == "running" else "    "
            if item.status == "running" and self.motion == "heavy":
                stripe = "".join("▰" if (index + self.clock.frame) % 4 == 0 else "▱" for index in range(4))
            lines.append(f"#{item.id:<3} {item.kind:<10} {item.site or '—':<8} {self.status(item.status):<14} {stripe} {item.attempts}/{item.max_attempts} {item.last_error[:max(10, width - 52)]}")
        lines.extend(["", f"{DIM}Open Agent Activity for the tool-level transcript; Scheduler shows whether a background worker is enabled.{RESET}"])
        return lines

    def render_scheduler(self, width: int) -> list[str]:
        settings = get_settings()
        jobs = settings.scheduler.get("jobs", {})
        enabled = bool(settings.scheduler.get("enabled", False))
        runs = self.store().list_runs()[:5]
        lines = [
            f"{BOLD}SCHEDULER{RESET}",
            f"Background scheduler: {self.status('ENABLED' if enabled else 'DISABLED')}",
            f"{CYAN}[Space]{RESET} toggle scheduler  {CYAN}[↑/↓]{RESET} select task  {CYAN}[E]{RESET} enable/disable task  {CYAN}[+/-]{RESET} interval ±15 min",
            f"{DIM}Settings are saved to ~/.job-harness/config.yaml. The running scheduler reads them on its next cycle; manual queue actions work immediately.{RESET}",
            "",
            f"{BOLD}RECURRING TASKS{RESET}",
        ]
        if not jobs:
            lines.append(f"{DIM}No recurring jobs configured yet. Add them in config or Settings → Automation.{RESET}")
        for index, (name, item) in enumerate(jobs.items()):
            marker = self.selection(index == self.scheduler_index)
            lines.append(f"{marker}{name:<24} {'on' if item.get('enabled') else 'off':<3} · every {item.get('interval_minutes', '—')} min")
        lines.extend(["", f"{BOLD}RECENT RUNS{RESET}"])
        if not runs:
            lines.append(f"{DIM}No completed or failed runs recorded yet. Use Q to queue work, or wait for the scheduler.{RESET}")
        for run in runs:
            detail = (run.detail or "—").replace("\n", " ")
            lines.append(f"{run.started_at:%d.%m %H:%M} {run.kind:<10} {run.site or '—':<8} {self.status(run.status):<14} {detail[:max(12, width - 52)]}")
        return lines

    def render_models(self, width: int) -> list[str]:
        settings = get_settings()
        lines = [f"{BOLD}SETTINGS // MODELS & ROLES{RESET}", f"{DIM}↑/↓ choose role · Enter select model · R refresh · / model ROLE PROVIDER/MODEL [fallback ...]{RESET}", ""]
        for index, role in enumerate(LLM_ROLES):
            choice = settings.llm.roles.get(role)
            selected = f"{choice.provider}/{choice.model or 'default'}" if choice and choice.provider else "not selected"
            fallback = f" · fallback: {', '.join(choice.fallbacks)}" if choice and choice.fallbacks else ""
            used = self.store().latest_llm_usage(role)
            usage = f" · used: {used.provider}/{used.model or 'default'}" if used else ""
            marker = self.selection(index == self.settings_role_index)
            lines.append(f"{marker}{CYAN}{role:<14}{RESET} {selected}{DIM}{fallback}{usage}{RESET}")
        lines.append("")
        if self.registry_loading:
            lines.append(f"{DIM}{self.spinner()} discovering providers…{RESET}")
        elif not self.registry:
            lines.append(f"{DIM}No live discovery yet. Run / models refresh.{RESET}")
        for item in self.registry:
            lines.append(f"{self.status(item.availability):<24} {BOLD}{item.provider_id}{RESET}  {DIM}{item.kind} · auth: {item.auth} · {item.detail}{RESET}")
            if item.models:
                for model in item.models[:8]:
                    caps = "stream" if model.stream else "no-stream"
                    lines.append(f"  {CYAN}{model.qualified_id}{RESET}  {DIM}{model.source} · {caps}{RESET}")
            else:
                lines.append(f"  {DIM}No model list yet.{RESET}")
        if self.model_picker:
            role = LLM_ROLES[self.settings_role_index]
            lines.extend(["", f"{YELLOW}{BOLD}MODEL PICKER // {role.upper()}{RESET}"])
            choices = self.model_choices()
            for index, model_ref in enumerate(choices[:12]):
                lines.append(f"{self.selection(index == self.model_picker_index)}{model_ref}")
        lines.extend(["", f"{BOLD}USAGE BY MODEL / ACTION{RESET}"])
        for label, window in (("24h", timedelta(hours=24)), ("7d", timedelta(days=7)), ("all", None)):
            summary = self.store().llm_usage_summary(datetime.now(UTC) - window if window else None)
            if not summary:
                lines.append(f"{DIM}{label}: no recorded calls{RESET}")
                continue
            for item in summary[:6]:
                token_text = str(item["tokens"]) if item["reported_calls"] else "not reported"
                lines.append(f"{DIM}{label:<4}{RESET} {item['provider']}/{item['model']:<20} {item['action']:<22} {token_text} tok · ${float(item['cost_usd']):.4f}")
        return lines

    # Models remains a compatibility name for existing CLI/TUI tests. The
    # user-facing entry point is now Settings → Models & Roles.
    def render_settings_models(self, width: int) -> list[str]:
        return self.render_models(width)

    def render_settings(self, width: int) -> list[str]:
        lines = [f"{BOLD}SETTINGS HUB{RESET}", f"{DIM}↑/↓ choose · Enter open · Esc back · changes stay in ~/.job-harness only{RESET}", ""]
        descriptions = {
            "providers": "Connection status, discovery and enable/disable.",
            "models": "Primary/fallback models for normalization, analysis, writing, orchestration and recovery.",
            "prompts": "Edit role system prompts in the built-in editor.",
            "automation": "Sites, schedule, browser delay and safety limits.",
            "visual": "Motion and terminal visual preferences.",
        }
        for index, section in enumerate(SETTINGS_SECTIONS):
            marker = self.selection(index == self.settings_index)
            lines.append(f"{marker}{BOLD if index == self.settings_index else ''}{section.upper():<16}{RESET} {DIM}{descriptions[section]}{RESET}")
        return lines

    def render_settings_providers(self, width: int) -> list[str]:
        lines = [f"{BOLD}SETTINGS // PROVIDERS{RESET}", f"{DIM}↑/↓ select · E enable · X disable · R refresh · provider tokens are never displayed or copied.{RESET}", ""]
        if self.registry_loading:
            lines.append(f"{CYAN}{self.spinner()} DISCOVERING configured providers…{RESET}")
        elif not self.registry:
            lines.append(f"{DIM}No live discovery yet. Press R to refresh.{RESET}")
        for item in self.registry:
            selected = self.selection(item.provider_id == self.selected_provider_id)
            lines.append(f"{selected}{self.status(item.availability):<24} {BOLD}{item.provider_id:<14}{RESET} {item.kind:<18} auth={item.auth}")
            lines.extend(f"  {DIM}{model.qualified_id} · {model.source} · stream={'yes' if model.stream else 'no'} · tools={'yes' if model.tool_calling else 'no'}{RESET}" for model in item.models[:6])
            if item.detail:
                lines.extend(self.wrap(f"  {item.detail}", max(20, width - 4)))
        return lines

    def render_settings_prompts(self, width: int) -> list[str]:
        role = self.settings_prompt_role
        source = "override" if get_system_prompt(role) != DEFAULT_PROMPTS[role] else "built-in template"
        lines = [
            f"{BOLD}SETTINGS // PROMPTS{RESET}",
            f"{DIM}↑/↓ choose role · Enter/E edit · X reset{RESET}",
            "",
            f"{BOLD}ROLE TO EDIT{RESET}",
        ]
        for index, item in enumerate(LLM_ROLES):
            marker = self.selection(index == self.settings_prompt_role_index)
            item_source = "override" if get_system_prompt(item) != DEFAULT_PROMPTS[item] else "template"
            accent = CYAN if item == role else ""
            lines.append(f"{marker}{accent}{item.upper():<16}{RESET} {DIM}{item_source}{RESET}")
        lines.extend(["", f"{CYAN}ACTIVE ROLE{RESET}  {BOLD}{role.upper()}{RESET}  {DIM}{source}{RESET}", ""])
        lines.extend(self.wrap(get_system_prompt(role), max(20, width - 4)))
        if self.prompt_editing:
            before = self.prompt_buffer[: self.prompt_cursor]
            after = self.prompt_buffer[self.prompt_cursor :]
            editor = before + f"{CYAN}▍{RESET}" + after
            lines.extend(["", f"{YELLOW}{BOLD}EDITOR · Ctrl+S save · Esc cancel · arrows move cursor{RESET}", *self.wrap(editor, max(20, width - 4))])
        return lines

    def render_settings_automation(self, width: int) -> list[str]:
        settings = get_settings()
        actions = self.automation_actions()
        if actions:
            self.automation_index %= len(actions)
        selected = actions[self.automation_index] if actions else ("", None)
        lines = [
            f"{BOLD}SETTINGS // AUTOMATION{RESET}",
            f"{DIM}↑/↓ choose a setting · Enter applies the selected button · Esc returns to Settings{RESET}",
            "",
            f"{BOLD}AUTO WORKFLOW{RESET}",
            f"{self.selection(selected[0] == 'auto_toggle')}[ {'DISABLE' if (settings.applications.auto_mode or settings.applications.unattended_submission) else 'ENABLE'} AUTO MODE ]  {self.status('ENABLED' if (settings.applications.auto_mode or settings.applications.unattended_submission) else 'DISABLED')}",
            f"{self.selection(selected[0] == 'match_down')}[ − ]  match threshold: {settings.applications.auto_match_threshold}/100  {self.selection(selected[0] == 'match_up')}[ + ]",
            f"{self.selection(selected[0] == 'review_down')}[ − ]  review threshold: {settings.applications.auto_review_threshold}/100  {self.selection(selected[0] == 'review_up')}[ + ]",
            f"{self.selection(selected[0] == 'reply_toggle')}[ {'DISABLE' if settings.applications.auto_reply_messages else 'ENABLE'} FACTUAL MESSAGE REPLIES ]",
            f"{DIM}Flow: discover → normalize → analyze → draft → review → rewrite (≤{settings.applications.auto_max_rewrite_attempts}) → site confirmation.{RESET}",
            f"{CYAN}Manual rule:{RESET} an explicit ARMED submission always attempts the site form; required clarifications and site confirmation still apply.",
            "",
            f"{BOLD}SITES{RESET}",
        ]
        for site, config in settings.sites.items():
            marker = self.selection(selected == ("site_toggle", site))
            button = "DISABLE" if config.enabled else "ENABLE"
            lines.append(f"{marker}[ {button:<7} ]  {site:<10} {'enabled' if config.enabled else 'disabled':<9} scan={'on' if config.automation.get('scan_enabled', True) else 'off'} · profile={config.browser_profile}")
        lines.extend(["", f"{BOLD}MESSAGE CAPABILITIES{RESET}"])
        for site, config in settings.sites.items():
            try:
                adapter = build_adapter(config.adapter, BrowserManager(True, 0, 0), config.browser_profile)
                caps = adapter.capabilities
                lines.append(f"{site:<10} read={'yes' if caps.read_messages else 'no'} · send internal={'yes' if caps.send_messages else 'no'}")
            except Exception:
                lines.append(f"{site:<10} {DIM}adapter unavailable{RESET}")
        lines.append("")
        lines.append(f"Browser delay: {settings.env.browser_min_action_delay_seconds}–{settings.env.browser_max_action_delay_seconds}s · daily applications: {settings.limits.applications_per_day}")
        return lines

    def render_settings_visual(self, width: int) -> list[str]:
        return [
            f"{BOLD}SETTINGS // VISUAL{RESET}",
            f"{DIM}Ghostty owns background transparency and blur. Job Harness emits foreground ANSI only.{RESET}",
            "",
            f"Current motion: {self.status(self.motion.upper())}",
            f"{self.selection(self.visual_index == 0)}{self.chip('LIGHT', CYAN, active=self.motion == 'light')} state-driven signals only",
            f"{self.selection(self.visual_index == 1)}{self.chip('HEAVY', YELLOW, active=self.motion == 'heavy')} slow active focus and signal flow",
            "",
            f"{DIM}↑/↓ choose · Enter apply · 1/2 and / motion light|heavy also work.{RESET}",
        ]

    def render_options(self, width: int) -> list[str]:
        current = self.motion.upper()
        return [
            f"{BOLD}OPTIONS // VISUAL SYSTEM{RESET}",
            f"{DIM}Preferences are stored only in ~/.job-harness/config.yaml.{RESET}",
            "",
            f"{self.chip('1 LIGHT', CYAN, active=self.motion == 'light')}  State-driven motion only.",
            f"{self.chip('2 HEAVY', YELLOW, active=self.motion == 'heavy')}  Y2K signal flow and animated active states.",
            "",
            f"Current motion profile: {self.status(current)}",
            f"{DIM}/ motion light|heavy also changes this setting.{RESET}",
        ]

    def render_agent_activity(self, width: int) -> list[str]:
        events = self.store().list_agent_events(limit=max(1, shutil.get_terminal_size((100, 32)).lines - 8))
        lines = [f"{BOLD}AGENT ACTIVITY{RESET}", f"{DIM}Tool transcript: proposal → policy gate → execution result.{RESET}", ""]
        for event in reversed(events):
            subject = f"{event.subject_type}#{event.subject_id or '—'}"
            lines.append(f"{event.created_at:%d.%m %H:%M} {subject:<14} {event.event_type:<21} {event.tool_name or 'agent':<24} {event.detail[:max(8, width - 80)]}")
        return lines if events else lines + [f"{DIM}No agent actions yet. Use / agent message ID or / agent job ID.{RESET}"]

    def render_chat(self, width: int) -> list[str]:
        """One OMP-like transcript with an inline live state and composer.

        Panels are deliberately not animated.  This keeps the Ghostty
        transparent background visually quiet while a single active signal
        tells the user what is happening.
        """
        height = getattr(self, "_render_height", shutil.get_terminal_size((100, 32)).lines)
        inner_width = max(24, width - 8)
        events = self.store().list_agent_events("chat", self.chat_session_id, limit=80) if self.chat_session_id else []
        transcript: list[str] = []
        tool_events = []
        for event in reversed(events):
            if event.event_type == "user_message":
                transcript.extend(self.transcript_message("YOU", CYAN, event.detail, inner_width))
                transcript.append("")
            elif event.event_type == "assistant_message":
                if event.detail.startswith("KWORK RESULT\n"):
                    transcript.extend(self.render_kwork_result(event.detail, inner_width))
                else:
                    transcript.extend(self.transcript_message("AGENT", YELLOW, event.detail, inner_width))
                transcript.append("")
            elif event.event_type in {"tool_execution_end", "tool_execution_error", "tool_blocked"}:
                tool_events.append(event)
        if tool_events:
            if self.chat_show_tools:
                transcript.extend(f"{DIM}↳ {event.tool_name or 'tool'} · {event.detail[:inner_width - 14]}{RESET}" for event in tool_events)
            else:
                counts: dict[str, int] = {}
                for event in tool_events:
                    name = event.tool_name or "tool"
                    counts[name] = counts.get(name, 0) + 1
                summary = " · ".join(f"{name.replace('_', ' ')} ×{count}" for name, count in counts.items())
                transcript.append(f"{DIM}TOOLS  {summary[:inner_width - 20]} · Ctrl+O details{RESET}")
            transcript.append("")
        if self.chat_thinking:
            if self.chat_stream_response:
                transcript.extend(self.transcript_message("AGENT", YELLOW, self.chat_stream_response, inner_width))
                transcript.append(f"{CYAN}{self.spinner()}▍{RESET}")
            elif (self.clock.frame // 8) % 2 == 0:  # two seconds visible, two seconds quiet at active cadence
                tool = next((name.removeprefix("agent-") for name in self.busy_work if name != "chat"), "planning")
                transcript.append(f"{DIM}THINKING  {self.spinner()} {tool} · reading safe Job Harness context{RESET}")
        state = "QUEUED" if self.chat_queued_input else "THINKING" if self.chat_thinking else "NEEDS CLARIFICATION" if self.store().list_clarifications() else "COMPLETE" if self.chat_session_id else "READY"
        state_color = YELLOW if state in {"THINKING", "QUEUED"} else PURPLE if state == "NEEDS CLARIFICATION" else CYAN
        composer_rows = 7
        # Global menu/status uses seven physical rows; this view needs eleven
        # more. Keep composer visible even on a short terminal.
        transcript_rows = max(3, height - composer_rows - 14)
        if not transcript:
            transcript = [f"{DIM}Ask the agent to inspect vacancies, messages, drafts, or the next safe action.{RESET}"]
        self.chat_scroll_offset = min(self.chat_scroll_offset, max(0, len(transcript) - 1))
        end = max(0, len(transcript) - self.chat_scroll_offset)
        start = max(0, end - transcript_rows)
        visible_transcript = transcript[start:end]
        lines = [
            f"{BOLD}JOB HARNESS // AGENT CHAT{RESET}",
            f"{DIM}Memory: profile + conversation + local state · SAFE/ARMED gates external actions · Ctrl+O tool details{RESET}",
            "",
        ]
        lines.extend(self.panel("TRANSCRIPT", visible_transcript, transcript_rows, width, active=self.chat_thinking, prewrapped=True))
        lines.append("")
        model = get_settings().llm.roles.get("orchestration")
        model_ref = f"{model.provider}/{model.model or 'default'}" if model and model.provider else "no orchestration model"
        marker = "▍" if self.chat_editing else ""
        display_input = self.chat_input.replace("\n", " ↵ ")
        prompt = display_input or ("" if self.chat_editing else "Type a message…")
        # Placeholder is a soft idle signal, not a distracting cursor blink.
        prompt_style = "" if self.chat_input else (GRAY if (self.clock.frame // 2) % 2 == 0 else DIM)
        if self.chat_search_mode:
            input_line = f"{DIM}history search: {self.chat_search}{RESET}"
        elif self.chat_input:
            before = self.chat_input[: self.chat_cursor].replace("\n", " ↵ ")
            after = self.chat_input[self.chat_cursor :].replace("\n", " ↵ ")
            input_line = f"> {before}{CYAN}▍{RESET}{after}"
        else:
            input_line = f"{prompt_style}> {prompt}{RESET}{CYAN}{marker}{RESET}"
        composer = [
            f"{state_color}{BOLD}{state}{RESET}  {DIM}{'ARM READY' if self.arm_pending else 'ARMED' if self.armed else 'SAFE'} · ⇧Tab mode · {model_ref}{RESET}",
            input_line,
            f"{DIM}Enter send · Alt+Enter newline · ↑/↓ history · Esc reads transcript · Tab opens main menu{RESET}",
        ]
        lines.extend(self.panel("COMPOSER", composer, 3, width, CYAN, active=self.chat_editing, prewrapped=True))
        lines.append(f"{DIM}Transcript {start + 1}–{end} / {len(transcript)} · Esc then ↑/↓, PgUp/PgDn, Home/End to read · Enter returns to composer{RESET}")
        return lines

    def transcript_message(self, label: str, color: str, value: str, width: int) -> list[str]:
        """One sender label per message; wrapped continuation rows stay aligned."""
        prefix = f"{color}{label}{RESET} "
        indent = " " * (len(label) + 1)
        rows = self.wrap(value, max(20, width - len(label) - 2))
        return [f"{prefix}{rows[0]}", *(f"{indent}{row}" for row in rows[1:])]

    def render_kwork_result(self, value: str, width: int) -> list[str]:
        """Render the deterministic Kwork receipt as a bounded four-column table."""
        source = value.splitlines()
        table_rows = [line for line in source if " | " in line]
        tail = [line for line in source if " | " not in line and line not in {"KWORK RESULT", ""}]
        columns = ("TYPE", "PLATFORM", "SCORE/STATUS", "ITEM", "NEXT STEP")
        # Fixed narrow metadata columns reserve readable space for the title.
        available = max(18, width - 4 - 8 - 8 - 15 - 18)
        widths = (7, 7, 14, available, 17)
        output = [f"{YELLOW}{BOLD}AGENT  KWORK SCAN RESULT{RESET}"]
        output.append(f"{DIM}{' | '.join(name[:size].ljust(size) for name, size in zip(columns, widths))}{RESET}")
        output.append(f"{PURPLE}{'─' * min(width - 2, sum(widths) + 12)}{RESET}")
        for row in table_rows[1:]:
            cells = [cell.strip() for cell in row.split(" | ")]
            cells.extend([""] * (len(widths) - len(cells)))
            output.append(" | ".join(self.clip_ansi(cell, size).ljust(size) for cell, size in zip(cells, widths)))
        output.extend(f"{YELLOW}AGENT{RESET} {line}" for line in tail)
        return output

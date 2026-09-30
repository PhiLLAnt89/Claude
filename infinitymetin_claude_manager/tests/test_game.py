"""Headless tests: SDL dummy drivers, a fake claude CLI, and a strict palette check."""
import os
import queue
import stat
import sys
import time
from pathlib import Path

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import pygame  # noqa: E402
import pytest  # noqa: E402

import infinitymetin_claude_manager as game  # noqa: E402
from infinitymetin_claude_manager import (  # noqa: E402
    IDLE, PALETTE, ROLES, STATUS_DONE, STATUS_FAIL, STATUS_NEW, STATUS_RUN, WORKING,
    Chiptune, ClaudeBackend, Device, Office, SimBackend, SimulationEngine, wrap_text,
)


@pytest.fixture(scope="module", autouse=True)
def init_pygame():
    pygame.mixer.pre_init(22050, -16, 1, 512)
    pygame.init()
    pygame.display.set_mode((64, 64))
    yield
    pygame.quit()


def make_engine(backend_factory, tmp_path, **kwargs):
    events = queue.Queue()
    backend = backend_factory(events)
    engine = SimulationEngine(str(tmp_path), backend, Chiptune(enabled=True), log=lambda text: None,
                              state_path=tmp_path / "state.json", **kwargs)
    return engine


def run_frames(engine, frames):
    for _ in range(frames):
        engine.update()
        engine.draw()


def run_until(engine, condition, seconds=20.0):
    deadline = time.time() + seconds
    while time.time() < deadline:
        engine.update()
        if condition():
            return True
        time.sleep(1 / 240)
    return False


def type_text(engine, text):
    for char in text:
        engine.handle_event(pygame.event.Event(pygame.TEXTINPUT, text=char))
    engine.handle_event(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_RETURN, mod=0, unicode="\r"))


def key(engine, keycode, mod=0):
    engine.handle_event(pygame.event.Event(pygame.KEYDOWN, key=keycode, mod=mod, unicode=""))


def canvas_colors(surface):
    raw = pygame.image.tobytes(surface, "RGB")
    return {tuple(raw[i:i + 3]) for i in range(0, len(raw), 3)}


# ---------------------------------------------------------------------------------------------

def test_art_and_font_are_valid():
    art = game.Art()
    assert set(art.tiles) >= set("#W~DdcSsPCKR.L")
    for role in ROLES:
        for facing in ("down", "up", "left", "right"):
            assert len(art.sprites[role.name][facing]) == 4
    font = game.PixelFont()
    assert font.clean("hello, w0rld!") == "HELLO, W0RLD!"
    assert font.clean("ünïcode") == "?N?CODE"


def test_wrap_text():
    assert wrap_text("one two three four", 9) == ["one two", "three", "four"]
    assert wrap_text("abcdefghijkl", 5) == ["abcde", "fghij", "kl"]
    assert wrap_text("a\n\nb", 10) == ["a", "", "b"]


def test_all_desks_reachable_from_the_lounge():
    office = Office()
    assert len(office.desks) == 18 and len(office.lounge) >= 40
    dummy = game.Agent("X", ROLES[0], office.lounge[0])
    for desk in office.desks:
        for spot in office.lounge[::7]:
            assert office.find_path(spot, desk.seat, dummy), (spot, desk.seat)


def test_chiptune_sounds_are_generated_from_code():
    sound = Chiptune(enabled=True)
    assert sound.ready
    for name in ("spawn", "complete", "assign", "error", "reply", "select", "cancel"):
        assert sound.sounds[name].get_length() > 0.01
    assert sound.sounds["complete"].get_length() > sound.sounds["spawn"].get_length()
    sound.play("complete")  # dummy driver: must not raise


def test_canvas_uses_only_the_four_palette_colours(tmp_path):
    engine = make_engine(lambda events: SimBackend(events, seconds=(0.5, 0.8)), tmp_path)
    for index, role in enumerate(ROLES):
        engine.role_index = index
        type_text(engine, f"task for {role.short}")
    run_until(engine, lambda: any(a.state == WORKING for a in engine.agents), 10)
    engine.select(engine.tasks[0].id)
    engine.show_help = False
    canvas = engine.draw()
    assert canvas_colors(canvas) <= set(PALETTE)
    engine.show_help = True
    assert canvas_colors(engine.draw()) <= set(PALETTE)
    engine.show_help = False
    engine.select(None)
    assert canvas_colors(engine.draw()) <= set(PALETTE)


def test_simulated_task_lifecycle_and_reply(tmp_path):
    engine = make_engine(lambda events: SimBackend(events, seconds=(0.3, 0.6)), tmp_path)
    assert len(engine.agents) == 24
    engine.role_index = 1  # TESTER
    type_text(engine, "check the login flow")
    task = engine.tasks[0]
    assert task.role == "TESTER" and task.status == STATUS_NEW
    assert run_until(engine, lambda: task.status == STATUS_RUN, 10)
    agent = engine.agent_for_task(task)
    assert agent is not None and agent.role.name == "TESTER" and agent.desk is not None
    assert run_until(engine, lambda: task.status in (STATUS_DONE, STATUS_FAIL), 15)
    assert task.transcript[-1][0] == agent.name and task.unread
    assert run_until(engine, lambda: agent.state == IDLE and agent.desk is None, 30)
    # reply continues the same session with the same agent
    engine.select(task.id)
    assert not task.unread
    type_text(engine, "thanks, also check logout")
    assert task.status == STATUS_NEW and task.transcript[-1] == ("YOU", "thanks, also check logout")
    assert run_until(engine, lambda: task.status in (STATUS_DONE, STATUS_FAIL), 30)
    assert engine.agent_for_task(task) is None or task.agent_name
    assert (tmp_path / "state.json").exists() or engine.dirty
    engine.save_state(force=True)
    reloaded = SimulationEngine(str(tmp_path), SimBackend(queue.Queue()), Chiptune(enabled=False),
                                log=lambda t: None, state_path=tmp_path / "state.json")
    assert [t.name for t in reloaded.tasks] == ["check the login flow"]


def test_delete_cancels_running_and_removes_finished(tmp_path):
    engine = make_engine(lambda events: SimBackend(events, seconds=(30.0, 30.0)), tmp_path)
    type_text(engine, "long task")
    task = engine.tasks[0]
    assert run_until(engine, lambda: task.status == STATUS_RUN, 10)
    engine.select(task.id)
    key(engine, pygame.K_DELETE)
    assert run_until(engine, lambda: task.status == STATUS_FAIL, 10)
    assert "CANCELLED" in task.transcript[-1][1]
    key(engine, pygame.K_DELETE)
    assert task not in engine.tasks and engine.selected is None


def test_backlog_waits_for_matching_role_and_capacity(tmp_path):
    engine = make_engine(lambda events: SimBackend(events, seconds=(5.0, 5.0)), tmp_path, max_jobs=2)
    for i in range(5):
        type_text(engine, f"dev task {i}")
    run_frames(engine, 5)
    assert sum(1 for t in engine.tasks if t.active) == 2
    assert sum(1 for t in engine.tasks if t.status == STATUS_NEW) == 3


def test_input_box_editing_and_role_switching(tmp_path):
    engine = make_engine(lambda events: SimBackend(events), tmp_path)
    key(engine, pygame.K_TAB)
    assert engine.current_role.name == "TESTER"
    for char in "helo":
        engine.handle_event(pygame.event.Event(pygame.TEXTINPUT, text=char))
    key(engine, pygame.K_BACKSPACE)
    engine.handle_event(pygame.event.Event(pygame.TEXTINPUT, text="lo"))
    assert engine.input.text == "hello"
    assert engine.input.visible(10, True) == "hello_"
    key(engine, pygame.K_F1)
    assert engine.show_help
    key(engine, pygame.K_ESCAPE)
    assert not engine.show_help and engine.selected is None


def fake_claude_executable(tmp_path):
    script = HERE / "fake_claude.py"
    if os.name == "nt":
        wrapper = tmp_path / "claude.cmd"
        wrapper.write_text(f'@"{sys.executable}" "{script}" %*\n')
    else:
        wrapper = tmp_path / "claude"
        wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n')
        wrapper.chmod(wrapper.stat().st_mode | stat.S_IEXEC)
    return str(wrapper)


def test_claude_backend_runs_tasks_and_resumes_sessions(tmp_path, monkeypatch):
    log_file = tmp_path / "calls.jsonl"
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(log_file))
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ok")
    project = tmp_path / "project"
    project.mkdir()
    executable = fake_claude_executable(tmp_path)
    engine = make_engine(lambda events: ClaudeBackend(executable, str(project), events, "acceptEdits",
                                                      log=lambda t: None, allowed_tools="Read,Edit"), tmp_path)
    type_text(engine, "fix the login bug")
    task = engine.tasks[0]
    assert run_until(engine, lambda: task.status == STATUS_DONE, 30), task.transcript
    assert task.transcript[-1][1].startswith("DONE: FIX THE LOGIN BUG".title()) or "Done: fix the login bug" in task.transcript[-1][1]
    assert "DENIED" in task.transcript[-1][1] and "Bash" in task.transcript[-1][1]
    assert task.session_id.startswith("sess-") and abs(task.cost - 0.0421) < 1e-6
    assert task.tool_calls >= 2
    engine.select(task.id)
    type_text(engine, "now add a test")
    assert run_until(engine, lambda: task.status == STATUS_DONE and len(task.transcript) == 4, 30), task.transcript
    calls = [line for line in log_file.read_text().splitlines() if line.strip()]
    assert len(calls) == 2
    import json
    first, second = json.loads(calls[0]), json.loads(calls[1])
    assert first["cwd"] == str(project) and not first["stdin_tty"]
    assert "-p" in first["args"] and "stream-json" in first["args"] and "--append-system-prompt" in first["args"]
    assert first["args"][first["args"].index("--permission-mode") + 1] == "acceptEdits"
    assert first["args"][first["args"].index("--allowedTools") + 1] == "Read,Edit"
    assert "--resume" not in first["args"]
    assert second["args"][second["args"].index("--resume") + 1] == task.session_id
    assert task.transcript[-1][1].startswith("Resumed: now add a test")


def test_claude_backend_reports_crashes(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(tmp_path / "calls.jsonl"))
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "crash")
    executable = fake_claude_executable(tmp_path)
    engine = make_engine(lambda events: ClaudeBackend(executable, str(tmp_path), events, log=lambda t: None), tmp_path)
    type_text(engine, "anything")
    task = engine.tasks[0]
    assert run_until(engine, lambda: task.status == STATUS_FAIL, 30)
    assert "CLAUDE EXITED (3)" in task.transcript[-1][1] and "something broke" in task.transcript[-1][1]


def test_claude_backend_cancel_kills_the_process(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(tmp_path / "calls.jsonl"))
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "slow")
    executable = fake_claude_executable(tmp_path)
    engine = make_engine(lambda events: ClaudeBackend(executable, str(tmp_path), events, log=lambda t: None), tmp_path)
    type_text(engine, "slow one")
    task = engine.tasks[0]
    assert run_until(engine, lambda: task.status == STATUS_RUN, 30)
    started = time.time()
    engine.select(task.id)
    key(engine, pygame.K_DELETE)
    assert run_until(engine, lambda: task.status == STATUS_FAIL, 15)
    assert time.time() - started < 10 and "CANCELLED" in task.transcript[-1][1]


def test_device_presents_frames(tmp_path):
    device = Device(2, grid=True)
    engine = make_engine(lambda events: SimBackend(events), tmp_path)
    for frame in range(3):
        device.present(engine.draw(), busy=frame % 2 == 0, frame=frame)
    assert device.window.get_size() == (game.DEVICE_W * 2, game.DEVICE_H * 2)
    assert device.to_canvas((game.BEZEL_L * 2 + 10, game.BEZEL_T * 2 + 10)) == (5, 5)
    assert device.to_canvas((0, 0)) is None
    colors = canvas_colors(device.shell)
    assert colors <= set(PALETTE)  # the shell itself is strict; only the optional LCD overlay blends


def test_find_claude_and_command_for(tmp_path, monkeypatch):
    monkeypatch.setattr(game.shutil, "which", lambda name: None)
    monkeypatch.setattr(game.Path, "home", lambda: tmp_path)
    assert game.find_claude(None) is None
    fake = tmp_path / "claude-bin"
    fake.write_text("")
    assert game.find_claude(str(fake)) == str(fake)
    local = tmp_path / ".local" / "bin"
    local.mkdir(parents=True)
    (local / "claude.exe").write_text("")
    assert game.find_claude(None) == str(local / "claude.exe")
    assert game.command_for("/usr/bin/claude", ["-p", "x"]) == ["/usr/bin/claude", "-p", "x"]
    if os.name == "nt":
        assert game.command_for(r"C:\npm\claude.cmd", ["-p"])[:2] == ["cmd.exe", "/c"]

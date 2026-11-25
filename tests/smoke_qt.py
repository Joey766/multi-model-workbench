import os
from pathlib import Path
import sys
import tempfile
import threading
import time
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRATCH = ROOT / "build" / "test-artifacts"
SCRATCH.mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(ROOT / "src"))

from PySide6.QtWidgets import QApplication, QComboBox, QDialog, QLineEdit, QPushButton
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QImage
from PySide6.QtTest import QTest
from app import App, WorkspaceBusy, checked_images
from quotas import parse_usage
from model_catalog import model_spec
from providers import PROVIDERS, Reply, image_content, messages_for_chat
import project_git
from qt_settings import qr_pixmap
from qt_widgets import split_diff
from qt_workbench import Workbench


def until(check, qt, seconds=15):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        qt.processEvents()
        if value := check():
            return value
        time.sleep(.035)
    raise AssertionError("Timed out waiting for requested UI state.")


def desktop(engine, qt):
    bench = Workbench(engine)
    bench.show()
    qt.processEvents()
    assert bench.grab().save(str(SCRATCH / "qt-home.png"))
    until(lambda: engine.plugins.snapshot()[0][0]["state"] == "connected", qt)
    bench.new_demo()
    for kind in ("edit", "command", "plugin"):
        pending = until(lambda: bench.pending if bench.pending and bench.pending["kind"] == kind else None, qt)
        if kind == "edit":
            left, right = split_diff(pending["diff"])
            assert "Hello" in left and "你好" in right
        engine.runs[bench.task["id"]].decide(pending["id"], True)
    until(lambda: bench.task["changes"] and not engine.active(), qt)
    assert "退出码 0" in bench.transcript.toPlainText()
    assert '"valid": true' in bench.transcript.toPlainText()
    assert bench.task["changes"][0]["status"] == "applied"
    bench.project.tabs.setCurrentIndex(1)
    bench.toggle_terminal()
    bench.terminal.entry.setText("python -B -m unittest -v")
    bench.terminal.execute()
    until(lambda: "退出码 0" in bench.terminal.output.toPlainText(), qt)
    assert "test_greeting" in bench.terminal.output.toPlainText()
    until(lambda: bench.transcript.verticalScrollBar().value() == bench.transcript.verticalScrollBar().maximum(), qt)
    assert bench.grab().save(str(SCRATCH / "qt-task.png"))
    assert not qr_pixmap("https://example.invalid/verification").isNull()
    picture = QImage(100, 60, QImage.Format.Format_RGB32)
    picture.fill(QColor("#12161f"))
    bench.add_image(picture, "test.jpg")
    pictures = checked_images(bench.images[bench.task["id"]], "gpt-6.2-sol")
    messages = [{"role": "user", "text": "image check", "images": pictures}]
    for provider, field in (("openai", "input_image"), ("claude", "image"), ("deepseek", "image_url")):
        wire_shape = image_content(messages_for_chat(messages), provider)[0]["content"]
        assert wire_shape[1]["type"] == field
    bench.drop_context()
    bench.close()
    qt.processEvents()


def parallel(qt, base):
    engine = App(base / "engine")
    project = base / "project"
    project.mkdir()
    second = base / "isolated"
    second.mkdir()
    engine.store.set_key("openai", "fake-unit-test-value")
    one, overlap, two, plan = [engine.create(path)["id"] for path in (project, project, second, project)]
    release = threading.Event()
    observed = {}

    def simulated(model, effort, key, system, messages, tools, history, emit, stop):
        observed[messages[-1]["text"].split("\n", 1)[0]] = {item["name"] for item in tools}
        release.wait(4)
        emit("done")
        return Reply("done", [], None)

    def task_args(task, mode, text):
        return {"task": task, "mode": mode, "model": "gpt-6.2-sol", "effort": "medium", "prompt": text,
                "context": "Attached project file"}

    try:
        with patch.dict("app.ADAPTERS", {"openai": simulated}):
            engine.start(task_args(one, "agent", "first"))
            engine.start(task_args(two, "ask", "second"))
            engine.start(task_args(plan, "plan", "third"))
            assert set(engine.state()["active_tasks"]) == {one, two, plan}
            try:
                engine.start(task_args(overlap, "agent", "overlap"))
                raise AssertionError("Overlapping workspace was not blocked.")
            except WorkspaceBusy:
                pass
            release.set()
            until(lambda: not engine.active(), qt)
            assert "run_command" in observed["first"]
            assert "read_file" in observed["third"] and "run_command" not in observed["third"]
            assert not observed["second"]
            assert engine.detail(one)["messages"][0]["context"] == "Attached project file"
    finally:
        release.set()
        engine.plugins.close()

    project_git.git(project, "init", "-b", "main")
    file = project / "hello.txt"
    file.write_text("old\n", encoding="utf-8")
    project_git.git(project, "add", "--", "hello.txt")
    project_git.git(project, "-c", "user.name=Workbench test", "-c", "user.email=test@example.invalid", "commit", "-m", "seed")
    file.write_text("new\n", encoding="utf-8")
    branch, rows = project_git.inventory(project)
    assert branch == "main" and rows[0][1] == "hello.txt"
    from workspace import Workspace
    diff = project_git.difference(Workspace(project), rows[0][1], rows[0][0])
    assert "-old" in diff and "+new" in diff
    tree = project_git.add_worktree(project, "agent/demo", base / "worktree")
    assert (tree / "hello.txt").read_text() == "old\n"
    assert file.read_text() == "new\n"


def multitask(qt, base):
    project = base / "multi-project"
    separate = base / "isolation"
    project.mkdir()
    separate.mkdir()
    engine = App(base / "multi-engine")
    engine.store.set_key("openai", "fake-multi-test-token")
    writer = engine.create(project, draft="writer")["id"]
    queued = engine.create(project, draft="queued draft")["id"]
    reader = engine.create(project, draft="reader", mode="plan")["id"]
    isolated = engine.create(separate, project_root=project, draft="isolated")["id"]
    release = threading.Event()
    launched = set()

    def simulate(model, effort, key, system, messages, tools, history, emit, stop):
        prompt = messages[-1]["text"]
        launched.add(prompt)
        if prompt == "writer":
            release.wait(12)
        emit("confirmed")
        return Reply("confirmed", [], None)

    bench = Workbench(engine)
    bench.show()
    try:
        with patch.dict("app.ADAPTERS", {"openai": simulate}):
            bench.open_task(writer)
            bench.run_selected()
            until(lambda: "writer" in launched and engine.active(writer), qt)
            bench.open_task(reader)
            qt.processEvents()
            for key in (writer, queued, isolated):
                group = bench.tree.topLevelItem(0)
                node = next(group.child(i) for i in range(group.childCount()) if group.child(i).data(0, Qt.ItemDataRole.UserRole) == key)
                where = bench.tree.visualItemRect(node).center()
                QTest.mouseClick(bench.tree.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ControlModifier, where)
                qt.processEvents()
            assert bench.tree.topLevelItemCount() == 1
            assert len(bench.selected_tasks()) == 4
            bench.refresh_sidebar(force=True)
            assert len(bench.selected_tasks()) == 4
            bench.run_selected()
            until(lambda: {"reader", "isolated"} <= launched and queued in bench.queued, qt)
            assert "queued draft" not in launched
            assert engine.active(writer)
            assert engine.detail(queued)["draft"] == "queued draft"
            release.set()
            until(lambda: "queued draft" in launched and not engine.active(), qt)
            assert not bench.queued
            assert engine.detail(queued)["messages"][0]["text"] == "queued draft"
            until(lambda: bench.project.git is not None, qt)
    finally:
        release.set()
        bench.queued.clear()
        until(lambda: not engine.active(), qt)
        until(lambda: bench.project.git is not None, qt)
        bench.close()
        qt.processEvents()


def quota_metadata():
    from datetime import datetime, timezone, timedelta
    now = datetime(2026, 11, 25, 9, 0, tzinfo=timezone.utc)
    oai = parse_usage("openai", 429, {"X-RateLimit-Limit-Requests": "60", "X-RateLimit-Remaining-Requests": "0",
                                    "X-RateLimit-Reset-Requests": "1m12.5s", "Retry-After": "20"}, now)
    assert oai["buckets"][0]["remaining"] == 0
    assert oai["buckets"][0]["reset"] == now + timedelta(seconds=72.5)
    assert oai["retry"] == now + timedelta(seconds=20)
    claude = parse_usage("claude", 200, {"anthropic-ratelimit-input-tokens-limit": "50000",
                                        "anthropic-ratelimit-input-tokens-remaining": "45000",
                                        "anthropic-ratelimit-input-tokens-reset": "2026-11-25T09:01:00Z"}, now)
    assert claude["buckets"][0]["reset"] == now + timedelta(minutes=1)
    assert not parse_usage("gemini", 200, {}, now)["buckets"]


def discovery_ui(qt, base):
    project = base / "discovery-project"
    project.mkdir()
    engine = App(base / "discovery")
    first = dict([model_spec("openai", {"id": "gpt-4.1-mini"})])
    second = dict([model_spec("openai", {"id": "gpt-4o-mini"})])
    no_provider_keys = {provider["env"]: "" for provider in PROVIDERS.values()}
    with patch.dict(os.environ, no_provider_keys), patch("model_catalog.fetch_models", side_effect=[first, second]):
        bench = Workbench(engine)
        bench.show()
        bench.open_task(engine.create(str(project))["id"])
        panel = bench.settings.provider_panel(QDialog(bench), "openai", PROVIDERS["openai"])
        field = panel.findChild(QLineEdit)

        def click(caption):
            next(control for control in panel.findChildren(QPushButton) if control.text() == caption).click()

        field.setText("qt-offline-fixture-key")
        click("保存 Key")
        until(lambda: bench.model.findData("gpt-4.1-mini") >= 0, qt)
        assert panel.findChild(QComboBox).currentData() == "gpt-4.1-mini"
        assert bench.model.currentData() == "gpt-6.2-sol" and not bench.send_btn.isEnabled()
        bench.model.setCurrentIndex(bench.model.findData("gpt-4.1-mini"))
        assert bench.effort.currentData() == "auto" and bench.send_btn.isEnabled()
        click("刷新模型")
        until(lambda: bench.model.findData("gpt-4o-mini") >= 0, qt)
        assert not bench.send_btn.isEnabled()
        until(lambda: bench.project.git is not None, qt)
        bench.close()
        qt.processEvents()


if __name__ == "__main__":
    qt = QApplication(["qa"] if "--native" in sys.argv else ["qa", "-platform", "offscreen"])
    with tempfile.TemporaryDirectory(dir=SCRATCH) as scratch:
        base = Path(scratch)
        desktop(App(base / "desktop"), qt)
        parallel(qt, base)
        multitask(qt, base)
        quota_metadata()
        discovery_ui(qt, base)
    print("PASS: desktop workflows, multi-selection, queued writes, concurrent reads, worktrees, quotas and model discovery")

import argparse
import base64
from pathlib import Path
import queue
import sys
import threading

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QItemSelectionModel, QPoint, QTimer, Qt
from PySide6.QtGui import QColor, QIcon, QImage, QImageReader, QKeySequence, QShortcut
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QFileDialog, QFrame, QHBoxLayout,
                               QInputDialog, QLineEdit, QMainWindow, QMenu, QPlainTextEdit,
                               QSizePolicy, QSplitter, QStackedWidget, QTreeWidget,
                               QTreeWidgetItem, QVBoxLayout, QWidget)

from app import App, WorkspaceBusy, instance_lock, local_data
from project_panel import ProjectPanel
from providers import IMAGE_MODELS, MODELS, PROVIDERS
from qt_settings import Settings, base_dialog, confirm, message
from qt_theme import DARK, LIGHT, Combo, apply_theme, button, label, separator
from qt_widgets import Terminal, Transcript, diff_dialog
from wechat import WeChat


TITLE = "多模型工作台"
EFFORTS = {"auto": "默认", "none": "关闭", "low": "低", "medium": "中", "high": "高", "xhigh": "超高", "max": "最高"}
MODES = {"agent": "执行", "plan": "计划", "ask": "问答"}
MODEL_NAMES = {key: value["name"] for key, value in MODELS.items()}
ROLE = Qt.ItemDataRole.UserRole


def flexible_label(text="", style="Muted"):
    widget = label(text, style)
    widget.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
    return widget


class Workbench(QMainWindow):
    def __init__(self, engine):
        super().__init__()
        self.engine = engine
        self.theme = engine.store.state.get("qt_theme", "dark")
        self.p = LIGHT if self.theme == "light" else DARK
        apply_theme(self.p)
        self.jobs = queue.SimpleQueue()
        self.task = self.workspace = None
        self.drafts, self.attachments = {}, {}
        self.images = {}
        self.queued = {}
        self.queue_signature = None
        self.run_id = self.live = self.pending = None
        self.after = 0
        self.task_signature = None
        self.settings = Settings(self)
        try:
            self.wechat = WeChat(engine.store)
        except ValueError:
            self.wechat = None
        if self.wechat:
            self.wechat.start_poll()
        self.build()
        self.shortcuts()
        last = engine.store.state.get("desktop_task")
        if any(item["id"] == last and not item["archived"] for item in engine.state()["tasks"]):
            self.open_task(last)
        self.refresh_sidebar()
        self.update_controls()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(180)
        QTimer.singleShot(0, self.settings.discover_configured)

    def build(self):
        self.setWindowTitle(TITLE)
        self.resize(1390, 845)
        self.setMinimumSize(960, 640)
        icon = Path(__file__).with_name("desktop.ico")
        if icon.exists():
            self.setWindowIcon(QIcon(str(icon)))
        shell = QSplitter(Qt.Orientation.Horizontal)
        shell.setChildrenCollapsible(False)
        self.setCentralWidget(shell)
        shell.addWidget(self.build_sidebar())
        body = QWidget()
        boxes = QVBoxLayout(body)
        boxes.setContentsMargins(0, 0, 0, 0)
        boxes.setSpacing(0)
        boxes.addWidget(self.build_header())
        self.work_area = QSplitter(Qt.Orientation.Horizontal)
        self.work_area.setChildrenCollapsible(False)
        boxes.addWidget(self.work_area, 1)
        self.center = QSplitter(Qt.Orientation.Vertical)
        self.center.setChildrenCollapsible(False)
        self.screens = QStackedWidget()
        self.screens.addWidget(self.build_welcome())
        self.screens.addWidget(self.build_chat())
        self.center.addWidget(self.screens)
        self.terminal = Terminal(self.p, self.refresh_project)
        self.terminal.setMinimumHeight(165)
        self.center.addWidget(self.terminal)
        self.terminal.hide()
        self.work_area.addWidget(self.center)
        self.project = ProjectPanel(self)
        self.work_area.addWidget(self.project)
        self.work_area.setSizes([750, 340])
        self.project.hide()
        shell.addWidget(body)
        shell.setStretchFactor(1, 1)
        shell.setSizes([248, 1140])
        self.statusBar().showMessage("本机工作台")

    def build_sidebar(self):
        sidebar = QWidget()
        sidebar.setObjectName("Sidebar")
        sidebar.setMinimumWidth(210)
        sidebar.setMaximumWidth(340)
        bar = QVBoxLayout(sidebar)
        bar.setContentsMargins(15, 19, 15, 12)
        bar.setSpacing(8)
        brand = QHBoxLayout()
        badge = label("›_", "Badge")
        badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        badge.setFixedSize(34, 34)
        brand.addWidget(badge)
        brand.addWidget(label(TITLE, "Brand"), 1)
        bar.addLayout(brand)
        bar.addSpacing(15)
        create = QHBoxLayout()
        create.addWidget(button("＋ 新建任务", self.new_task, "Primary", "新建任务 · Ctrl+N"), 1)
        create.addWidget(button("批量…", self.new_bulk, "Pill", "在同一个项目批量新建草稿"))
        bar.addLayout(create)
        bar.addWidget(button("打开项目…", self.pick_project, "Quiet", "打开项目 · Ctrl+O"))
        bar.addSpacing(11)
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("查找项目或任务")
        self.filter.setAccessibleName("查找项目或任务")
        self.filter.textChanged.connect(lambda _: self.refresh_sidebar(force=True))
        bar.addWidget(self.filter)
        heading = QHBoxLayout()
        heading.addWidget(label("任务", "Muted"), 1)
        self.archive_toggle = button("归档", self.toggle_archive, "Pill")
        heading.addWidget(self.archive_toggle)
        bar.addLayout(heading)
        self.show_archived = False
        self.tree = QTreeWidget()
        self.tree.setObjectName("TaskTree")
        self.tree.setHeaderHidden(True)
        self.tree.setIndentation(14)
        self.tree.setAccessibleName("项目与任务")
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.currentItemChanged.connect(lambda current, _: self.tree_selection(current))
        self.tree.itemSelectionChanged.connect(self.selection_controls)
        self.tree.customContextMenuRequested.connect(self.task_menu)
        bar.addWidget(self.tree, 1)
        selected = QHBoxLayout()
        self.run_selected_btn = button("运行所选", self.run_selected, "Pill", "按 Ctrl 或 Shift 多选任务，再运行所选草稿")
        self.stop_selected_btn = button("停止所选", self.stop_selected, "Pill")
        selected.addWidget(self.run_selected_btn, 1)
        selected.addWidget(self.stop_selected_btn)
        bar.addLayout(selected)
        bar.addWidget(separator())
        self.plugin_button = button("◇  连接与插件", self.settings.plugins, "Quiet")
        self.wechat_button = button("○  微信 ClawBot", self.settings.wechat, "Quiet")
        for widget in (self.plugin_button, self.wechat_button, button("⚙  模型与设置", self.settings.models, "Quiet")):
            bar.addWidget(widget)
        return sidebar

    def build_header(self):
        header = QFrame()
        header.setObjectName("Header")
        header.setMinimumHeight(62)
        layout = QHBoxLayout(header)
        layout.setContentsMargins(20, 9, 17, 9)
        self.project_name = label("工作台", "Brand")
        self.breadcrumb = flexible_label()
        layout.addWidget(self.project_name)
        layout.addWidget(self.breadcrumb, 1)
        self.branch_tag = button("Git", lambda: self.project.git_menu(self.branch_tag), "Pill", "Git 分支与工作树")
        limit_btn = button("模型额度", lambda: self.settings.usage(self.model.currentData()), "Pill")
        self.terminal_btn = button(">_  终端", self.toggle_terminal, "Pill", "显示项目终端")
        self.review_btn = button("▤  项目面板", self.toggle_project, "Pill", "显示项目面板")
        for widget in (limit_btn, self.branch_tag, self.terminal_btn, self.review_btn):
            layout.addWidget(widget)
        return header

    def build_welcome(self):
        panel = QWidget()
        shell = QVBoxLayout(panel)
        shell.setContentsMargins(38, 25, 38, 36)
        shell.addStretch(1)
        content = QWidget()
        content.setMaximumWidth(690)
        welcome = QVBoxLayout(content)
        welcome.setSpacing(12)
        badge = label("›_", "Badge")
        badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        badge.setFixedSize(47, 47)
        welcome.addWidget(badge)
        welcome.addWidget(label("开始一个任务", "Hero"))
        desc = label("打开本地项目，用你选的模型阅读代码、修改文件和运行测试。", "Muted")
        desc.setWordWrap(True)
        welcome.addWidget(desc)
        welcome.addSpacing(14)
        cards = QHBoxLayout()
        cards.setSpacing(12)
        for name, detail, call in (("打开项目", "选择本地代码目录", self.pick_project),
                                   ("离线演示", "看差异、审批改动、运行测试", self.new_demo)):
            card = QFrame()
            card.setObjectName("Card")
            contents = QVBoxLayout(card)
            contents.setContentsMargins(14, 13, 14, 13)
            contents.addWidget(button(name + "   ↗", call, "Quiet"))
            description = label(detail, "Muted")
            description.setWordWrap(True)
            contents.addWidget(description)
            cards.addWidget(card, 1)
        welcome.addLayout(cards)
        welcome.addWidget(button("配置模型与 API Key", self.settings.models, "Quiet"), alignment=Qt.AlignmentFlag.AlignLeft)
        shell.addWidget(content, alignment=Qt.AlignmentFlag.AlignHCenter)
        shell.addStretch(2)
        return panel

    def build_chat(self):
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(24, 17, 24, 15)
        self.task_title = flexible_label("", "Title")
        layout.addWidget(self.task_title)
        self.transcript = Transcript(self.p)
        layout.addWidget(self.transcript, 1)
        self.approval = QFrame()
        self.approval.setObjectName("Approval")
        pending = QHBoxLayout(self.approval)
        pending.setContentsMargins(13, 6, 7, 6)
        self.approval_text = flexible_label("等待审批")
        pending.addWidget(self.approval_text, 1)
        pending.addWidget(button("查看并审批", self.review_pending, "Pill"))
        layout.addWidget(self.approval)
        self.approval.hide()
        composer = QFrame()
        composer.setObjectName("Composer")
        draft = QVBoxLayout(composer)
        draft.setContentsMargins(12, 10, 11, 10)
        self.prompt = QPlainTextEdit()
        self.prompt.setObjectName("Prompt")
        self.prompt.setPlaceholderText("告诉工作台你想做什么…")
        self.prompt.setAccessibleName("任务指令")
        self.prompt.setFixedHeight(76)
        draft.addWidget(self.prompt)
        contextual = QHBoxLayout()
        self.context_text = flexible_label()
        self.clear_context = button("移除上下文", self.drop_context, "Pill")
        contextual.addWidget(self.context_text, 1)
        contextual.addWidget(self.clear_context)
        draft.addLayout(contextual)
        controls = QHBoxLayout()
        controls.setSpacing(6)
        self.mode = Combo()
        self.mode.setAccessibleName("任务模式")
        self.mode.setToolTip("执行可提议改动和命令，计划只读项目，问答仅使用对话和主动附加内容。")
        for name, title in MODES.items():
            self.mode.addItem(title, name)
        self.model = Combo()
        self.model.setAccessibleName("选择模型")
        self.model.setMinimumWidth(170)
        self.model.currentIndexChanged.connect(lambda _: self.update_effort())
        self.effort = Combo()
        self.effort.setAccessibleName("推理强度")
        self.effort.setMinimumWidth(59)
        self.attach_btn = button("＋", self.attach_menu, "Pill", "添加项目上下文")
        for widget in (self.attach_btn, self.mode, self.model, self.effort):
            controls.addWidget(widget)
        controls.addStretch()
        self.stop_btn = button("停止", self.stop_task, "Pill")
        self.send_btn = button("发送 ↑", self.send, "Primary", "发送 · Ctrl+Enter")
        controls.addWidget(self.stop_btn)
        controls.addWidget(self.send_btn)
        draft.addLayout(controls)
        layout.addWidget(composer)
        self.subtitle = flexible_label()
        layout.addWidget(self.subtitle)
        return panel

    def shortcuts(self):
        self.keys = []
        for hotkey, action in (("Ctrl+O", self.pick_project), ("Ctrl+N", self.new_task), ("Ctrl+K", self.filter.setFocus),
                               ("Ctrl+Return", self.send), ("Ctrl+Enter", self.send), ("Ctrl+\x60", self.toggle_terminal)):
            key = QShortcut(QKeySequence(hotkey), self)
            key.activated.connect(action)
            self.keys.append(key)

    def job(self, operation, finish):
        def perform():
            try:
                value, error = operation(), None
            except Exception as exc:
                value, error = None, self.engine.store.redact(str(exc))
            self.jobs.put((finish, value, error))
        threading.Thread(target=perform, daemon=True).start()

    def change_theme(self, name):
        if name not in {"dark", "light"} or name == self.theme:
            return
        self.theme = name
        self.p = LIGHT if name == "light" else DARK
        apply_theme(self.p)
        self.transcript.p = self.p
        self.paint_chat()
        self.refresh_sidebar(force=True)
        with self.engine.store.lock:
            self.engine.store.state["qt_theme"] = name
            self.engine.store.save()

    def refresh_sidebar(self, force=False):
        state = self.engine.state()
        rows, active = state["tasks"], set(state["active_tasks"])
        selected = self.task["id"] if self.task else None
        approvals = {key for key in active if self.engine.runs[key].approval}
        waiting = set(self.queued)
        signature = (tuple((x["id"], x["title"], x["updated"], x["pinned"], x["archived"], x["project_root"]) for x in rows),
                     active, approvals, waiting, selected)
        if not force and signature == self.task_signature:
            return
        self.task_signature = signature
        query = self.filter.text().strip().casefold()
        tree = self.tree
        selected_ids = set(self.selected_tasks())
        tree.blockSignals(True)
        expanded = {tree.topLevelItem(i).data(0, ROLE + 1): tree.topLevelItem(i).isExpanded()
                    for i in range(tree.topLevelItemCount())}
        tree.clear()
        projects = {}
        for item in sorted(rows, key=lambda x: not x["pinned"]):
            if item["archived"] != self.show_archived or query and query not in (
                item["root"] + item["title"] + item["project_root"]
            ).casefold():
                continue
            project = "演示项目" if item["demo"] else item["project_root"]
            if project not in projects:
                group = QTreeWidgetItem(tree, [Path(project).name or project])
                group.setData(0, ROLE + 1, project)
                group.setToolTip(0, project)
                group.setForeground(0, QColor(self.p["muted"]))
                group.setFlags(group.flags() & ~Qt.ItemFlag.ItemIsSelectable)
                projects[project] = group
            parent = projects[project]
            mark = ("◆  " if item["id"] in approvals else "●  " if item["id"] in active else
                    "◷  " if item["id"] in waiting else "★  " if item["pinned"] else "    ")
            worktree = item["root"] != item["project_root"]
            node = QTreeWidgetItem(parent, [mark + item["title"] + ("  ↗" if worktree else "")])
            node.setData(0, ROLE, item["id"])
            node.setToolTip(0, item["title"] + ("\n工作树：" + item["root"] if worktree else ""))
            if item["id"] == selected:
                tree.setCurrentItem(node, 0, QItemSelectionModel.SelectionFlag.NoUpdate)
            if item["id"] in selected_ids or (not selected_ids and item["id"] == selected):
                node.setSelected(True)
            parent.setExpanded(expanded.get(project, True) or bool(query))
        tree.blockSignals(False)
        self.selection_controls()

    def toggle_archive(self):
        self.show_archived = not self.show_archived
        self.archive_toggle.setText("返回任务" if self.show_archived else "归档")
        self.refresh_sidebar(force=True)

    def tree_selection(self, item):
        identifier = item.data(0, ROLE) if item else None
        if identifier and (not self.task or identifier != self.task["id"]):
            def open_current():
                current = self.tree.currentItem()
                if current and current.data(0, ROLE) == identifier:
                    self.open_task(identifier, keep_selection=True)
            QTimer.singleShot(0, open_current)

    def selected_tasks(self):
        return [item.data(0, ROLE) for item in self.tree.selectedItems() if item.data(0, ROLE)]

    def selection_controls(self):
        selection = self.selected_tasks()
        self.run_selected_btn.setText("运行所选" + (f" · {len(selection)}" if len(selection) > 1 else ""))
        self.run_selected_btn.setEnabled(bool(selection) and not self.show_archived)
        self.stop_selected_btn.setEnabled(any(key in self.queued or self.engine.active(key) for key in selection))

    def select_task_rows(self, keys):
        self.tree.blockSignals(True)
        self.tree.clearSelection()
        for n in range(self.tree.topLevelItemCount()):
            group = self.tree.topLevelItem(n)
            for i in range(group.childCount()):
                node = group.child(i)
                if node.data(0, ROLE) in keys:
                    group.setExpanded(True)
                    node.setSelected(True)
        self.tree.blockSignals(False)
        self.selection_controls()

    def task_menu(self, point):
        item = self.tree.itemAt(point)
        identifier = item.data(0, ROLE) if item else None
        if not identifier:
            return
        task = self.engine.detail(identifier)
        menu = QMenu(self)
        rename = menu.addAction("重命名")
        pin = menu.addAction("取消置顶" if task.get("pinned") else "置顶")
        archive = menu.addAction("恢复任务" if task.get("archived") else "归档任务")
        action = menu.exec(self.tree.viewport().mapToGlobal(point))
        values = {}
        if action is rename:
            value, ok = QInputDialog.getText(self, "重命名任务", "任务名称", text=task["title"])
            if ok and value.strip():
                values = {"title": value.strip()[:100], "title_custom": True}
        elif action is pin:
            values = {"pinned": not task.get("pinned", False)}
        elif action is archive:
            if self.engine.active(identifier) or identifier in self.queued:
                message(self, "请先结束这个任务。")
            else:
                values = {"archived": not task.get("archived", False)}
        if values:
            with self.engine.store.lock:
                self.engine.task(identifier).update(values)
                self.engine.store.save()
            if self.task and self.task["id"] == identifier:
                self.task = self.engine.detail(identifier)
                self.paint_chat()
                self.update_controls()
            self.refresh_sidebar(force=True)

    def pick_project(self):
        directory = str(self.workspace.root) if self.workspace else str(Path.home())
        path = QFileDialog.getExistingDirectory(self, "选择本地项目", directory)
        if path:
            self.make_task(path)

    def make_task(self, path=None, demo=False, project_root=None):
        try:
            self.open_task(self.engine.create(path, demo, project_root=project_root)["id"])
        except (ValueError, OSError) as exc:
            message(self, exc)

    def new_task(self):
        if self.workspace and self.task and not self.task["demo"]:
            self.make_task(self.task.get("project_root", self.task["root"]))
        else:
            self.pick_project()

    def new_bulk(self):
        root = self.task.get("project_root", self.task["root"]) if self.workspace and self.task and not self.task["demo"] else ""
        if not root:
            root = QFileDialog.getExistingDirectory(self, "选择同一项目的任务目录", str(Path.home()))
        if not root:
            return
        dialog, layout = base_dialog(self, "批量新建任务", "一行一个任务。任务会出现在同一个项目下。", 705, 455)
        location = flexible_label(root)
        location.setToolTip(root)
        layout.addWidget(location)
        editor = QPlainTextEdit()
        editor.setAccessibleName("每行一项任务")
        editor.setPlaceholderText("检查登录页的边界情况\n梳理数据导入流程\n调整项目的错误提示")
        layout.addWidget(editor, 1)
        controls = QHBoxLayout()
        mode = Combo()
        mode.setAccessibleName("批量任务模式")
        for name, title in MODES.items():
            mode.addItem(title, name)
        if self.task:
            mode.setCurrentIndex(max(0, mode.findData(self.mode.currentData())))
        controls.addWidget(label("模式", "Muted"))
        controls.addWidget(mode)
        controls.addStretch()
        controls.addWidget(button("创建草稿", dialog.accept))
        controls.addWidget(button("创建并运行", lambda: dialog.done(2), "Primary"))
        layout.addLayout(controls)
        choice = dialog.exec()
        prompts = [line.strip() for line in editor.toPlainText().splitlines() if line.strip()]
        if not choice or not prompts:
            return
        keys = []
        try:
            for prompt in prompts:
                item = self.engine.create(root, draft=prompt, mode=mode.currentData())
                keys.append(item["id"])
        except (ValueError, OSError) as exc:
            message(self, exc, "无法创建任务")
        if not keys:
            return
        self.show_archived = False
        self.archive_toggle.setText("归档")
        self.filter.clear()
        self.open_task(keys[0])
        self.select_task_rows(keys)
        if choice == 2:
            self.run_selected()

    def new_demo(self):
        self.make_task(demo=True)
        self.prompt.setPlainText("把示例项目的问候语改成中文，运行测试，再用 MCP 检查一段 JSON。")
        self.send()

    def open_task(self, identifier, keep_selection=False):
        previous = self.task
        if previous:
            self.drafts[previous["id"]] = (self.prompt.toPlainText(), self.model.currentData(),
                                           self.effort.currentData(), self.mode.currentData())
        self.task = self.engine.detail(identifier)
        try:
            self.workspace = self.engine.workspace(self.task)
        except (ValueError, OSError) as exc:
            self.workspace = None
            self.statusBar().showMessage(str(exc), 6000)
        with self.engine.store.lock:
            self.engine.store.state["desktop_task"] = identifier
            self.engine.store.save()
        self.run_id, self.after, self.live, self.pending = None, 0, None, None
        run = self.engine.runs.get(identifier)
        if run and run.done:
            self.run_id, self.after = run.id, len(run.events)
        draft = self.draft_config(self.task)
        self.prompt.setPlainText(draft[0])
        self.mode.setCurrentIndex(max(0, self.mode.findData(draft[3])))
        self.update_models(draft[1], draft[2])
        self.screens.setCurrentIndex(1)
        self.project_name.setText("演示项目" if self.task["demo"] else Path(self.task["root"]).name)
        self.breadcrumb.setText("本地示例项目" if self.task["demo"] else self.task["root"])
        self.breadcrumb.setToolTip(self.task["root"])
        if not previous:
            self.project.show()
        self.paint_chat(True)
        self.project.load(self.workspace, self.task)
        self.terminal.set_project(self.task["root"] if self.workspace else None, self.engine.store,
                                  "演示项目" if self.task["demo"] else None)
        self.refresh_sidebar()
        if not keep_selection:
            self.select_task_rows({identifier})
        self.update_controls()
        self.prompt.setFocus()

    def draft_config(self, task):
        if task["id"] in self.queued:
            row = self.queued[task["id"]]
            return row["prompt"], row["model"], row["effort"], row["mode"]
        return self.drafts.get(task["id"], (task.get("draft", ""), task["model"], task["effort"], task.get("mode", "agent")))

    def update_models(self, selected=None, effort=None):
        if not self.task:
            return
        current = selected or self.model.currentData() or self.task["model"]
        effort = effort or self.effort.currentData()
        available = self.engine.catalog.available()
        self.model.blockSignals(True)
        self.model.clear()
        for identifier, spec in available.items():
            provider = spec["provider"]
            if provider == "demo" and not self.task["demo"]:
                continue
            suffix = " · 缺少 Key" if provider in PROVIDERS and self.engine.store.key_status(provider) == "missing" else ""
            self.model.addItem(spec["name"] + suffix, identifier)
            self.model.setItemData(self.model.count() - 1, identifier, Qt.ItemDataRole.ToolTipRole)
        if current and self.model.findData(current) < 0:
            self.model.addItem(MODEL_NAMES.get(current, current) + " · 当前不可用", current)
        self.model.setCurrentIndex(max(0, self.model.findData(current)))
        self.model.blockSignals(False)
        self.update_effort(effort)

    def update_effort(self, selected=None):
        model = self.model.currentData()
        self.effort.clear()
        spec = self.engine.catalog.available().get(model)
        if spec:
            for key in spec["efforts"]:
                self.effort.addItem(EFFORTS[key], key)
            key = selected if selected in spec["efforts"] else spec["default"]
            self.effort.setCurrentIndex(self.effort.findData(key))
        self.update_controls()

    def paint_chat(self, follow=False):
        if self.task:
            self.task_title.setText(self.task["title"])
            self.task_title.setToolTip(self.task["title"])
            names = {**MODEL_NAMES, **{key: spec["name"] for key, spec in self.engine.catalog.available().items()}}
            self.transcript.render(self.task["messages"], self.live, names, follow)

    def update_controls(self):
        task = self.task
        model = self.model.currentData()
        running = bool(task and self.engine.active(task["id"]))
        queued = bool(task and task["id"] in self.queued)
        archived = bool(task and task.get("archived"))
        provider = self.engine.catalog.available().get(model, {}).get("provider")
        ready = provider == "demo" or (provider in PROVIDERS and self.engine.store.key_status(provider) != "missing")
        self.send_btn.setEnabled(bool(self.workspace and task and ready and not running and not queued and not archived))
        self.stop_btn.setText("取消排队" if queued else "停止")
        self.stop_btn.setVisible(running or queued)
        self.prompt.setReadOnly(queued)
        for widget in (self.model, self.effort, self.mode, self.attach_btn):
            widget.setEnabled(bool(task and not running and not queued and not archived))
        for widget in (self.branch_tag, self.terminal_btn, self.review_btn):
            widget.setEnabled(bool(self.workspace))
        self.approval.setVisible(bool(self.pending and running))
        self.update_context()
        self.subtitle.setText("已归档 · 在任务菜单恢复" if archived else "等待审批" if self.pending else
                              "正在处理…" if running else "排队中 · 此目录空闲后自动运行" if queued else
                              "当前模型不可用，请选择其他模型" if task and not provider else
                              "请在设置中保存选中供应商的 API Key"
                              if task and not ready else "Ctrl + Enter 发送")

    def send(self):
        if not self.task or not self.send_btn.isEnabled():
            return
        if not self.prompt.toPlainText().strip():
            self.prompt.setFocus()
            return
        self.run_drafts([self.task["id"]])

    def run_selected(self):
        self.run_drafts(self.selected_tasks())

    def run_drafts(self, identifiers):
        started = queued = skipped = 0
        errors = []
        for identifier in identifiers:
            if identifier in self.queued or self.engine.active(identifier):
                skipped += 1
                continue
            try:
                task = self.engine.detail(identifier)
                if task.get("archived"):
                    skipped += 1
                    continue
                request = self.task_request(task)
                if not request:
                    skipped += 1
                    continue
                try:
                    self.engine.start(request)
                except WorkspaceBusy:
                    self.enqueue(task, request)
                    queued += 1
                else:
                    self.did_start(identifier)
                    started += 1
            except (ValueError, OSError) as exc:
                errors.append(str(exc))
        self.update_controls()
        self.refresh_sidebar()
        if errors:
            message(self, "\n".join(dict.fromkeys(errors)) + (f"\n已启动 {started} 项，排队 {queued} 项。" if started or queued else ""),
                    "任务启动结果")
        if started or queued or skipped:
            self.statusBar().showMessage(f"已启动 {started} 项 · 排队 {queued} 项" +
                                         (f" · 已运行或无草稿 {skipped} 项" if skipped else ""), 6500)

    def task_request(self, task):
        identifier = task["id"]
        if self.task and self.task["id"] == identifier:
            text, model = self.prompt.toPlainText().strip(), self.model.currentData()
            effort, mode = self.effort.currentData(), self.mode.currentData()
        else:
            text, model, effort, mode = self.draft_config(task)
            text = text.strip()
        if not text:
            return None
        spec = self.engine.catalog.available().get(model)
        if not spec:
            raise ValueError("当前模型不可用，请在任务中重新选择模型。")
        provider = spec["provider"]
        last = self.engine.previous_provider(task["messages"])
        share = False
        if last and provider in PROVIDERS and last != provider:
            share = confirm(self, "切换模型供应商",
                            f"将「{task['title']}」的近期对话和附加内容发送给 {PROVIDERS[provider]['name']}？")
            if not share:
                return None
        context = "\n\n".join(f"Project context ({name}):\n{value}" for name, value in self.attachments.get(identifier, {}).items())
        return {"task": identifier, "model": model, "effort": effort, "mode": mode, "prompt": text,
                "context": context, "images": list(self.images.get(identifier, [])), "share_context": share}

    def enqueue(self, task, request):
        identifier = task["id"]
        with self.engine.store.lock:
            saved = self.engine.task(identifier)
            saved["draft"] = self.engine.store.redact(request["prompt"])
            saved["model"], saved["effort"], saved["mode"] = request["model"], request["effort"], request["mode"]
            if not saved["messages"] and not saved.get("title_custom"):
                saved["title"] = " ".join(saved["draft"].split())[:46]
            self.engine.store.save()
        self.queued[identifier] = request
        self.queue_signature = None
        if self.task and self.task["id"] == identifier:
            self.task = self.engine.detail(identifier)
            self.paint_chat()

    def did_start(self, identifier):
        self.queued.pop(identifier, None)
        self.drafts.pop(identifier, None)
        self.attachments.pop(identifier, None)
        self.images.pop(identifier, None)
        if self.task and self.task["id"] == identifier:
            self.prompt.clear()
            self.task = self.engine.detail(identifier)
            self.run_id, self.after, self.live, self.pending = None, 0, None, None
            self.paint_chat(True)

    def stop_task(self):
        if self.task:
            self.stop_tasks([self.task["id"]])

    def stop_selected(self):
        self.stop_tasks(self.selected_tasks())

    def stop_tasks(self, identifiers):
        for identifier in identifiers:
            request = self.queued.pop(identifier, None)
            if request:
                self.drafts[identifier] = (request["prompt"], request["model"], request["effort"], request["mode"])
            run = self.engine.runs.get(identifier)
            if run and not run.done:
                run.cancel()
        self.update_controls()
        self.refresh_sidebar()

    def start_queued(self):
        if not self.queued:
            return
        running = frozenset(run.id for run in self.engine.runs.values() if not run.done)
        if running == self.queue_signature:
            return
        self.queue_signature = running
        for key, request in list(self.queued.items()):
            try:
                self.engine.start(request)
            except WorkspaceBusy:
                continue
            except (ValueError, OSError) as exc:
                self.queued.pop(key, None)
                self.drafts[key] = (request["prompt"], request["model"], request["effort"], request["mode"])
                self.statusBar().showMessage("排队任务保留为草稿 · " + str(exc), 12000)
            else:
                self.did_start(key)
        self.update_controls()

    def tick(self):
        while not self.jobs.empty():
            finish, value, error = self.jobs.get_nowait()
            finish(value, error)
        self.start_queued()
        self.refresh_sidebar()
        rows, _ = self.engine.plugins.snapshot()
        connected = sum(item["state"] == "connected" for item in rows)
        self.plugin_button.setText(f"◇  连接与插件    {connected}" if connected else "◇  连接与插件")
        if self.wechat:
            count = len(self.wechat.messages())
            self.wechat_button.setText("○  微信 ClawBot" + (f"    {count}" if count else ""))
        if not self.task:
            return
        run = self.engine.runs.get(self.task["id"])
        if not run:
            return
        if run.id != self.run_id:
            self.run_id, self.after = run.id, 0
            self.live = {"role": "assistant", "model": run.model, "mode": run.mode, "text": "", "activity": []}
        state = run.snapshot(self.after)
        if state["done"] and state["count"] == self.after and self.live is None:
            return
        self.after = state["count"]
        changed = activity = False
        for item in state["events"]:
            if item["type"] == "text":
                self.live["text"] += item["text"]
                changed = True
            elif item["type"] == "result":
                self.live["activity"].append(item)
                activity = True
        if state["approval"] != self.pending:
            self.pending = state["approval"]
            if self.pending:
                self.approval_text.setText(self.pending["title"] + (" · " + self.pending["path"] if self.pending.get("path") else ""))
            self.update_controls()
        if state["done"]:
            self.live = None
            self.task = self.engine.detail(run.task)
            self.paint_chat()
            self.refresh_project()
            self.update_controls()
        else:
            if changed:
                self.paint_chat()
            if activity:
                self.project.paint_activity(self.task, self.live["activity"])

    def review_pending(self):
        approval = self.pending
        run = self.engine.runs.get(self.task["id"]) if self.task else None
        if not approval or not run:
            return
        if approval["kind"] == "edit":
            title, content = approval["path"], approval["diff"]
        elif approval["kind"] == "command":
            title, content = "运行项目命令", approval["cwd"] + "\n\n" + approval["command"]
        else:
            title = approval["plugin"] + " / " + approval["tool"]
            content = approval["args"] + "\n\n批准后，工具返回的内容会交给本轮所选模型。"

        def decide(answer):
            try:
                run.decide(approval["id"], answer)
            except ValueError as exc:
                message(self, exc)
        diff_dialog(self, title, content, self.p, decide)

    def toggle_terminal(self):
        if self.workspace:
            self.terminal.setVisible(self.terminal.isHidden())
            if self.terminal.isVisible():
                self.center.setSizes([500, 225])
                self.terminal.entry.setFocus()
                QTimer.singleShot(30, lambda: self.transcript.verticalScrollBar().setValue(
                    self.transcript.verticalScrollBar().maximum()))

    def toggle_project(self):
        if self.workspace:
            self.project.setVisible(self.project.isHidden())
            if self.project.isVisible():
                self.work_area.setSizes([715, 355])

    def refresh_project(self):
        if self.task:
            self.project.load(self.workspace, self.task, live=self.live["activity"] if self.live else ())

    def attach_menu(self):
        menu = QMenu(self)
        add = menu.addAction("添加项目文件…")
        notes = menu.addAction("加载项目说明 AGENTS.md")
        picture = menu.addAction("添加图片…")
        clipboard = menu.addAction("粘贴剪贴板中的截图")
        model = self.model.currentData()
        vision = self.engine.catalog.available().get(model, {}).get("images", model in IMAGE_MODELS)
        picture.setEnabled(vision)
        clipboard.setEnabled(vision)
        chosen = menu.exec(self.attach_btn.mapToGlobal(QPoint(0, self.attach_btn.height())))
        if chosen is add:
            paths, _ = QFileDialog.getOpenFileNames(self, "选择上下文文件", str(self.workspace.root))
            for path in paths[:4]:
                try:
                    relative = Path(path).resolve().relative_to(self.workspace.root).as_posix()
                    self.attach_file(relative)
                except ValueError:
                    self.statusBar().showMessage("请选择当前项目目录内的文件。", 5000)
        elif chosen is notes:
            self.attach_file("AGENTS.md")
        elif chosen is picture:
            files, _ = QFileDialog.getOpenFileNames(self, "选择图片", str(self.workspace.root),
                                                    "图片 (*.png *.jpg *.jpeg *.webp *.bmp)")
            for file in files[:3]:
                reader = QImageReader(file)
                reader.setAutoTransform(True)
                size = reader.size()
                if size.isValid() and max(size.width(), size.height()) > 2100:
                    reader.setScaledSize(size.scaled(2100, 2100, Qt.AspectRatioMode.KeepAspectRatio))
                self.add_image(reader.read(), Path(file).name)
        elif chosen is clipboard:
            self.add_image(QApplication.clipboard().image(), "剪贴板截图.jpg")

    def add_image(self, image, name):
        if image.isNull():
            message(self, "没找到可用的图片。")
            return
        pictures = self.images.setdefault(self.task["id"], [])
        if len(pictures) >= 3:
            message(self, "一轮最多附加三张图片。")
            return
        image = image.scaled(2100, 2100, Qt.AspectRatioMode.KeepAspectRatio,
                             Qt.TransformationMode.SmoothTransformation) if max(image.width(), image.height()) > 2100 else image
        content = QByteArray()
        buffer = QBuffer(content)
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        if not image.convertToFormat(QImage.Format.Format_RGB888).save(buffer, "JPG", 85):
            message(self, "无法编码图片。")
            return
        buffer.close()
        encoded = base64.b64encode(bytes(content)).decode("ascii")
        if len(encoded) > 1600000:
            message(self, "图片超过本轮大小限制，请缩小尺寸后重试。")
            return
        pictures.append({"name": name[:170], "mime": "image/jpeg", "base64": encoded})
        self.update_context()

    def attach_file(self, path):
        try:
            text = self.workspace.read_text(self.workspace.resolve(path))
            items = self.attachments.setdefault(self.task["id"], {})
            if len(items) >= 4 and path not in items:
                raise ValueError("每次可附加最多四个文件。")
            items[path] = text[:8800] + ("\n[长文件仅附前 8800 字符]" if len(text) > 8800 else "")
            self.update_context()
        except (ValueError, OSError) as exc:
            message(self, exc, "无法附加文件")

    def update_context(self):
        items = self.attachments.get(self.task["id"], {}) if self.task else {}
        pictures = self.images.get(self.task["id"], []) if self.task else []
        names = "  ·  ".join([*items, *(pic["name"] for pic in pictures)])
        self.context_text.setText(names)
        self.context_text.setToolTip(names)
        self.context_text.setVisible(bool(names))
        self.clear_context.setVisible(bool(names))

    def drop_context(self):
        if self.task:
            self.attachments.pop(self.task["id"], None)
            self.images.pop(self.task["id"], None)
            self.update_context()

    def import_prompt(self, text):
        if not self.task:
            self.pick_project()
        if not self.task:
            return False
        if self.prompt.toPlainText().strip() and not confirm(self, "替换草稿", "用选中的微信消息替换当前草稿？"):
            return False
        self.prompt.setPlainText(text)
        self.prompt.setFocus()
        return True

    def last_answer(self):
        return next((item["text"] for item in reversed(self.task["messages"])
                     if item["role"] == "assistant" and item["text"]), "") if self.task else ""

    def closeEvent(self, event):
        if (self.engine.active() or self.queued) and not confirm(
            self, "退出工作台", "退出会停止运行中的任务。排队任务会保存为草稿。继续退出？"
        ):
            event.ignore()
            return
        self.timer.stop()
        for run in self.engine.runs.values():
            if not run.done:
                run.cancel()
        self.terminal.stop()
        self.engine.plugins.close()
        if self.wechat:
            self.wechat.close()
        event.accept()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, default=local_data())
    parser.add_argument("--demo", action="store_true")
    parser.add_argument("--wechat", action="store_true")
    args = parser.parse_args()
    qt_app = QApplication([sys.argv[0]])
    qt_app.setApplicationName(TITLE)
    try:
        args.data_dir.mkdir(parents=True, exist_ok=True)
        with instance_lock(args.data_dir):
            engine = App(args.data_dir, desktop_defaults=True)
            window = Workbench(engine)
            window.show()
            if args.demo:
                QTimer.singleShot(400, window.new_demo)
            if args.wechat:
                QTimer.singleShot(400, window.settings.wechat)
            qt_app.exec()
    except (OSError, RuntimeError, ValueError) as exc:
        message(None, exc)


if __name__ == "__main__":
    main()

from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QPoint, Qt
from PySide6.QtWidgets import (QFormLayout, QHBoxLayout, QInputDialog, QLineEdit, QListWidget,
                               QMenu, QSizePolicy, QTabWidget, QTreeWidget, QTreeWidgetItem,
                               QVBoxLayout, QWidget)

import project_git
from qt_settings import base_dialog, confirm, message
from qt_theme import button, label
from qt_widgets import CodeView, diff_dialog
from workspace import SKIPPED, sensitive


ROLE = Qt.ItemDataRole.UserRole
TOOLS = {"list_files": "列出文件", "read_file": "读取文件", "search_project": "搜索项目", "git_status": "Git 状态",
         "edit_file": "修改文件", "create_file": "创建文件", "run_command": "运行命令"}


class ProjectPanel(QWidget):
    def __init__(self, owner):
        super().__init__()
        self.owner = owner
        self.workspace = self.task = self.git = None
        self.activities, self.changes = [], []
        self.latest = 0
        self.setMinimumWidth(263)
        body = QVBoxLayout(self)
        body.setContentsMargins(13, 16, 13, 11)
        header = QHBoxLayout()
        header.addWidget(label("项目", "Brand"), 1)
        header.addWidget(button("刷新", owner.refresh_project, "Pill"))
        body.addLayout(header)
        self.tabs = QTabWidget()
        body.addWidget(self.tabs, 1)
        self.build_files()
        self.build_review()
        self.build_activity()

    def build_files(self):
        pane = QWidget()
        layout = QVBoxLayout(pane)
        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setIndentation(13)
        self.tree.setAccessibleName("项目文件树")
        self.tree.itemExpanded.connect(self.open_directory)
        self.tree.itemSelectionChanged.connect(self.preview_file)
        self.tree.setMinimumHeight(125)
        layout.addWidget(self.tree, 2)
        self.file_title = label("选择文件预览", "Muted")
        self.file_title.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        layout.addWidget(self.file_title)
        self.preview = CodeView(self.owner.p)
        layout.addWidget(self.preview, 3)
        layout.addWidget(button("添加到任务上下文", self.attach, "Pill"), alignment=Qt.AlignmentFlag.AlignLeft)
        self.tabs.addTab(pane, "文件")

    def build_review(self):
        pane = QWidget()
        layout = QVBoxLayout(pane)
        self.change_list = QListWidget()
        self.change_list.setAccessibleName("工作树和 Agent 改动")
        self.change_list.itemDoubleClicked.connect(lambda _: self.review())
        layout.addWidget(self.change_list, 1)
        self.git_summary = label("暂无 Git 工作树状态", "Muted")
        self.git_summary.setWordWrap(True)
        layout.addWidget(self.git_summary)
        buttons = QHBoxLayout()
        buttons.setSpacing(4)
        buttons.addWidget(button("查看差异", self.review, "Pill"))
        buttons.addWidget(button("撤销", self.undo, "Pill"))
        self.git_btn = button("Git 操作", self.change_menu, "Pill")
        buttons.addWidget(self.git_btn)
        buttons.addStretch()
        layout.addLayout(buttons)
        self.tabs.addTab(pane, "改动")

    def build_activity(self):
        pane = QWidget()
        layout = QVBoxLayout(pane)
        self.log_list = QListWidget()
        self.log_list.setAccessibleName("Agent 工具活动")
        self.log_list.currentRowChanged.connect(self.select_log)
        layout.addWidget(self.log_list, 2)
        self.log = CodeView(self.owner.p)
        self.log.setAccessibleName("工具输出")
        layout.addWidget(self.log, 3)
        self.tabs.addTab(pane, "活动")

    def load(self, workspace, task, live=()):
        old_root = self.workspace.root if self.workspace else None
        self.workspace, self.task = workspace, task
        new_root = workspace.root if workspace else None
        self.latest += 1
        number = self.latest
        self.tree.clear()
        self.preview.clear()
        self.file_title.setText("选择文件预览")
        if old_root != new_root:
            self.git = None
            self.owner.branch_tag.setText("Git")
        self.paint_activity(task, live)
        self.paint_changes()
        if not workspace:
            return
        self.populate(self.tree.invisibleRootItem(), ".")

        def done(value, error):
            if number != self.latest:
                return
            self.git = value
            self.owner.branch_tag.setText("⎇  " + value[0][:23] if value and value[0] else "Git")
            self.paint_changes()
            if error:
                self.git_summary.setText(error)
        self.owner.job(lambda: project_git.inventory(workspace.root), done)

    def populate(self, parent, relative):
        try:
            folder = self.workspace.resolve(relative, allow_root=True)
            entries = sorted(folder.iterdir(), key=lambda p: (not p.is_dir(), p.name.casefold()))
        except (ValueError, OSError):
            return
        for path in entries[:800]:
            if path.name.casefold() in SKIPPED or sensitive(path.name):
                continue
            name = path.relative_to(self.workspace.root).as_posix()
            try:
                self.workspace.resolve(name)
            except (ValueError, OSError):
                continue
            folder = path.is_dir()
            item = QTreeWidgetItem(parent, [path.name])
            item.setData(0, ROLE, name)
            item.setData(0, ROLE + 1, folder)
            if folder:
                item.setChildIndicatorPolicy(QTreeWidgetItem.ChildIndicatorPolicy.ShowIndicator)

    def open_directory(self, item):
        if item.data(0, ROLE + 1) and not item.childCount():
            self.populate(item, item.data(0, ROLE))
            if not item.childCount():
                item.setChildIndicatorPolicy(QTreeWidgetItem.ChildIndicatorPolicy.DontShowIndicatorWhenChildless)

    def chosen_file(self):
        node = self.tree.currentItem()
        return node.data(0, ROLE) if node and self.workspace and not node.data(0, ROLE + 1) else None

    def preview_file(self):
        name = self.chosen_file()
        if not name:
            return
        self.file_title.setText(name)
        self.file_title.setToolTip(name)
        try:
            source = self.workspace.read_text(self.workspace.resolve(name))
            self.preview.show_source(source[:96000])
        except (ValueError, OSError) as exc:
            self.preview.setPlainText(str(exc))

    def attach(self):
        name = self.chosen_file()
        if name:
            self.owner.attach_file(name)

    def paint_activity(self, task, live=()):
        self.activities = [entry for item in task["messages"] for entry in item.get("activity", [])] + list(live)
        self.log_list.blockSignals(True)
        self.log_list.clear()
        for entry in reversed(self.activities):
            name = TOOLS.get(entry["name"], entry["name"])
            self.log_list.addItem(name + ("  ·  " + entry["path"] if entry.get("path") else ""))
        self.log_list.blockSignals(False)
        if self.activities:
            self.log_list.setCurrentRow(0)
        else:
            self.log.clear()

    def select_log(self, index):
        if 0 <= index < len(self.activities):
            self.log.setPlainText(self.activities[-1-index]["output"])

    def paint_changes(self):
        self.changes = []
        self.change_list.clear()
        for change in reversed(self.task["changes"] if self.task else []):
            status = {"applied": "已应用", "undone": "已撤销", "conflict": "冲突"}.get(change["status"], change["status"])
            self.change_list.addItem("Agent  ·  " + change["path"] + "  ·  " + status)
            self.changes.append(("agent", change))
        entries = self.git[1] if self.git else []
        for state, name, previous in entries:
            if any(sensitive(part) for part in Path(name).parts):
                continue
            self.change_list.addItem(f"{state.strip()}  ·  {name}")
            self.changes.append(("git", (state, name, previous)))
        self.git_summary.setText(f"{self.git[0]} · {len(entries)} 个 Git 改动" if self.git and self.git[0] else "暂无 Git 工作树状态")

    def selected(self):
        index = self.change_list.currentRow()
        return self.changes[index] if 0 <= index < len(self.changes) else (None, None)

    def review(self):
        kind, item = self.selected()
        if kind == "agent":
            diff_dialog(self.owner, item["path"], item["diff"], self.owner.p)
        elif kind == "git":
            state, name, _ = item
            workspace = self.workspace

            def finished(value, error):
                if error:
                    message(self.owner, error, "Git")
                elif self.workspace is workspace:
                    diff_dialog(self.owner, name, value or "此文件没有可显示的文本差异。", self.owner.p)
            self.owner.job(lambda: project_git.difference(workspace, name, state), finished)

    def undo(self):
        kind, item = self.selected()
        if kind != "agent" or item["status"] != "applied":
            message(self.owner, "选择一项已应用的 Agent 改动。")
            return
        if confirm(self.owner, "撤销改动", f"撤销对 {item['path']} 的这次改动？"):
            try:
                self.owner.engine.undo(self.task["id"], item["id"])
                self.owner.task = self.owner.engine.detail(self.task["id"])
                self.owner.refresh_project()
            except (ValueError, OSError) as exc:
                message(self.owner, exc)

    def git_menu(self, anchor):
        if not self.workspace:
            return
        menu = QMenu(self)
        review = menu.addAction("查看工作树改动")
        worktree = menu.addAction("从当前 HEAD 创建工作树…")
        worktree.setEnabled(bool(self.git and self.git[0]))
        action = menu.exec(anchor.mapToGlobal(QPoint(0, anchor.height())))
        if action is review:
            self.show()
            self.tabs.setCurrentIndex(1)
            self.owner.refresh_project()
        elif action is worktree:
            self.make_worktree()

    def change_menu(self):
        if not self.workspace or not self.git or not self.git[0]:
            message(self.owner, "当前目录没有 Git 工作树。")
            return
        kind, item = self.selected()
        menu = QMenu(self)
        stage = menu.addAction("暂存选中文件")
        unstage = menu.addAction("取消暂存")
        commit = menu.addAction("提交暂存内容到本地…")
        stage.setEnabled(kind == "git")
        unstage.setEnabled(kind == "git" and item[0][0] not in {" ", "?"})
        action = menu.exec(self.git_btn.mapToGlobal(QPoint(0, self.git_btn.height())))
        if not action:
            return
        root = self.workspace.root
        if action is commit:
            if self.owner.engine.active(self.task["id"]):
                message(self.owner, "请先结束当前项目的任务。")
                return
            text, ok = QInputDialog.getText(self.owner, "创建本地提交", "提交说明")
            if ok and text.strip() and confirm(self.owner, "确认提交", "将此 Git 工作树的全部暂存内容创建为一条本地提交？"):
                self.owner.job(lambda: project_git.git(root, "commit", "-m", text.strip(), timeout=40), self.git_done)
            return
        state, name, previous = item
        try:
            names = [self.workspace.resolve(value).relative_to(root).as_posix() for value in (name, previous) if value]
        except (ValueError, OSError) as exc:
            message(self.owner, exc)
            return
        args = ("add", "-A") if action is stage else ("reset", "-q", "HEAD")
        self.owner.job(lambda: project_git.git(root, *args, "--", *names), self.git_done)

    def git_done(self, value, error):
        if error:
            message(self.owner, error, "Git")
        else:
            self.owner.statusBar().showMessage(value.strip().splitlines()[0] if value.strip() else "Git 操作完成", 5500)
        self.owner.refresh_project()

    def make_worktree(self):
        root = self.workspace.root
        original_project = self.task.get("project_root", str(root))
        window, form = base_dialog(self.owner, "创建 Git 工作树",
                                   "从当前 HEAD 创建独立目录。未提交改动留在当前目录。", 647, 276)
        fields = QFormLayout()
        suffix = datetime.now().strftime("%m%d-%H%M%S")
        branch = QLineEdit("agent/" + suffix)
        destination = QLineEdit(str(root.parent / (root.name + "-worktrees") / suffix))
        fields.addRow("新分支", branch)
        fields.addRow("工作树目录", destination)
        form.addLayout(fields)
        form.addStretch()
        form.addWidget(button("创建并打开", window.accept, "Primary"), alignment=Qt.AlignmentFlag.AlignRight)
        if not window.exec():
            return
        name, target = branch.text().strip(), destination.text().strip()
        if not target or not Path(target).is_absolute():
            message(self.owner, "请输入工作树的绝对路径。")
            return
        self.owner.statusBar().showMessage("正在创建工作树…")

        def done(path, error):
            if error:
                message(self.owner, error, "Git 工作树")
            else:
                self.owner.make_task(str(path), project_root=original_project)
            self.owner.statusBar().showMessage("本机工作台")
        self.owner.job(lambda: project_git.add_worktree(root, name, target), done)

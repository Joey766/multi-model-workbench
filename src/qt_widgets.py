import codecs
from html import escape
from itertools import zip_longest
from pathlib import Path
import os
import re
import shutil

from PySide6.QtCore import QProcess, QProcessEnvironment, Qt
from PySide6.QtGui import QColor, QFont, QSyntaxHighlighter, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (QDialog, QHBoxLayout, QLineEdit, QPlainTextEdit,
                               QSplitter, QTextBrowser, QVBoxLayout, QWidget)

from qt_theme import button, label
from workspace import child_environment


def inline(text, palette):
    parts = re.split(r"(\x60[^\x60\n]+\x60|\*\*[^*\n]+\*\*)", text)
    return "".join(
        f'<span style="font-family:Consolas;background-color:{palette["raised"]}">{escape(x[1:-1])}</span>'
        if x.startswith("\x60") and x.endswith("\x60") and len(x) > 2 else
        "<b>" + escape(x[2:-2]) + "</b>" if x.startswith("**") and x.endswith("**") and len(x) > 4 else escape(x)
        for x in parts)


def markup(text, p):
    output, lines = [], []
    in_code = False
    for line in text.splitlines():
        if line.startswith("\x60\x60\x60"):
            if in_code:
                output.append(code_block(lines, p))
                lines = []
            in_code = not in_code
        elif in_code:
            lines.append(line)
        elif not line.strip():
            output.append("<br>")
        elif line.startswith(("# ", "## ", "### ")):
            output.append("<p><b>" + inline(line.lstrip("# "), p) + "</b></p>")
        elif line.startswith(("- ", "* ")):
            output.append("　•　" + inline(line[2:], p) + "<br>")
        else:
            output.append(inline(line, p) + "<br>")
    if lines:
        output.append(code_block(lines, p))
    return "".join(output)


def code_block(lines, p):
    return (f'<pre style="background-color:{p["code"]};color:{p["text"]};font-family:Consolas;'
            f'white-space:pre-wrap">{escape(chr(10).join(lines))}</pre>')


class Transcript(QTextBrowser):
    def __init__(self, palette):
        super().__init__()
        self.p = palette
        self.setObjectName("Conversation")
        self.setAccessibleName("任务对话")
        self.setOpenLinks(False)
        self.document().setDocumentMargin(15)

    def loadResource(self, resource_type, name):
        return None

    def render(self, messages, live, titles, follow=False):
        p = self.p
        position = self.verticalScrollBar().value()
        tail = position + 65 >= self.verticalScrollBar().maximum()
        rows = []
        for item in [*messages, *([live] if live else [])]:
            user = item["role"] == "user"
            name = "你" if user else titles.get(item.get("model"), item.get("model", "Agent"))
            mode = {"plan": "计划", "ask": "问答"}.get(item.get("mode"))
            tag = "  ·  " + mode if mode else ""
            status = item.get("info", "")
            display = markup(item.get("text", ""), p)
            conversation = (f'<table width="100%" cellspacing="0" cellpadding="13" bgcolor="{p["surface"]}">'
                            f'<tr><td>{display}</td></tr></table>') if user else display
            refs = []
            if item.get("context"):
                refs.append("附带项目上下文")
            if item.get("image_names"):
                refs.append("图片 " + " · ".join(item["image_names"]))
            hint = '<p style="color:' + p["muted"] + '">' + escape(" · ".join(refs)) + "</p>" if refs else ""
            rows.append(f'<p style="margin-top:18px;color:{p["accent"]}"><b>{escape(name+tag)}</b></p>'
                        + conversation + hint +
                        (f'<p style="color:{p["note"]}">{escape(status)}</p>' if status else ""))
        if not rows:
            rows.append(f'<p style="color:{p["muted"]}">描述你要完成的工作，或先让 Agent 检查项目。</p>')
        self.setHtml(f'<body style="font-size:14px;color:{p["text"]};line-height:155%">{"".join(rows)}</body>')
        self.verticalScrollBar().setValue(self.verticalScrollBar().maximum() if follow or tail else position)


class Highlighter(QSyntaxHighlighter):
    def __init__(self, document, p, diff=False):
        super().__init__(document)
        self.diff = diff
        self.rules = []
        rules = [(r"\b(?:def|class|from|import|return|async|await|for|while|if|elif|else|try|except|with|let|const|function|pub|fn|struct|var|new|static|using|private)\b", p["accent"]),
                 (r"\b(?:true|false|null|True|False|None|[0-9]+)\b", p["note"]),
                 (r"""(?:"[^"\n]*"|'[^'\n]*')""", p["success"]),
                 (r"(?://|#).*", p["muted"])]
        for pattern, color in rules:
            fmt = QTextCharFormat()
            fmt.setForeground(QColor(color))
            self.rules.append((re.compile(pattern), fmt))
        self.added = QTextCharFormat()
        self.added.setBackground(QColor(p["added"]))
        self.removed = QTextCharFormat()
        self.removed.setBackground(QColor(p["removed"]))
        self.hunk = QTextCharFormat()
        self.hunk.setForeground(QColor(p["accent"]))

    def highlightBlock(self, text):
        if self.diff:
            if text.startswith(("+", "-")):
                self.setFormat(0, len(text), self.added if text[0] == "+" else self.removed)
            elif text.startswith("@@"):
                self.setFormat(0, len(text), self.hunk)
            return
        for pattern, fmt in self.rules:
            for match in pattern.finditer(text):
                self.setFormat(match.start(), len(match.group()), fmt)


class CodeView(QPlainTextEdit):
    def __init__(self, p, diff=False):
        super().__init__()
        self.setReadOnly(True)
        self.setFont(QFont("Cascadia Mono", 10))
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.setAccessibleName("代码差异" if diff else "文件预览")
        self.highlighter = Highlighter(self.document(), p, diff)

    def show_source(self, text):
        self.setPlainText("\n".join(f"{line:>4}  {value}" for line, value in enumerate(text.splitlines(), 1)))


def split_diff(diff):
    left, right, removed, added = [], [], [], []
    before, after = 0, 0

    def flush():
        for a, b in zip_longest(removed, added, fillvalue=""):
            left.append(a)
            right.append(b)
        removed.clear()
        added.clear()

    for line in diff.splitlines():
        if match := re.match(r"@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@", line):
            flush()
            before, after = (int(value) for value in match.groups())
            left.append(line)
            right.append(line)
        elif not before and not after:
            continue
        elif line.startswith("-"):
            removed.append(f"-{before:>5}  {line[1:]}")
            before += 1
        elif line.startswith("+"):
            added.append(f"+{after:>5}  {line[1:]}")
            after += 1
        elif line.startswith(" "):
            flush()
            left.append(f" {before:>5}  {line[1:]}")
            right.append(f" {after:>5}  {line[1:]}")
            before += 1
            after += 1
    flush()
    if not left:
        return diff, diff
    return "\n".join(left), "\n".join(right)


class DiffView(QWidget):
    def __init__(self, p):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        pair = QSplitter(Qt.Orientation.Horizontal)
        layout.addWidget(pair)
        boxes = []
        for title in ("修改前", "修改后"):
            panel = QWidget()
            side = QVBoxLayout(panel)
            side.setContentsMargins(0, 0, 0, 0)
            side.addWidget(label(title, "Muted"))
            view = CodeView(p, True)
            side.addWidget(view)
            boxes.append(view)
            pair.addWidget(panel)
        self.left, self.right = boxes
        self.left.verticalScrollBar().valueChanged.connect(self.right.verticalScrollBar().setValue)
        self.right.verticalScrollBar().valueChanged.connect(self.left.verticalScrollBar().setValue)
        pair.setSizes([1, 1])

    def show_diff(self, value):
        left, right = split_diff(value)
        self.left.setPlainText(left)
        self.right.setPlainText(right)


def diff_dialog(parent, title, value, p, decide=None, details=None):
    dialog = QDialog(parent)
    dialog.setWindowTitle(title)
    dialog.resize(980, 565)
    layout = QVBoxLayout(dialog)
    layout.setContentsMargins(20, 17, 20, 18)
    layout.addWidget(label(title, "Title"))
    if details:
        layout.addWidget(label(details, "Muted"))
    view = DiffView(p) if value.startswith(("---", "diff --git", "@@")) else CodeView(p)
    if isinstance(view, DiffView):
        view.show_diff(value)
    else:
        view.setPlainText(value)
    layout.addWidget(view)
    footer = QHBoxLayout()
    footer.addStretch()
    if decide:
        def choose(approved):
            decide(approved)
            dialog.accept()
        footer.addWidget(button("拒绝", lambda: choose(False)))
        footer.addWidget(button("批准", lambda: choose(True), "Primary"))
    else:
        footer.addWidget(button("关闭", dialog.accept))
    layout.addLayout(footer)
    dialog.exec()


class CommandInput(QLineEdit):
    def __init__(self):
        super().__init__()
        self.history, self.index = [], 0

    def keyPressEvent(self, event):
        if event.key() in {Qt.Key.Key_Up, Qt.Key.Key_Down} and self.history:
            self.index = max(0, min(len(self.history), self.index + (-1 if event.key() == Qt.Key.Key_Up else 1)))
            self.setText(self.history[self.index] if self.index < len(self.history) else "")
            return
        super().keyPressEvent(event)

    def save(self, command):
        self.history = (self.history + [command])[-100:]
        self.index = len(self.history)


class Terminal(QWidget):
    def __init__(self, p, refresh):
        super().__init__()
        self.setAccessibleName("项目终端")
        self.refresh = refresh
        self.root = None
        self.store = None
        self.process = QProcess(self)
        self.process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        self.process.readyReadStandardOutput.connect(self.drain)
        self.process.finished.connect(self.finished)
        self.process.errorOccurred.connect(self.process_error)
        boxes = QVBoxLayout(self)
        boxes.setContentsMargins(10, 8, 10, 8)
        header = QHBoxLayout()
        header.addWidget(label("PowerShell", "Accent"))
        self.location = label("选择项目", "Muted")
        header.addWidget(self.location, 1)
        self.halt = button("终止", self.stop, "Pill")
        self.halt.setEnabled(False)
        header.addWidget(self.halt)
        header.addWidget(button("清空输出", lambda: self.output.clear(), "Pill"))
        boxes.addLayout(header)
        self.output = CodeView(p)
        self.output.setAccessibleName("命令输出")
        self.output.document().setMaximumBlockCount(1600)
        boxes.addWidget(self.output, 1)
        self.entry = CommandInput()
        self.entry.setPlaceholderText("输入 PowerShell 命令，按 Enter 运行")
        self.entry.setAccessibleName("PowerShell 命令")
        self.entry.returnPressed.connect(self.execute)
        boxes.addWidget(self.entry)

    def set_project(self, root, store, name=None):
        self.root = root
        self.store = store
        self.location.setText((name or Path(root).name) if root else "选择项目")
        self.entry.setEnabled(bool(root) and self.process.state() == QProcess.ProcessState.NotRunning)

    def append(self, text):
        cursor = self.output.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertText(text)
        self.output.setTextCursor(cursor)
        self.output.ensureCursorVisible()

    def execute(self):
        command = self.entry.text().strip()
        if not self.root or not command or self.process.state() != QProcess.ProcessState.NotRunning:
            return
        exe = (shutil.which("pwsh") or shutil.which("powershell")) if os.name == "nt" else shutil.which("sh")
        if not exe:
            self.append("找不到 PowerShell。\n")
            return
        flags = ["-NoLogo", "-NoProfile", "-NonInteractive", "-Command"] if os.name == "nt" else ["-c"]
        if os.name == "nt":
            raw = "[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false); & {" + command + "}"
        else:
            raw = command
        safe = QProcessEnvironment()
        for name, value in child_environment().items():
            safe.insert(name, value)
        self.process.setProcessEnvironment(safe)
        self.process.setWorkingDirectory(self.root)
        self.append("\n› " + (self.store.redact(command) if self.store else command) + "\n")
        self.entry.save(command)
        self.entry.clear()
        self.entry.setEnabled(False)
        self.halt.setEnabled(True)
        self.decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self.process.start(exe, [*flags, raw])

    def drain(self):
        data = self.decoder.decode(bytes(self.process.readAllStandardOutput()))
        if data:
            self.append(self.store.redact(data) if self.store else data)

    def finished(self, code, _status):
        self.drain()
        self.append(f"\n退出码 {code}\n")
        self.entry.setEnabled(bool(self.root))
        self.halt.setEnabled(False)
        self.refresh()

    def process_error(self, reason):
        if reason == QProcess.ProcessError.FailedToStart:
            self.append("\n命令未能启动。\n")
            self.entry.setEnabled(bool(self.root))
            self.halt.setEnabled(False)

    def stop(self):
        pid = self.process.processId()
        if pid and os.name == "nt":
            QProcess.startDetached("taskkill.exe", ["/PID", str(pid), "/T", "/F"])
        elif pid:
            self.process.kill()

import argparse
import ctypes
import os
from pathlib import Path
import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from app import App, instance_lock, local_data
from providers import MODELS, PROVIDERS
from wechat import WeChat


TITLE = "多模型工作台"
EFFORTS = {"none": "关闭", "low": "低", "medium": "中", "high": "高", "xhigh": "超高", "max": "最高"}
TOOL_LABELS = {"list_files": "文件列表", "read_file": "读取文件", "search_project": "搜索项目",
               "git_status": "Git 状态", "edit_file": "修改文件", "create_file": "创建文件",
               "run_command": "运行命令"}
DARK = {"bg": "#12191d", "side": "#10161a", "card": "#1c282d", "edge": "#35464d",
        "text": "#eff6f5", "dim": "#a9b9bc", "accent": "#a6edc5", "onaccent": "#0e261d",
        "danger": "#ffb6a8", "green": "#b0efcb", "red": "#ffc1b8"}
LIGHT = {"bg": "#f7faf9", "side": "#eef3f3", "card": "#e4efed", "edge": "#a7bbbd",
         "text": "#132825", "dim": "#4a625f", "accent": "#206246", "onaccent": "#ffffff",
         "danger": "#9f3124", "green": "#17623a", "red": "#982d25"}
FONT = ("Microsoft YaHei UI", 10)
MONO = ("Cascadia Mono", 10)


def light_mode():
    if os.name == "nt":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as key:
                return bool(winreg.QueryValueEx(key, "AppsUseLightTheme")[0])
        except OSError:
            pass
    return False


def apply_theme(root, colors):
    root.configure(bg=colors["bg"])
    root.option_add("*Font", "{Microsoft YaHei UI} 10")
    root.option_add("*Listbox*Background", colors["card"])
    root.option_add("*Listbox*Foreground", colors["text"])
    style = ttk.Style(root)
    style.theme_use("clam")
    style.configure(".", background=colors["bg"], foreground=colors["text"], font=FONT,
                    fieldbackground=colors["card"], bordercolor=colors["edge"],
                    lightcolor=colors["edge"], darkcolor=colors["edge"], troughcolor=colors["side"])
    style.configure("Side.TFrame", background=colors["side"])
    style.configure("Card.TFrame", background=colors["card"])
    style.configure("Side.TLabel", background=colors["side"])
    style.configure("Dim.TLabel", foreground=colors["dim"])
    style.configure("Accent.TLabel", foreground=colors["accent"])
    style.configure("Error.TLabel", foreground=colors["danger"])
    style.configure("TButton", padding=(11, 7), background=colors["card"])
    style.map("TButton", background=[("active", colors["edge"])],
              foreground=[("disabled", colors["dim"])])
    style.configure("Primary.TButton", padding=(13, 7), background=colors["accent"], foreground=colors["onaccent"])
    style.map("Primary.TButton", background=[("disabled", colors["card"]), ("active", colors["accent"])],
              foreground=[("disabled", colors["dim"]), ("!disabled", colors["onaccent"])])
    style.configure("TCombobox", padding=5, selectbackground=colors["card"], selectforeground=colors["text"])
    style.map("TCombobox", fieldbackground=[("readonly", colors["card"])],
              selectbackground=[("readonly", colors["card"])], selectforeground=[("readonly", colors["text"])])
    style.configure("TNotebook.Tab", padding=(11, 7))
    style.map("TNotebook.Tab", background=[("selected", colors["card"])])


def set_text(widget, value):
    widget.configure(state="normal")
    widget.delete("1.0", "end")
    widget.insert("end", value)
    widget.configure(state="disabled")


def input_text(widget):
    return widget.get("1.0", "end-1c").strip()


def show_dialog(root, title, width=650, height=520):
    window = tk.Toplevel(root)
    window.title(title)
    window.transient(root)
    window.geometry(f"{width}x{height}")
    window.minsize(min(width, 480), min(height, 330))
    return window


def make_text(parent, colors, height=10, mono=False, bg=None, editable=False):
    box = ttk.Frame(parent)
    widget = tk.Text(box, height=height, wrap="word", bg=bg or colors["card"], fg=colors["text"],
                     insertbackground=colors["text"], selectbackground=colors["edge"],
                     padx=12, pady=9, relief="flat", borderwidth=0, font=MONO if mono else FONT,
                     highlightthickness=1 if editable else 0, highlightbackground=colors["edge"],
                     highlightcolor=colors["accent"], undo=editable)
    scrollbar = ttk.Scrollbar(box, command=widget.yview)
    widget.configure(yscrollcommand=scrollbar.set)
    scrollbar.pack(side="right", fill="y")
    widget.pack(side="left", fill="both", expand=True)
    if not editable:
        widget.configure(state="disabled")
    return box, widget


class Desktop:
    def __init__(self, root, app):
        self.root = root
        self.app = app
        self.colors = LIGHT if light_mode() else DARK
        self.jobs = queue.SimpleQueue()
        self.task = None
        self.activity = []
        self.change_ids = []
        self.task_list_ids = []
        self.run_id = None
        self.event_count = 0
        self.pending = None
        self.review_window = None
        self.plugin_window = None
        self.model_window = None
        self.wechat_window = None
        try:
            self.wechat = WeChat(app.store)
        except ValueError:
            messagebox.showerror("微信连接", "Windows 无法解密旧的微信授权。请使用原 Windows 账户。", parent=root)
            self.wechat = None
        self.build()
        self.reload_tasks()
        with app.store.lock:
            last = app.store.state.get("desktop_task")
        if last and last in self.task_list_ids:
            self.open_task(last)
        else:
            self.render_task()
        if self.wechat:
            self.wechat.start_poll()
        self.root.after(100, self.tick)

    def build(self):
        root = self.root
        root.title(TITLE)
        root.geometry("1250x775")
        root.minsize(925, 630)
        apply_theme(root, self.colors)
        root.protocol("WM_DELETE_WINDOW", self.close)

        panes = ttk.Panedwindow(root, orient="horizontal")
        panes.pack(fill="both", expand=True)
        sidebar = ttk.Frame(panes, width=222, padding=(16, 19), style="Side.TFrame")
        center = ttk.Frame(panes, padding=(20, 15), width=720)
        inspector = ttk.Frame(panes, width=294, padding=(12, 15))
        panes.add(sidebar, weight=0)
        panes.add(center, weight=3)
        panes.add(inspector, weight=1)

        brand = ttk.Label(sidebar, text=TITLE, style="Side.TLabel", font=("Microsoft YaHei UI", 16, "bold"))
        brand.pack(anchor="w", pady=(0, 17))
        ttk.Button(sidebar, text="＋ 打开项目", style="Primary.TButton", command=self.new_project).pack(fill="x")
        ttk.Button(sidebar, text="试用离线演示", command=self.demo).pack(fill="x", pady=(8, 16))
        ttk.Label(sidebar, text="任务记录", style="Side.TLabel").pack(anchor="w", pady=(0, 6))
        self.tasks = tk.Listbox(sidebar, width=19, height=8, exportselection=False, relief="flat",
                                highlightthickness=1, highlightbackground=self.colors["edge"],
                                selectbackground=self.colors["accent"], selectforeground=self.colors["onaccent"],
                                activestyle="none")
        self.tasks.pack(fill="both", expand=True)
        self.tasks.bind("<<ListboxSelect>>", self.select_task)
        ttk.Separator(sidebar).pack(fill="x", pady=13)
        ttk.Button(sidebar, text="模型与 API Key", command=self.show_models).pack(fill="x", pady=(0, 6))
        self.plugin_button = ttk.Button(sidebar, text="MCP 插件", command=self.show_plugins)
        self.plugin_button.pack(fill="x", pady=(0, 6))
        self.wechat_button = ttk.Button(sidebar, text="微信 ClawBot", command=self.show_wechat)
        self.wechat_button.pack(fill="x")

        self.task_title = ttk.Label(center, font=("Microsoft YaHei UI", 18, "bold"))
        self.task_title.pack(anchor="w")
        self.task_path = ttk.Label(center, style="Dim.TLabel", font=("Microsoft YaHei UI", 9), width=52)
        self.task_path.pack(anchor="w", fill="x", pady=(3, 12))
        ttk.Separator(center).pack(fill="x", pady=(0, 9))
        conversation, self.conversation = make_text(center, self.colors, bg=self.colors["bg"])
        self.conversation.tag_configure("label", foreground=self.colors["accent"],
                                        font=("Microsoft YaHei UI", 10, "bold"), spacing1=9, spacing3=2)
        self.conversation.tag_configure("user", foreground=self.colors["text"], spacing1=5, spacing3=7)
        self.conversation.tag_configure("intro", foreground=self.colors["text"],
                                        font=("Microsoft YaHei UI", 20, "bold"), spacing1=28, spacing3=8)
        self.conversation.tag_configure("muted", foreground=self.colors["dim"])
        self.conversation.tag_configure("danger", foreground=self.colors["danger"])
        conversation.pack(fill="both", expand=True)

        self.composer = ttk.Frame(center)
        self.approval_bar = ttk.Frame(self.composer, style="Card.TFrame", padding=8)
        self.approval_label = ttk.Label(self.approval_bar, style="Accent.TLabel")
        self.approval_label.pack(side="left", fill="x", expand=True)
        ttk.Button(self.approval_bar, text="查看并审批", command=self.review_pending).pack(side="right")
        self.prompt_box, self.prompt = make_text(self.composer, self.colors, height=4, editable=True)
        self.prompt_box.pack(fill="x", pady=(5, 6), expand=False)
        self.prompt.bind("<Control-Return>", self.send_shortcut)
        bottom = ttk.Frame(self.composer)
        bottom.pack(fill="x")
        self.model = ttk.Combobox(bottom, width=24, state="readonly")
        self.effort = ttk.Combobox(bottom, width=6, state="readonly")
        ttk.Label(bottom, text="模型").pack(side="left", padx=(0, 5))
        self.model.pack(side="left")
        ttk.Label(bottom, text="推理").pack(side="left", padx=(12, 5))
        self.effort.pack(side="left")
        self.model.bind("<<ComboboxSelected>>", self.model_changed)
        self.send_button = ttk.Button(bottom, text="发送 ↑", style="Primary.TButton", command=self.send)
        self.send_button.pack(side="right")
        self.stop_button = ttk.Button(bottom, text="停止", command=self.stop)
        self.stop_button.pack(side="right", padx=6)
        self.status = ttk.Label(center, style="Dim.TLabel", wraplength=560)
        self.status.pack(side="bottom", anchor="w", pady=(7, 0))

        ttk.Label(inspector, text="任务详情", font=("Microsoft YaHei UI", 13, "bold")).pack(anchor="w", pady=(4, 10))
        tabs = ttk.Notebook(inspector)
        tabs.pack(fill="both", expand=True)
        activities = ttk.Frame(tabs, padding=7)
        changes = ttk.Frame(tabs, padding=7)
        tabs.add(activities, text="活动")
        tabs.add(changes, text="文件改动")
        self.activity_rows = tk.Listbox(activities, height=9, width=23, exportselection=False, relief="flat",
                                        selectbackground=self.colors["accent"], selectforeground=self.colors["onaccent"])
        self.activity_rows.pack(fill="x", pady=(0, 8))
        self.activity_rows.bind("<<ListboxSelect>>", self.show_activity)
        output_box, self.output = make_text(activities, self.colors, mono=True)
        output_box.pack(fill="both", expand=True)
        self.changes = tk.Listbox(changes, height=9, width=23, exportselection=False, relief="flat",
                                  selectbackground=self.colors["accent"], selectforeground=self.colors["onaccent"])
        self.changes.pack(fill="both", expand=True, pady=(0, 8))
        self.changes.bind("<Double-Button-1>", lambda _: self.review_change())
        ttk.Button(changes, text="查看差异", command=self.review_change).pack(fill="x", pady=(0, 6))
        ttk.Button(changes, text="撤销选中改动", command=self.undo_change).pack(fill="x")

    def async_call(self, call, finish):
        def work():
            try:
                result, error = call(), None
            except Exception as exc:
                result, error = None, self.app.store.redact(str(exc))
            self.jobs.put((finish, result, error))
        threading.Thread(target=work, daemon=True).start()

    def error(self, title, exc):
        messagebox.showerror(title, self.app.store.redact(str(exc)), parent=self.root)

    def reload_tasks(self):
        tasks = self.app.state()["tasks"]
        self.task_list_ids = [item["id"] for item in tasks]
        self.tasks.delete(0, "end")
        for item in tasks:
            self.tasks.insert("end", ("●  " if self.app.active() and self.app.run.task == item["id"] else "    ") + item["title"])
        if self.task and self.task["id"] in self.task_list_ids:
            self.tasks.selection_set(self.task_list_ids.index(self.task["id"]))

    def render_models(self, selected=None, effort=None):
        providers = self.app.state()["providers"]
        self.models = [identifier for identifier, spec in MODELS.items() if spec["provider"] != "demo" or self.task["demo"]]
        labels = []
        for identifier in self.models:
            spec = MODELS[identifier]
            labels.append(spec["name"] + ("  ·  缺少 Key" if providers.get(spec["provider"], {}).get("status") == "missing" else ""))
        self.model.configure(values=labels)
        self.model.current(self.models.index(selected) if selected in self.models else self.models.index("gpt-6.2-sol"))
        self.refresh_efforts(effort)

    def selected_model(self):
        index = self.model.current()
        return self.models[index] if index >= 0 else "gpt-6.2-sol"

    def refresh_efforts(self, selected=None):
        spec = MODELS[self.selected_model()]
        self.effort_values = spec["efforts"]
        self.effort.configure(values=[EFFORTS[effort] for effort in self.effort_values])
        self.effort.current(self.effort_values.index(selected if selected in self.effort_values else spec["default"]))
        self.refresh_controls()

    def model_changed(self, _):
        self.refresh_efforts()

    def render_task(self):
        self.conversation.configure(state="normal")
        self.conversation.delete("1.0", "end")
        self.activity.clear()
        self.activity_rows.delete(0, "end")
        self.changes.delete(0, "end")
        self.change_ids.clear()
        set_text(self.output, "")
        if not self.task:
            self.task_title.configure(text="工作台")
            self.task_path.configure(text="")
            self.conversation.insert("end", "下一步，做什么？\n", "intro")
            self.conversation.insert("end", "打开本地项目，或试用离线演示。模型和插件可在左侧配置。\n", "muted")
            self.composer.pack_forget()
            self.conversation.configure(state="disabled")
            return
        self.task_title.configure(text=self.task["title"])
        self.task_path.configure(text=self.task["root"])
        if not self.task["messages"]:
            self.conversation.insert("end", "描述要做的事，或让 Agent 先检查项目。\n", "muted")
        for message in self.task["messages"]:
            if message["role"] == "user":
                self.conversation.insert("end", "\n你\n", "label")
            else:
                name = MODELS.get(message["model"], {}).get("name", message["model"])
                self.conversation.insert("end", "\n" + name + " · " + EFFORTS[message["effort"]] + "\n", "label")
            self.conversation.insert("end", message["text"].strip() + "\n", "user")
            if message.get("info"):
                self.conversation.insert("end", message["info"] + "\n", "danger" if message.get("status") == "error" else "muted")
            for entry in message.get("activity", []):
                self.add_activity(entry)
        for change in reversed(self.task["changes"]):
            label = {"applied": "已应用", "undone": "已撤销", "conflict": "冲突"}.get(change["status"], "处理中")
            self.changes.insert("end", f"{change['path']}  ·  {label}")
            self.change_ids.append(change)
        self.conversation.configure(state="disabled")
        self.conversation.see("end")
        if not self.composer.winfo_manager():
            self.composer.pack(side="bottom", fill="x")

    def select_task(self, _):
        selection = self.tasks.curselection()
        if selection:
            identifier = self.task_list_ids[selection[0]]
            if not self.task or self.task["id"] != identifier:
                self.open_task(identifier)

    def open_task(self, identifier):
        self.task = self.app.detail(identifier)
        with self.app.store.lock:
            self.app.store.state["desktop_task"] = identifier
            self.app.store.save()
        self.event_count, self.run_id = 0, None
        if self.app.run and self.app.run.task == identifier and self.app.run.done:
            self.run_id = self.app.run.id
            self.event_count = len(self.app.run.events)
        self.pending = None
        self.approval_bar.pack_forget()
        self.prompt.delete("1.0", "end")
        self.reload_tasks()
        self.render_task()
        self.render_models(self.task["model"], self.task["effort"])

    def new_project(self):
        start = self.task["root"] if self.task and not self.task["demo"] else str(Path.home())
        path = filedialog.askdirectory(parent=self.root, title="选择项目文件夹", initialdir=start, mustexist=True)
        if path:
            try:
                self.open_task(self.app.create(path)["id"])
                self.prompt.focus_set()
            except (ValueError, OSError) as exc:
                self.error("无法打开项目", exc)

    def demo(self):
        if self.app.active():
            self.error("离线演示", "请先等待当前任务完成，或点停止。")
            return
        self.open_task(self.app.create(demo=True)["id"])
        self.prompt.insert("1.0", "把示例项目的问候语改成中文，运行测试，再检查一段 JSON。")
        self.send()

    def send_shortcut(self, _):
        if self.task and self.send_button.instate(("!disabled",)):
            self.send()
        return "break"

    def send(self):
        text = input_text(self.prompt)
        if not self.task or not text:
            self.prompt.focus_set()
            return
        try:
            model = self.selected_model()
            effort = self.effort_values[self.effort.current()]
            provider = MODELS[model]["provider"]
            last = next((item["model"] for item in reversed(self.task["messages"]) if item.get("model") in MODELS), None)
            share = False
            if last and provider in PROVIDERS and MODELS[last]["provider"] != provider:
                share = messagebox.askyesno("确认切换供应商",
                                            f"将近期任务对话交给 {PROVIDERS[provider]['name']}，继续当前任务？",
                                            parent=self.root)
                if not share:
                    return
            self.app.start({"task": self.task["id"], "model": model, "effort": effort, "prompt": text, "share_context": share})
            self.prompt.delete("1.0", "end")
            self.task = self.app.detail(self.task["id"])
            self.event_count, self.run_id = 0, None
            self.reload_tasks()
            self.render_task()
            self.refresh_controls()
        except (ValueError, RuntimeError, OSError) as exc:
            self.error("无法发送", exc)

    def refresh_controls(self):
        if not self.task:
            return
        current = self.app.run
        active = self.app.active()
        here = active and current.task == self.task["id"]
        provider = MODELS[self.selected_model()]["provider"]
        ready = provider == "demo" or self.app.store.key_status(provider) != "missing"
        self.send_button.state(["!disabled"] if not active and ready else ["disabled"])
        self.stop_button.state(["!disabled"] if here else ["disabled"])
        for field in (self.model, self.effort):
            field.configure(state="disabled" if here else "readonly")
        if here:
            description = "等待审批" if self.pending else "正在处理任务…"
        elif active:
            description = "另一个任务正在运行。"
        elif not ready:
            description = "请在「模型与 API Key」中为选中的模型保存 Key。"
        else:
            description = "Ctrl + Enter 发送"
        self.status.configure(text=description)

    def stop(self):
        if self.app.active() and self.app.run.task == self.task["id"]:
            self.app.run.cancel()

    def add_activity(self, entry):
        label = TOOL_LABELS.get(entry["name"], entry["name"])
        self.activity.insert(0, entry)
        self.activity_rows.insert(0, label + ("  ·  " + entry["path"] if entry.get("path") else ""))

    def show_activity(self, _):
        selected = self.activity_rows.curselection()
        if selected:
            set_text(self.output, self.activity[selected[0]]["output"])

    def selected_change(self):
        selected = self.changes.curselection()
        if selected:
            return self.change_ids[selected[0]]
        return None

    def review_change(self):
        change = self.selected_change()
        if change:
            self.review(change["path"], change["diff"], True)

    def undo_change(self):
        change = self.selected_change()
        if change and change["status"] == "applied" and messagebox.askyesno(
                "撤销文件改动", f"撤销工作台上对 {change['path']} 的这次改动？", parent=self.root):
            try:
                self.app.undo(self.task["id"], change["id"])
                self.task = self.app.detail(self.task["id"])
                self.render_task()
            except (ValueError, OSError) as exc:
                self.error("撤销失败", exc)

    def review(self, title, value, diff=False, approval=None):
        if self.review_window and self.review_window.winfo_exists():
            self.review_window.destroy()
        window = show_dialog(self.root, title, 740, 450)
        self.review_window = window
        frame = ttk.Frame(window, padding=18)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text=title, font=("Microsoft YaHei UI", 15, "bold")).pack(anchor="w", pady=(0, 10))
        container, text = make_text(frame, self.colors, mono=True)
        container.pack(fill="both", expand=True)
        text.configure(state="normal")
        text.tag_configure("add", foreground=self.colors["green"])
        text.tag_configure("remove", foreground=self.colors["red"])
        for line in value.splitlines():
            tag = "add" if diff and line.startswith("+") else "remove" if diff and line.startswith("-") else ""
            text.insert("end", line + "\n", tag)
        text.configure(state="disabled")
        footer = ttk.Frame(frame)
        footer.pack(fill="x", pady=(9, 0))
        if approval:
            ttk.Button(footer, text="批准", style="Primary.TButton",
                       command=lambda: self.decide(approval, True, window)).pack(side="right")
            ttk.Button(footer, text="拒绝", command=lambda: self.decide(approval, False, window)).pack(side="right", padx=6)
        else:
            ttk.Button(footer, text="关闭", command=window.destroy).pack(side="right")

    def review_pending(self):
        approval = self.pending
        if not approval:
            return
        if approval["kind"] == "edit":
            self.review(approval["path"], approval["diff"], True, approval)
        elif approval["kind"] == "command":
            self.review("命令审批", f"{approval['cwd']}\n\n{approval['command']}", approval=approval)
        else:
            self.review("MCP 插件调用", f"{approval['plugin']} / {approval['tool']}\n\n{approval['args']}", approval=approval)

    def decide(self, approval, allowed, window):
        try:
            self.app.run.decide(approval["id"], allowed)
        except ValueError as exc:
            self.error("审批失效", exc)
        window.destroy()
        self.pending = None
        self.approval_bar.pack_forget()

    def update_run(self):
        run = self.app.run
        if not run or not self.task or run.task != self.task["id"]:
            return
        if self.run_id != run.id:
            if run.done:
                self.task = self.app.detail(self.task["id"])
                self.render_task()
                self.reload_tasks()
                self.refresh_controls()
                self.run_id = run.id
                self.event_count = len(run.events)
                return
            self.run_id = run.id
            self.append_live("\n" + MODELS[run.model]["name"] + " · " + EFFORTS[run.effort] + "\n", "label")
        state = run.snapshot(self.event_count)
        if state["done"] and state["count"] == self.event_count:
            return
        self.event_count = state["count"]
        for event in state["events"]:
            if event["type"] == "text":
                self.append_live(event["text"])
            elif event["type"] == "result":
                self.add_activity(event)
        approval = state["approval"]
        if approval and approval != self.pending:
            self.pending = approval
            self.approval_label.configure(text=approval["title"] + ("  ·  " + approval["path"] if approval.get("path") else ""))
            if not self.approval_bar.winfo_manager():
                self.approval_bar.pack(fill="x", before=self.prompt_box)
        elif not approval and self.pending:
            self.pending = None
            self.approval_bar.pack_forget()
        if state["done"]:
            self.task = self.app.detail(self.task["id"])
            self.render_task()
            self.reload_tasks()
            self.refresh_controls()

    def append_live(self, value, style=""):
        follows = self.conversation.yview()[1] > .95
        self.conversation.configure(state="normal")
        self.conversation.insert("end", value, style)
        self.conversation.configure(state="disabled")
        if follows:
            self.conversation.see("end")

    def tick(self):
        try:
            while not self.jobs.empty():
                finish, value, error = self.jobs.get_nowait()
                finish(value, error)
            self.update_run()
            status, _ = self.app.plugins.snapshot()
            active_plugins = sum(item["state"] == "connected" for item in status)
            self.plugin_button.configure(text=f"MCP 插件  ·  {active_plugins} 已连接")
            if self.plugin_window and self.plugin_window.window.winfo_exists():
                self.plugin_window.refresh()
            if self.wechat_window and self.wechat_window.window.winfo_exists():
                self.wechat_window.refresh()
            if self.wechat:
                count = len(self.wechat.messages())
                self.wechat_button.configure(text="微信 ClawBot" + (f"  ·  {count} 条" if count else ""))
        finally:
            self.root.after(160, self.tick)

    def show_models(self):
        from desktop_settings import ModelSettings
        if self.model_window and self.model_window.window.winfo_exists():
            self.model_window.window.lift()
        else:
            self.model_window = ModelSettings(self)

    def show_plugins(self):
        from desktop_settings import PluginSettings
        if self.plugin_window and self.plugin_window.window.winfo_exists():
            self.plugin_window.window.lift()
        else:
            self.plugin_window = PluginSettings(self)

    def show_wechat(self):
        from desktop_settings import WeChatSettings
        if not self.wechat:
            self.error("微信", "本机微信凭证无法读取。")
        elif self.wechat_window and self.wechat_window.window.winfo_exists():
            self.wechat_window.window.lift()
        else:
            self.wechat_window = WeChatSettings(self)

    def import_wechat(self, text):
        if not self.task:
            self.new_project()
        if not self.task:
            return False
        if input_text(self.prompt) and not messagebox.askyesno("替换草稿", "用选中的微信消息替换当前草稿？", parent=self.root):
            return False
        self.prompt.delete("1.0", "end")
        self.prompt.insert("1.0", text)
        self.prompt.focus_set()
        return True

    def close(self):
        if self.app.active():
            self.app.run.cancel()
        if self.wechat:
            self.wechat.close()
        self.app.plugins.close()
        self.root.destroy()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, default=local_data())
    parser.add_argument("--wechat", action="store_true")
    parser.add_argument("--pair-wechat", action="store_true")
    args = parser.parse_args()
    if os.name == "nt":
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (OSError, AttributeError):
            pass
    root = tk.Tk()
    icon = Path(__file__).resolve().with_name("desktop.ico")
    if icon.exists():
        root.iconbitmap(default=str(icon))
    # Root is displayed only after acquiring the state lock and building the native window.
    root.withdraw()
    try:
        args.data_dir.mkdir(parents=True, exist_ok=True)
        with instance_lock(args.data_dir):
            app = App(args.data_dir, desktop_defaults=True)
            desktop = Desktop(root, app)
            root.deiconify()
            if args.wechat or args.pair_wechat:
                def open_wechat():
                    desktop.show_wechat()
                    if args.pair_wechat and desktop.wechat_window and not desktop.wechat.account:
                        desktop.wechat_window.start()
                root.after(300, open_wechat)
            root.mainloop()
    except (OSError, RuntimeError, ValueError) as exc:
        messagebox.showerror(TITLE, str(exc), parent=root)
        root.destroy()


if __name__ == "__main__":
    main()

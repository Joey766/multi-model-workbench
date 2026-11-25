import json
import queue
import tkinter as tk
from tkinter import messagebox, ttk
import webbrowser

import qrcode

from catalog import PRESETS, plugin_configuration, set_preset
from desktop import FONT, MODELS, PROVIDERS, input_text, make_text, set_text, show_dialog
from plugins import validate


class ModelSettings:
    def __init__(self, desktop):
        self.desktop = desktop
        self.app = desktop.app
        self.window = show_dialog(desktop.root, "模型与 API Key", 725, 496)
        frame = ttk.Frame(self.window, padding=16)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="模型与 API Key", font=("Microsoft YaHei UI", 16, "bold")).pack(anchor="w")
        ttk.Label(frame, text="填任意一家即可开始。Windows 会加密保存，重启后自动读取。",
                  style="Dim.TLabel").pack(anchor="w", pady=(4, 13))
        tabs = ttk.Notebook(frame)
        tabs.pack(fill="both", expand=True)
        for identifier, provider in PROVIDERS.items():
            tab = ttk.Frame(tabs, padding=16)
            tabs.add(tab, text=provider["name"])
            self.panel(tab, identifier, provider)

    def panel(self, panel, identifier, provider):
        model_ids = [key for key, model in MODELS.items() if model["provider"] == identifier]
        ttk.Label(panel, text=provider["name"], font=("Microsoft YaHei UI", 13, "bold")).pack(anchor="w")
        ttk.Label(panel, text=" / ".join(MODELS[key]["name"] for key in model_ids),
                  style="Dim.TLabel").pack(anchor="w", pady=(2, 16))
        status = ttk.Label(panel, style="Accent.TLabel")
        status.pack(anchor="w")
        ttk.Label(panel, text="API Key").pack(anchor="w", pady=(13, 3))
        entry = ttk.Entry(panel, show="•")
        entry.pack(fill="x")
        result = ttk.Label(panel, style="Dim.TLabel", wraplength=580)
        result.pack(anchor="w", pady=(8, 0))
        actions = ttk.Frame(panel)
        actions.pack(fill="x", pady=(8, 10))

        def refresh():
            current = self.app.store.key_status(identifier)
            status.configure(text={"saved": "已加密保存", "env": "来自环境变量", "missing": "尚未配置"}[current])
            test.state(["disabled"] if current == "missing" else ["!disabled"])
            remove.state(["!disabled"] if current == "saved" else ["disabled"])
            if self.desktop.task:
                selected = self.desktop.selected_model()
                index = self.desktop.effort.current()
                effort = self.desktop.effort_values[index] if index >= 0 else None
                self.desktop.render_models(selected, effort)

        def save():
            value = entry.get().strip()
            if len(value) < 8 or len(value) > 800 or any(char.isspace() for char in value):
                result.configure(text="请输入有效的 API Key。", style="Error.TLabel")
                return
            try:
                self.app.store.set_key(identifier, value)
                entry.delete(0, "end")
                result.configure(text="已保存。", style="Accent.TLabel")
                refresh()
            except (ValueError, OSError) as exc:
                result.configure(text=str(exc), style="Error.TLabel")

        def erase():
            if messagebox.askyesno("移除 API Key", f"移除这台电脑上保存的 {provider['name']} Key？", parent=self.window):
                self.app.store.set_key(identifier, None)
                result.configure(text="已移除。", style="Dim.TLabel")
                refresh()

        def ping():
            test.state(["disabled"])
            result.configure(text="正在发出测试请求…", style="Dim.TLabel")
            model = model_ids[selector.current()]

            def finish(_result, error):
                if self.window.winfo_exists():
                    result.configure(text=error or f"连接成功 · {MODELS[model]['name']}",
                                     style="Error.TLabel" if error else "Accent.TLabel")
                    refresh()

            self.desktop.async_call(lambda: self.app.test_key(identifier, model), finish)

        ttk.Button(actions, text="保存 Key", style="Primary.TButton", command=save).pack(side="left")
        remove = ttk.Button(actions, text="移除", command=erase)
        remove.pack(side="left", padx=6)
        ttk.Button(actions, text="获取 API Key ↗", command=lambda: webbrowser.open(provider["key_url"])).pack(side="right")
        check = ttk.Frame(panel)
        check.pack(fill="x", pady=(9, 0))
        selector = ttk.Combobox(check, state="readonly", values=[MODELS[key]["name"] for key in model_ids], width=27)
        selector.current(0)
        selector.pack(side="left")
        test = ttk.Button(check, text="测试连接", command=ping)
        test.pack(side="left", padx=8)
        ttk.Label(panel, text="测试连接会发送一条真实请求，使用对应 API 额度。",
                  style="Dim.TLabel").pack(anchor="w", pady=(10, 0))
        refresh()


class PluginSettings:
    def __init__(self, desktop):
        self.desktop = desktop
        self.app = desktop.app
        self.window = show_dialog(desktop.root, "MCP 插件", 830, 618)
        panel = ttk.Frame(self.window, padding=16)
        panel.pack(fill="both", expand=True)
        ttk.Label(panel, text="MCP 插件", font=("Microsoft YaHei UI", 16, "bold")).pack(anchor="w")
        ttk.Label(panel, text="选插件并启用一次，已启用的连接会在下次启动时自动恢复。",
                  style="Dim.TLabel").pack(anchor="w", pady=(4, 10))
        tabs = ttk.Notebook(panel)
        tabs.pack(fill="both", expand=True)
        presets = ttk.Frame(tabs, padding=13)
        custom = ttk.Frame(tabs, padding=13)
        tabs.add(presets, text="插件预设")
        tabs.add(custom, text="自定义 MCP")
        self.names = ["builtin-text", *PRESETS]
        self.selected = self.names[0]
        self.last_status = None
        self.listbox = tk.Listbox(presets, width=29, relief="flat", exportselection=False, font=FONT,
                                  selectbackground=desktop.colors["accent"], selectforeground=desktop.colors["onaccent"])
        self.listbox.pack(side="left", fill="both", padx=(0, 13))
        self.listbox.bind("<<ListboxSelect>>", self.select)
        details = ttk.Frame(presets)
        details.pack(side="left", fill="both", expand=True)
        self.title = ttk.Label(details, font=("Microsoft YaHei UI", 14, "bold"))
        self.title.pack(anchor="w")
        self.description = ttk.Label(details, style="Dim.TLabel", wraplength=445)
        self.description.pack(anchor="w", pady=(4, 9))
        self.status = ttk.Label(details, style="Accent.TLabel", wraplength=445)
        self.status.pack(anchor="w")
        self.token_box = ttk.Frame(details)
        self.token_label = ttk.Label(self.token_box)
        self.token_label.pack(anchor="w")
        self.token = ttk.Entry(self.token_box, show="•")
        self.token.pack(fill="x", pady=4)
        self.credentials = ttk.Label(self.token_box, style="Dim.TLabel")
        self.credentials.pack(anchor="w")
        self.token_remove = ttk.Button(self.token_box, text="移除已保存令牌", command=self.erase)
        self.token_remove.pack(anchor="w", pady=(3, 0))
        self.actions = ttk.Frame(details)
        self.actions.pack(side="bottom", fill="x", pady=(7, 0))
        self.enable = ttk.Button(self.actions, text="启用并连接", style="Primary.TButton", command=lambda: self.toggle(True))
        self.enable.pack(side="left")
        self.disable = ttk.Button(self.actions, text="停用", command=lambda: self.toggle(False))
        self.disable.pack(side="left", padx=6)
        self.help = ttk.Button(self.actions, text="官方说明 ↗",
                               command=lambda: webbrowser.open(PRESETS[self.selected]["docs"]))
        self.help.pack(side="right")
        tools_frame, self.tools = make_text(details, desktop.colors, height=6)
        tools_frame.pack(side="bottom", fill="both", expand=True, pady=(8, 0))
        ttk.Label(custom, text="支持 stdio 和 Streamable HTTP。令牌可从环境变量取值。",
                  style="Dim.TLabel").pack(anchor="w", pady=(0, 7))
        container, self.json = make_text(custom, desktop.colors, mono=True, editable=True)
        container.pack(fill="both", expand=True)
        self.load_configuration()
        custom_footer = ttk.Frame(custom)
        custom_footer.pack(fill="x", pady=(9, 0))
        self.error = ttk.Label(custom_footer, style="Error.TLabel", wraplength=510)
        self.error.pack(side="left", fill="x", expand=True)
        ttk.Button(custom_footer, text="重读配置", command=self.load_configuration).pack(side="right", padx=6)
        ttk.Button(custom_footer, text="保存并连接", style="Primary.TButton", command=self.save_configuration).pack(side="right")
        self.refresh()
        self.details()

    def load_configuration(self):
        self.json.delete("1.0", "end")
        self.json.insert("1.0", json.dumps({"mcpServers": plugin_configuration(self.app.store)}, ensure_ascii=False, indent=2))

    def save_configuration(self):
        try:
            data = json.loads(input_text(self.json))
            config = validate(data["mcpServers"])
            with self.app.store.lock:
                self.app.store.state["plugins"] = config
                self.app.store.save()
            self.reconnect()
            self.error.configure(text="设置已保存。", style="Accent.TLabel")
        except (ValueError, KeyError, TypeError, OSError) as exc:
            self.error.configure(text=self.app.store.redact(str(exc)), style="Error.TLabel")

    def select(self, _):
        indexes = self.listbox.curselection()
        if indexes:
            name = self.names[indexes[0]]
            if name != self.selected:
                self.selected = name
                self.token.delete(0, "end")
                self.details()

    def reconnect(self):
        def finish(_value, error):
            if self.window.winfo_exists():
                self.status.configure(text=error or "正在连接已启用的插件…")
                self.last_status = None
                self.refresh()
        self.desktop.async_call(self.app.refresh_plugins, finish)

    def toggle(self, enabled):
        if self.selected not in PRESETS:
            return
        value = self.token.get().strip()
        try:
            set_preset(self.app.store, self.selected, enabled, value or None)
            self.token.delete(0, "end")
            self.load_configuration()
            self.reconnect()
        except (ValueError, OSError) as exc:
            self.status.configure(text=str(exc), style="Error.TLabel")

    def erase(self):
        if messagebox.askyesno("移除插件令牌", "移除本机加密保存的令牌并停用该插件？", parent=self.window):
            try:
                set_preset(self.app.store, self.selected, False, "")
                self.load_configuration()
                self.reconnect()
            except (ValueError, OSError) as exc:
                self.status.configure(text=str(exc), style="Error.TLabel")

    def refresh(self):
        items, _ = self.app.plugins.snapshot()
        config = plugin_configuration(self.app.store)
        current = (items, config)
        if current == self.last_status:
            return
        self.last_status = current
        self.results = {item["name"]: item for item in items}
        self.names = ["builtin-text", *PRESETS] + sorted(set(config) - set(PRESETS))
        if self.selected not in self.names:
            self.selected = self.names[0]
        self.listbox.delete(0, "end")
        for name in self.names:
            title = "内置文本工具" if name == "builtin-text" else PRESETS.get(name, {}).get("title", name)
            state = self.results.get(name, {}).get("state")
            short = {"connected": "已连接", "connecting": "连接中", "error": "失败"}.get(state, "未启用")
            self.listbox.insert("end", title + "  ·  " + short)
        self.listbox.selection_set(self.names.index(self.selected))
        self.details()

    def details(self):
        name = self.selected
        preset = PRESETS.get(name)
        self.title.configure(text="内置文本工具" if name == "builtin-text" else preset["title"] if preset else name)
        self.description.configure(text="读取和校验 JSON，统计文本。" if name == "builtin-text"
                                   else preset["description"] if preset else "从「自定义 MCP」管理此连接。")
        state = self.results.get(name, {})
        label = {"connected": "已连接", "connecting": "正在连接…", "error": "连接失败"}.get(state.get("state"), "未启用")
        detail = state.get("error", "")
        self.status.configure(text=label + (f" · {detail}" if detail else ""),
                              style="Error.TLabel" if detail else "Accent.TLabel")
        set_text(self.tools, "\n".join(state.get("tools", [])))
        for control in (self.enable, self.disable, self.help):
            control.state(["!disabled"] if preset else ["disabled"])
        self.token_box.pack_forget()
        if preset and "token" in preset:
            self.token_box.pack(anchor="w", fill="x", pady=(8, 0))
            self.token_label.configure(text=preset["token"])
            with self.app.store.lock:
                stored = bool(self.app.store.state["sealed"].get("mcp:" + name))
            self.credentials.configure(text="已加密保存" if stored else "尚未保存")
            self.token_remove.state(["!disabled"] if stored else ["disabled"])


class WeChatSettings:
    def __init__(self, desktop):
        self.desktop = desktop
        self.wechat = desktop.wechat
        self.window = show_dialog(desktop.root, "微信 ClawBot", 806, 660)
        frame = ttk.Frame(self.window, padding=17)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="微信 ClawBot", font=("Microsoft YaHei UI", 16, "bold")).pack(anchor="w")
        ttk.Label(frame, text="绑定自己的微信后，可把 ClawBot 的文字消息带进本地任务。",
                  style="Dim.TLabel").pack(anchor="w", pady=(4, 11))
        self.status = ttk.Label(frame, style="Accent.TLabel", wraplength=700)
        self.status.pack(anchor="w", fill="x")
        controls = ttk.Frame(frame)
        controls.pack(fill="x", pady=(8, 10))
        self.link = ttk.Button(controls, text="生成微信二维码", style="Primary.TButton", command=self.start)
        self.link.pack(side="left")
        self.disconnect = ttk.Button(controls, text="断开本机关联", command=self.unlink)
        self.disconnect.pack(side="left", padx=6)
        self.stop_link = ttk.Button(controls, text="取消本次扫码", command=self.cancel)
        self.stop_link.pack(side="left", padx=6)
        self.middle = ttk.Frame(frame)
        self.middle.pack(fill="x")
        self.canvas = tk.Canvas(self.middle, width=235, height=235, bg="white", highlightthickness=0)
        self.pin_frame = ttk.Frame(self.middle, padding=(13, 4))
        ttk.Label(self.pin_frame, text="手机微信显示的 PIN").pack(anchor="w")
        self.pin = ttk.Entry(self.pin_frame, width=20, show="•")
        self.pin.pack(anchor="w", pady=5)
        ttk.Button(self.pin_frame, text="提交 PIN", command=self.submit_pin).pack(anchor="w")
        self.pin.bind("<Return>", lambda _event: self.submit_pin())

        body = ttk.Frame(frame)
        body.pack(fill="both", expand=True)
        self.rows = tk.Listbox(body, width=26, height=8, relief="flat", exportselection=False,
                               selectbackground=desktop.colors["accent"], selectforeground=desktop.colors["onaccent"])
        self.rows.pack(side="left", fill="both", padx=(0, 9))
        self.rows.bind("<<ListboxSelect>>", self.select)
        messages = ttk.Frame(body)
        messages.pack(fill="both", expand=True)
        text_box, self.text = make_text(messages, desktop.colors, height=6)
        text_box.pack(fill="both", expand=True)
        actions = ttk.Frame(messages)
        actions.pack(fill="x", pady=(8, 0))
        self.import_button = ttk.Button(actions, text="带入任务草稿", style="Primary.TButton", command=self.import_selected)
        self.import_button.pack(side="left")
        self.reply_button = ttk.Button(actions, text="写回复", command=self.compose_reply)
        self.reply_button.pack(side="left", padx=7)
        self.entries = []
        self.last_title = ""
        self.refresh(change=True)
        self.window.protocol("WM_DELETE_WINDOW", self.hide)

    def hide(self):
        if self.wechat.binding:
            self.wechat.cancel_bind()
        self.window.destroy()

    def start(self):
        try:
            self.wechat.start_bind()
            self.refresh()
        except (ValueError, OSError) as exc:
            self.status.configure(text=str(exc))

    def cancel(self):
        self.wechat.cancel_bind()
        self.canvas.pack_forget()
        self.pin_frame.pack_forget()
        self.wechat.update("已取消本次扫码。")

    def submit_pin(self):
        try:
            self.wechat.set_pin(self.pin.get())
            self.pin.delete(0, "end")
            self.pin_frame.pack_forget()
        except ValueError as exc:
            self.status.configure(text=str(exc))

    def draw(self, data):
        code = qrcode.QRCode(border=4)
        code.add_data(data)
        code.make(fit=True)
        matrix = code.get_matrix()
        scale = max(2, 235 // len(matrix))
        offset = (235 - len(matrix) * scale) // 2
        self.canvas.delete("all")
        for r, line in enumerate(matrix):
            for c, filled in enumerate(line):
                if filled:
                    x, y = offset + c * scale, offset + r * scale
                    self.canvas.create_rectangle(x, y, x + scale, y + scale, fill="#000000", width=0)
        self.canvas.pack(side="left", pady=(0, 8))

    def refresh(self, change=False):
        while True:
            try:
                kind, value = self.wechat.events.get_nowait()
            except queue.Empty:
                break
            if kind == "qr" and self.wechat.binding:
                self.draw(value)
            elif kind == "pin" and self.wechat.binding:
                self.pin_frame.pack(side="left", anchor="n")
                self.pin.focus_set()
            elif kind in {"linked", "bind-ended"}:
                self.canvas.pack_forget()
                self.pin_frame.pack_forget()
            elif kind == "inbox":
                change = True
        bound, binding = bool(self.wechat.account), self.wechat.binding
        self.status.configure(text=self.wechat.status)
        title = "微信 ClawBot · " + self.wechat.status
        if self.last_title != title:
            self.last_title = title
            self.window.title(title)
        self.link.state(["disabled"] if bound or binding else ["!disabled"])
        self.disconnect.state(["!disabled"] if bound else ["disabled"])
        self.stop_link.state(["!disabled"] if binding else ["disabled"])
        if change:
            previous = self.item()
            self.entries = self.wechat.messages()
            self.rows.delete(0, "end")
            if not self.entries:
                self.rows.insert("end", "暂无 ClawBot 消息")
            else:
                for entry in self.entries:
                    self.rows.insert("end", " ".join(entry["text"].split())[:34])
                ids = [x["id"] for x in self.entries]
                self.rows.selection_set(ids.index(previous["id"]) if previous and previous["id"] in ids else 0)
            self.select(None)

    def item(self):
        indexes = self.rows.curselection()
        return self.entries[indexes[0]] if indexes and self.entries else None

    def select(self, _):
        item = self.item()
        set_text(self.text, item["text"] if item else "绑定后，在手机的 ClawBot 对话里发送一条文字，即可在这里查看。")
        self.import_button.state(["!disabled"] if item else ["disabled"])
        self.reply_button.state(["!disabled"] if item and item.get("context") else ["disabled"])

    def import_selected(self):
        item = self.item()
        if item and self.desktop.import_wechat(item["text"]):
            self.window.destroy()

    def compose_reply(self):
        item = self.item()
        if not item:
            return
        window = show_dialog(self.desktop.root, "回复到自己的微信", 610, 420)
        frame = ttk.Frame(window, padding=15)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="回复至已绑定的微信 ClawBot 对话。").pack(anchor="w", pady=(0, 7))
        container, entry = make_text(frame, self.desktop.colors, editable=True)
        container.pack(fill="both", expand=True)
        last = next((m["text"] for m in reversed(self.desktop.task["messages"])
                     if m["role"] == "assistant" and m["text"]), "") if self.desktop.task else ""
        if last:
            entry.insert("1.0", last[:3500])
        status = ttk.Label(frame, style="Dim.TLabel")
        status.pack(anchor="w", pady=(6, 4))

        def send():
            text = input_text(entry)
            if not text:
                status.configure(text="请输入要发送的文字。")
                return
            submit.state(["disabled"])
            status.configure(text="正在发送…")

            def finish(_result, error):
                if window.winfo_exists():
                    status.configure(text=error or "已发送。", style="Error.TLabel" if error else "Accent.TLabel")
                    if error:
                        submit.state(["!disabled"])
            self.desktop.async_call(lambda: self.wechat.reply(item, text), finish)

        submit = ttk.Button(frame, text="发送到微信", style="Primary.TButton", command=send)
        submit.pack(side="right")

    def unlink(self):
        if messagebox.askyesno("断开微信", "移除此应用保存的微信授权和本地收件箱？", parent=self.window):
            self.wechat.disconnect()
            self.refresh(change=True)

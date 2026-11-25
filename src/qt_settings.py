import json
import queue
from datetime import datetime, timezone

from PySide6.QtCore import QTimer, Qt, QUrl
from PySide6.QtGui import QColor, QDesktopServices, QImage, QPixmap
from PySide6.QtWidgets import (QDialog, QFrame, QHBoxLayout, QLabel, QLineEdit, QListWidget,
                               QMessageBox, QPlainTextEdit, QTabWidget, QVBoxLayout, QWidget)
import qrcode

from catalog import PRESETS, plugin_configuration, set_preset
from plugins import validate
from providers import PROVIDERS, deepseek_balance
from quotas import PANELS
from qt_theme import Combo, button, label, separator


def base_dialog(owner, title, subtitle, width=740, height=550):
    dlg = QDialog(owner)
    dlg.setWindowTitle(title)
    dlg.resize(width, height)
    layout = QVBoxLayout(dlg)
    layout.setContentsMargins(23, 20, 23, 20)
    layout.setSpacing(10)
    layout.addWidget(label(title, "Title"))
    description = label(subtitle, "Muted")
    description.setWordWrap(True)
    layout.addWidget(description)
    layout.addSpacing(5)
    return dlg, layout


def message(parent, prompt, title="多模型工作台"):
    QMessageBox.information(parent, title, str(prompt))


def confirm(parent, title, text):
    return QMessageBox.question(parent, title, text) == QMessageBox.StandardButton.Yes


class Settings:
    def __init__(self, owner):
        self.owner = owner
        self.store = owner.engine.store
        self.model_panels = {}
        self.discoveries = {}
        self.discovery_errors = {}

    def discover_configured(self):
        for provider in PROVIDERS:
            if self.store.key_status(provider) != "missing" and provider not in self.discoveries:
                self.discover(provider)

    def discover(self, provider):
        ticket = self.discoveries[provider] = object()
        self.discovery_errors.pop(provider, None)
        panel = self.model_panels.get(provider)
        if panel:
            panel()

        def done(_, error):
            if self.discoveries.get(provider) is not ticket:
                return
            self.discoveries.pop(provider)
            if error:
                self.discovery_errors[provider] = error
            self.owner.update_models()
            current = self.model_panels.get(provider)
            if current:
                current()
        self.owner.job(lambda: self.owner.engine.catalog.refresh(provider), done)

    def models(self):
        window, layout = base_dialog(self.owner, "模型与连接", "保存 API Key 后自动获取模型列表。凭证使用 Windows 加密保存。")
        tabs = QTabWidget()
        layout.addWidget(tabs, 1)
        for name, provider in PROVIDERS.items():
            tabs.addTab(self.provider_panel(window, name, provider), provider["name"])
        footer = QHBoxLayout()
        footer.addWidget(label("工作台外观", "Muted"))
        theme = Combo()
        theme.addItem("深色", "dark")
        theme.addItem("浅色", "light")
        theme.setCurrentIndex(1 if self.owner.theme == "light" else 0)
        footer.addWidget(theme)
        footer.addStretch()
        footer.addWidget(button("模型额度", lambda: self.usage(self.owner.model.currentData())))
        footer.addWidget(button("关闭", window.accept))
        layout.addLayout(footer)
        theme.currentIndexChanged.connect(lambda _: self.owner.change_theme(theme.currentData()))
        try:
            window.exec()
        finally:
            self.model_panels.clear()

    def usage(self, selected=None):
        window, layout = base_dialog(self.owner, "模型额度",
                                     "显示 API 最近一次返回的限额。会员的 5 小时和周额度在对应客户端查看。", 747, 480)
        selector = Combo()
        selector.setAccessibleName("查看额度的模型")
        specs = self.owner.engine.catalog.available()
        for identifier, spec in specs.items():
            if spec["provider"] in PROVIDERS:
                selector.addItem(spec["name"], identifier)
        selector.setCurrentIndex(max(0, selector.findData(selected)))
        layout.addWidget(selector)
        account = label("", "Muted")
        layout.addWidget(account)
        result = QPlainTextEdit()
        result.setReadOnly(True)
        result.setAccessibleName("API 限额详情")
        layout.addWidget(result, 1)
        balance = label("", "Accent")
        balance.setWordWrap(True)
        layout.addWidget(balance)
        actions = QHBoxLayout()
        layout.addLayout(actions)

        def format_end(when):
            if not when:
                return "未提供"
            local = when.astimezone().strftime("%m-%d %H:%M:%S")
            return local + ("（时间点已过，等下次响应更新）" if when <= datetime.now(timezone.utc) else "")

        def refresh():
            model = selector.currentData()
            provider = specs.get(model, {}).get("provider")
            if not provider:
                result.setPlainText("暂无模型可查看。请到「模型与连接」刷新模型列表。")
                return
            key = ""
            try:
                key = self.store.key(provider)
            except ValueError:
                pass
            network = self.owner.engine.quotas.current(model, key) if key else None
            account.setText(PROVIDERS[provider]["name"] + (" · 已配置 API Key" if key else " · 请先配置 API Key"))
            balance_button.setVisible(provider == "deepseek")
            if not network:
                hints = {
                    "gemini": "Gemini API 按项目分配限额。请点「官方限额」查看当前层级和用量。每日请求配额在太平洋时间零点重置。",
                    "deepseek": "DeepSeek API 主要按并发请求限流。请点「查询 DeepSeek 余额」读取账户余额。",
                }
                result.setPlainText(hints.get(provider, "这个模型本次启动后尚无 API 限额数据。完成一次实际请求后，会显示供应商实际返回的数据。"))
                return
            rows = [f"最近响应 {network['seen'].astimezone():%m-%d %H:%M:%S} · HTTP {network['status']}"]
            if network["buckets"]:
                for item in network["buckets"]:
                    available = f'{item["remaining"]:,}' if item["remaining"] is not None else "未提供"
                    limit = f'{item["limit"]:,}' if item["limit"] is not None else "未提供"
                    rows += ["", f'{item["name"]}　剩余 {available} / 上限 {limit}', f'预计重置　{format_end(item["reset"])}']
            else:
                rows += ["", "这次响应没有提供剩余量或窗口重置时间，请查看供应商控制台。"]
            if network["retry"]:
                rows += ["", "建议重试时间　" + format_end(network["retry"])]
            result.setPlainText("\n".join(rows))

        def provider_page(index):
            spec = specs.get(selector.currentData())
            if spec:
                QDesktopServices.openUrl(QUrl(PANELS[spec["provider"]][index]))

        def query_balance():
            try:
                key = self.store.key("deepseek")
                if not key:
                    raise ValueError("请先保存 DeepSeek API Key。")
            except ValueError as exc:
                balance.setText(str(exc))
                return
            balance_button.setEnabled(False)
            balance.setText("正在查询余额…")

            def done(value, error):
                balance_button.setEnabled(True)
                if specs.get(selector.currentData(), {}).get("provider") == "deepseek":
                    balance.setText(error or ("DeepSeek API 余额  " + " / ".join(f"{curr} {amount}" for curr, amount in value)
                                              if value else "DeepSeek 未返回可显示的余额。"))
            self.owner.job(lambda: deepseek_balance(key), done)

        actions.addWidget(button("官方限额 ↗", lambda: provider_page(0)))
        actions.addWidget(button("账单或用量 ↗", lambda: provider_page(1)))
        balance_button = button("查询 DeepSeek 余额", query_balance)
        balance_button.hide()
        actions.addWidget(balance_button)
        actions.addStretch()
        actions.addWidget(button("关闭", window.accept))

        def changed(_):
            balance.clear()
            refresh()
        selector.currentIndexChanged.connect(changed)
        timer = QTimer(window)
        timer.timeout.connect(refresh)
        refresh()
        timer.start(1200)
        window.exec()
        timer.stop()

    def provider_panel(self, window, name, provider):
        panel = QWidget()
        content = QVBoxLayout(panel)
        content.setContentsMargins(16, 18, 16, 18)
        status = label("", "Muted")
        content.addWidget(status)
        secret = QLineEdit()
        secret.setEchoMode(QLineEdit.EchoMode.Password)
        secret.setPlaceholderText("输入 API Key")
        secret.setAccessibleName(provider["name"] + " API Key")
        content.addWidget(secret)
        controls = QHBoxLayout()
        content.addLayout(controls)
        key_test = Combo()
        key_test.setAccessibleName(provider["name"] + " 模型列表")
        content.addSpacing(14)
        catalog_line = label("", "Accent")
        catalog_line.setWordWrap(True)
        content.addWidget(catalog_line)
        testing = QHBoxLayout()
        testing.addWidget(key_test, 1)
        content.addLayout(testing)
        detail = label("", "Muted")
        detail.setWordWrap(True)
        content.addWidget(detail)
        content.addStretch()
        testing_request = False

        def sync():
            saved = self.store.key_status(name)
            status.setText({"saved": "已加密保存", "env": "使用环境变量", "missing": "尚未配置"}[saved])
            remove.setEnabled(saved == "saved")
            pending = name in self.discoveries
            refresh_button.setEnabled(saved != "missing" and not pending)
            current = key_test.currentData()
            key_test.clear()
            for identifier, model in self.owner.engine.catalog.available().items():
                if model["provider"] == name:
                    key_test.addItem(model["name"], identifier)
                    key_test.setItemData(key_test.count() - 1, identifier, Qt.ItemDataRole.ToolTipRole)
            key_test.setCurrentIndex(max(0, key_test.findData(current)))
            test.setEnabled(saved != "missing" and key_test.count() > 0 and not testing_request)
            snapshot = self.owner.engine.catalog.snapshot(name)
            if pending:
                catalog_line.setText("正在读取当前 Key 的模型列表…")
            elif name in self.discovery_errors:
                fallback = "正在显示上次成功读取的列表。" if snapshot is not None else "正在显示预设模型。"
                catalog_line.setText(f"{self.discovery_errors[name]} {fallback}")
            elif snapshot is not None:
                catalog_line.setText(f"当前 Key 返回 {len(snapshot)} 个兼容模型。" if snapshot else "当前 Key 的列表未返回兼容模型。")
            else:
                catalog_line.setText("预设模型 · 保存 Key 后核对账号可用模型")

        def save():
            key = secret.text().strip()
            if not 8 <= len(key) <= 800 or any(char.isspace() for char in key):
                detail.setText("请输入有效的 API Key。")
                return
            try:
                self.store.set_key(name, key)
                secret.clear()
                detail.clear()
                self.owner.update_models()
                self.discover(name)
            except (ValueError, OSError) as exc:
                detail.setText(str(exc))

        def erase():
            if confirm(window, "移除 API Key", f"移除本机保存的 {provider['name']} Key？"):
                try:
                    self.store.set_key(name, None)
                except OSError as exc:
                    detail.setText(str(exc))
                    return
                self.discoveries.pop(name, None)
                self.discovery_errors.pop(name, None)
                detail.clear()
                self.owner.update_models()
                if self.store.key_status(name) == "env":
                    self.discover(name)
                else:
                    sync()

        def ping():
            nonlocal testing_request
            model = key_test.currentData()
            caption = key_test.currentText()
            testing_request = True
            sync()
            detail.setText("正在发送一条真实测试请求…")

            def done(_, error):
                nonlocal testing_request
                testing_request = False
                if not window.isVisible():
                    return
                detail.setText(error or f"连接成功 · {caption}")
                sync()
            self.owner.job(lambda: self.owner.engine.test_key(name, model), done)

        controls.addWidget(button("保存 Key", save, "Primary"))
        remove = button("移除", erase)
        controls.addWidget(remove)
        refresh_button = button("刷新模型", lambda: self.discover(name))
        controls.addWidget(refresh_button)
        controls.addStretch()
        controls.addWidget(button("获取 API Key ↗", lambda: QDesktopServices.openUrl(QUrl(provider["key_url"]))))
        test = button("测试连接", ping)
        testing.addWidget(test)
        self.model_panels[name] = sync
        sync()
        return panel

    def plugins(self):
        engine = self.owner.engine
        window, layout = base_dialog(self.owner, "MCP 插件", "已启用的插件会随工作台自动连接。", 800, 570)
        tabs = QTabWidget()
        layout.addWidget(tabs, 1)
        catalog = QWidget()
        columns = QHBoxLayout(catalog)
        names = QListWidget()
        names.setFixedWidth(248)
        columns.addWidget(names)
        right = QVBoxLayout()
        columns.addLayout(right, 1)
        title = label("", "Brand")
        description = label("", "Muted")
        description.setWordWrap(True)
        status = label("", "Accent")
        status.setWordWrap(True)
        token = QLineEdit()
        token.setEchoMode(QLineEdit.EchoMode.Password)
        endpoint = QLineEdit()
        endpoint.setPlaceholderText("已授权的 MCP 服务地址")
        tools = QPlainTextEdit()
        tools.setReadOnly(True)
        for item in (title, description, status, endpoint, token, tools):
            right.addWidget(item, 1 if item is tools else 0)
        actions = QHBoxLayout()
        right.addLayout(actions)
        tabs.addTab(catalog, "连接与预设")

        custom = QWidget()
        custom_layout = QVBoxLayout(custom)
        json_edit = QPlainTextEdit()
        custom_layout.addWidget(label("粘贴 mcpServers JSON，支持 stdio 与 Streamable HTTP。", "Muted"))
        custom_layout.addWidget(json_edit, 1)
        detail = label("", "Muted")
        detail.setWordWrap(True)
        custom_layout.addWidget(detail)
        custom_footer = QHBoxLayout()
        custom_layout.addLayout(custom_footer)
        tabs.addTab(custom, "自定义")
        ids = ["builtin-text", *PRESETS, *sorted(set(plugin_configuration(self.store)) - set(PRESETS))]

        def name_at(index):
            return ids[index] if 0 <= index < len(ids) else ids[0]

        def reload_json():
            json_edit.setPlainText(json.dumps({"mcpServers": plugin_configuration(self.store)}, ensure_ascii=False, indent=2))

        def saved(_, error):
            detail.setText(error or "已保存，正在更新连接。")

        def connect():
            self.owner.job(engine.refresh_plugins, saved)

        def save_json():
            try:
                config = validate(json.loads(json_edit.toPlainText())["mcpServers"])
                with self.store.lock:
                    self.store.state["plugins"] = config
                    self.store.save()
                ids[:] = ["builtin-text", *PRESETS, *sorted(set(config) - set(PRESETS))]
                names.clear()
                names.addItems([PRESETS.get(name, {}).get("title", name) if name != "builtin-text" else "内置文本工具" for name in ids])
                names.setCurrentRow(0)
                connect()
            except (KeyError, ValueError, OSError) as exc:
                detail.setText(str(exc))

        def toggle(enabled, erase=False):
            name = name_at(names.currentRow())
            if name not in PRESETS:
                return
            try:
                set_preset(self.store, name, enabled, "" if erase else token.text().strip() or None,
                           endpoint.text().strip() if PRESETS[name].get("endpoint") else None)
                token.clear()
                reload_json()
                connect()
            except (ValueError, OSError) as exc:
                detail.setText(str(exc))
                message(window, exc)

        enable = button("启用并连接", lambda: toggle(True), "Primary")
        disable = button("停用", lambda: toggle(False))
        erase = button("移除令牌", lambda: toggle(False, True) if confirm(window, "移除令牌", "移除本机加密凭证并停用插件？") else None)
        help_btn = button("官方说明 ↗", lambda: QDesktopServices.openUrl(QUrl(PRESETS[name_at(names.currentRow())]["docs"])))
        for entry in (enable, disable, erase, help_btn):
            actions.addWidget(entry)
        custom_footer.addStretch()
        custom_footer.addWidget(button("重新读取", reload_json))
        custom_footer.addWidget(button("保存并连接", save_json, "Primary"))
        last = None

        def show_details():
            nonlocal last
            name = name_at(names.currentRow())
            preset = PRESETS.get(name)
            found = {row["name"]: row for row in engine.plugins.snapshot()[0]}
            values = (name, found, tuple(self.store.state["sealed"]))
            if values == last:
                return
            last = values
            row = found.get(name, {})
            title.setText(preset["title"] if preset else names.currentItem().text() if names.currentItem() else "内置文本工具")
            description.setText(preset["description"] if preset else "可用工具会随插件启动自动发现。")
            state = {"connected": "已连接", "connecting": "正在连接…", "error": "连接失败"}.get(row.get("state"), "未启用")
            status.setText(state + (f' · {row["error"]}' if "error" in row else ""))
            tools.setPlainText("\n".join(row.get("tools", [])))
            for control in (enable, disable, help_btn):
                control.setEnabled(bool(preset))
            needs = bool(preset and "token" in preset)
            endpoint.setVisible(bool(preset and preset.get("endpoint")))
            token.setVisible(needs)
            erase.setVisible(needs)
            if needs:
                present = "mcp:" + name in self.store.state["sealed"]
                token.setPlaceholderText(preset["token"] + (" · 已保存" if present else ""))
                erase.setEnabled(present)

        names.addItems([PRESETS.get(name, {}).get("title", name) if name != "builtin-text" else "内置文本工具" for name in ids])
        names.setCurrentRow(0)
        def selection_changed(_):
            token.clear()
            chosen = name_at(names.currentRow())
            endpoint.setText(plugin_configuration(self.store).get(chosen, {}).get("url", ""))
            show_details()
        names.currentRowChanged.connect(selection_changed)
        reload_json()
        show_details()
        timer = QTimer(window)
        timer.timeout.connect(show_details)
        timer.start(600)
        window.exec()
        timer.stop()

    def wechat(self):
        bridge = self.owner.wechat
        if not bridge:
            message(self.owner, "Windows 无法读取本机微信凭证。")
            return
        window, layout = base_dialog(self.owner, "微信 ClawBot", "将 ClawBot 私聊文字带入当前任务，也可手动回复。", 790, 615)
        state = label(bridge.status, "Accent")
        layout.addWidget(state)
        controls = QHBoxLayout()
        layout.addLayout(controls)
        qr = QLabel()
        qr.setAlignment(Qt.AlignmentFlag.AlignCenter)
        qr.setVisible(False)
        layout.addWidget(qr)
        pin_row = QWidget()
        pin_layout = QHBoxLayout(pin_row)
        pin = QLineEdit()
        pin.setEchoMode(QLineEdit.EchoMode.Password)
        pin.setPlaceholderText("手机上显示的 PIN")
        pin_layout.addWidget(pin, 1)
        layout.addWidget(pin_row)
        pin_row.setVisible(False)
        layout.addWidget(separator())
        inbox = QHBoxLayout()
        layout.addLayout(inbox, 1)
        listing = QListWidget()
        listing.setMinimumWidth(210)
        inbox.addWidget(listing, 1)
        text = QPlainTextEdit()
        text.setReadOnly(True)
        text.setPlaceholderText("在手机的 ClawBot 对话中发送一条文字，即可在这里查看。")
        inbox.addWidget(text, 2)
        foot = QHBoxLayout()
        layout.addLayout(foot)
        entries = []

        def select(_index=-1):
            index = listing.currentRow()
            item = entries[index] if 0 <= index < len(entries) else None
            text.setPlainText(item["text"] if item else "")
            insert.setEnabled(bool(item))
            reply.setEnabled(bool(item and item.get("context")))
            return item

        def import_item():
            item = select()
            if item and self.owner.import_prompt(item["text"]):
                window.accept()

        def send_reply():
            item = select()
            if not item:
                return
            edit_window, edit_layout = base_dialog(window, "写回复", "发往当前绑定账户的 ClawBot 对话。", 600, 410)
            draft = QPlainTextEdit(self.owner.last_answer()[:3500])
            edit_layout.addWidget(draft, 1)
            feedback = label("", "Muted")
            edit_layout.addWidget(feedback)

            def send():
                send_btn.setEnabled(False)
                value = draft.toPlainText()

                def sent(_, error):
                    feedback.setText(error or "已发送。")
                    send_btn.setEnabled(bool(error))
                self.owner.job(lambda: bridge.reply(item, value), sent)
            send_btn = button("发送到微信", send, "Primary")
            edit_layout.addWidget(send_btn, alignment=Qt.AlignmentFlag.AlignRight)
            edit_window.exec()

        def start():
            try:
                bridge.start_bind()
            except ValueError as exc:
                state.setText(str(exc))

        def cancel():
            bridge.cancel_bind()
            qr.hide()
            pin_row.hide()
            bridge.update("已取消本次扫码。")

        def unlink():
            if confirm(window, "断开微信", "移除此应用保存的微信授权和本地收件箱？"):
                bridge.disconnect()

        def submit():
            try:
                bridge.set_pin(pin.text())
                pin.clear()
                pin_row.hide()
            except ValueError as exc:
                state.setText(str(exc))

        link = button("生成微信二维码", start, "Primary")
        stop = button("取消扫码", cancel)
        disconnect = button("断开本机关联", unlink)
        controls.addWidget(link)
        controls.addWidget(stop)
        controls.addWidget(disconnect)
        controls.addStretch()
        pin_layout.addWidget(button("提交 PIN", submit))
        pin.returnPressed.connect(submit)
        insert = button("带入任务草稿", import_item, "Primary")
        reply = button("写回复", send_reply)
        foot.addWidget(insert)
        foot.addWidget(reply)
        foot.addStretch()
        foot.addWidget(button("关闭", window.accept))
        listing.currentRowChanged.connect(select)

        def refresh(force=False):
            while True:
                try:
                    kind, value = bridge.events.get_nowait()
                except queue.Empty:
                    break
                if kind == "qr" and bridge.binding:
                    qr.setPixmap(qr_pixmap(value))
                    qr.show()
                elif kind == "pin" and bridge.binding:
                    pin_row.show()
                    pin.setFocus()
                elif kind in {"linked", "bind-ended"}:
                    qr.hide()
                    pin_row.hide()
                elif kind == "inbox":
                    force = True
            state.setText(bridge.status)
            window.setWindowTitle("微信 ClawBot · " + bridge.status)
            link.setEnabled(not bridge.account and not bridge.binding)
            stop.setEnabled(bridge.binding)
            disconnect.setEnabled(bool(bridge.account))
            if force:
                old = listing.currentRow()
                listing.blockSignals(True)
                entries[:] = bridge.messages()
                listing.clear()
                listing.addItems([" ".join(item["text"].split())[:28] for item in entries])
                if entries:
                    listing.setCurrentRow(max(0, min(old, len(entries) - 1)))
                listing.blockSignals(False)
                select()

        timer = QTimer(window)
        timer.timeout.connect(refresh)
        timer.start(350)
        refresh(True)
        window.exec()
        timer.stop()
        if bridge.binding:
            bridge.cancel_bind()


def qr_pixmap(text):
    code = qrcode.QRCode(border=4)
    code.add_data(text)
    code.make(fit=True)
    matrix = code.get_matrix()
    image = QImage(len(matrix), len(matrix), QImage.Format.Format_RGB32)
    image.fill(QColor("white"))
    black = QColor("black")
    for y, row in enumerate(matrix):
        for x, dark in enumerate(row):
            if dark:
                image.setPixelColor(x, y, black)
    size = max(2, 260 // len(matrix)) * len(matrix)
    return QPixmap.fromImage(image.scaled(size, size, Qt.AspectRatioMode.KeepAspectRatio,
                                          Qt.TransformationMode.FastTransformation))

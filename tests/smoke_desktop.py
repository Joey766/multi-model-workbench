from pathlib import Path
import sys
import tempfile
import threading
import time
import tkinter as tk
import unittest
from urllib.parse import parse_qs, urlsplit


BASE = Path(__file__).resolve().parents[1]
SCRATCH = BASE / "build" / "test-artifacts"
SCRATCH.mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(BASE / "src"))
from app import App
from catalog import effective_configuration, set_preset
from desktop import Desktop
from store import Store
from wechat import WeChat, allowed_host


def wait_for(check, timeout=12, root=None):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if root:
            root.update()
        result = check()
        if result:
            return result
        time.sleep(.05)
    raise AssertionError("State was not reached")


class DesktopSmoke(unittest.TestCase):
    def test_presets_and_encrypted_authentication(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as directory:
            store = Store(directory)
            set_preset(store, "github", True, "unit-test-preset-token")
            snapshot = Store(directory)
            self.assertNotIn("unit-test-preset-token", snapshot.path.read_text(encoding="utf-8"))
            actual = effective_configuration(snapshot)["github"]
            self.assertEqual(actual["headers"]["Authorization"], "Bearer unit-test-preset-token")
            self.assertEqual(actual["headers"]["X-MCP-Readonly"], "true")
            snapshot.state["plugins"]["github"]["url"] = "https://different.invalid/mcp"
            self.assertNotIn("Authorization", effective_configuration(snapshot)["github"]["headers"])

    def test_wechat_pin_inbox_and_explicit_reply(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as directory:
            store = Store(directory)
            requests = []
            cancellation = threading.Event()
            own, other = "wxid_owner@im.wechat", "wxid_other@im.wechat"

            def message(sender, identifier, group_id=""):
                return {"from_user_id": sender, "message_type": 1, "message_id": identifier,
                        "group_id": group_id, "context_token": "context-secret",
                        "item_list": [{"type": 1, "text_item": {"text": "来自手机的任务"}}]}

            def transport(base, endpoint, body=None, token=None, timeout=35):
                requests.append((endpoint, body))
                if endpoint.startswith("get_bot_qrcode"):
                    return {"qrcode": "mock-qr", "qrcode_img_content": "https://example.invalid/wechat-mock"}
                if endpoint.startswith("get_qrcode_status"):
                    values = parse_qs(urlsplit(endpoint).query)
                    if values.get("verify_code") != ["123456"]:
                        return {"status": "need_verifycode"}
                    return {"status": "confirmed", "bot_token": "wx-private-token-888", "ilink_user_id": own,
                            "ilink_bot_id": "bot-test@im.bot", "baseurl": "https://ilinkai.weixin.qq.com"}
                if endpoint == "getupdates":
                    if body["get_updates_buf"]:
                        cancellation.wait(.2)
                        return {"ret": 0, "msgs": [], "get_updates_buf": "cursor"}
                    return {"ret": 0, "get_updates_buf": "cursor", "msgs": [
                        message(other, 2), message(own, 3, "my-group"), message(own, 5), message(own, 5)]}
                if endpoint == "sendmessage":
                    return {"ret": 0}
                raise AssertionError(endpoint)

            bridge = WeChat(store, transport)
            try:
                bridge.start_bind()
                seen = []
                def pin_requested():
                    while not bridge.events.empty():
                        seen.append(bridge.events.get_nowait()[0])
                    return "pin" in seen
                wait_for(pin_requested)
                self.assertIn("qr", seen)
                bridge.set_pin("123456")
                wait_for(lambda: len(bridge.messages()) == 1)
                saved = Store(directory)
                self.assertEqual(saved.unseal("wechat-account")["user"], own)
                self.assertNotIn("wx-private-token-888", saved.path.read_text(encoding="utf-8"))
                self.assertNotIn("context-secret", saved.path.read_text(encoding="utf-8"))
                self.assertFalse(any(endpoint == "sendmessage" for endpoint, _ in requests))
                bridge.reply(bridge.messages()[0], "明确点击发送的内容")
                payload = next(body for endpoint, body in requests if endpoint == "sendmessage")
                self.assertEqual(payload["msg"]["to_user_id"], own)
                self.assertEqual(payload["msg"]["context_token"], "context-secret")
                bridge.disconnect()
                self.assertIsNone(Store(directory).unseal("wechat-account"))
                self.assertEqual(bridge.messages(), [])
                with self.assertRaises(ValueError):
                    allowed_host("https://ilinkai.weixin.qq.com.example.invalid")
            finally:
                bridge.close()
                cancellation.set()

    def test_native_application_offline_flow(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as directory:
            root = tk.Tk()
            root.withdraw()
            app = App(directory)
            ui = Desktop(root, app)
            try:
                wait_for(lambda: app.plugins.snapshot()[0][0]["state"] == "connected", root=root)
                ui.demo()
                for kind in ["edit", "command", "plugin"]:
                    pending = wait_for(lambda: ui.pending and ui.pending["kind"] == kind and ui.pending, root=root)
                    ui.review_pending()
                    root.update()
                    self.assertTrue(ui.review_window.winfo_exists())
                    if kind == "edit":
                        self.assertIn("app.py", ui.review_window.title())
                    ui.decide(pending, True, ui.review_window)
                wait_for(lambda: ui.task["messages"][-1]["role"] == "assistant", root=root)
                self.assertIn("退出码 0", ui.task["messages"][-1]["text"])
                self.assertIn('"valid": true', ui.task["messages"][-1]["text"])
                self.assertEqual(ui.task["changes"][0]["status"], "applied")
                ui.show_models()
                ui.show_plugins()
                ui.show_wechat()
                root.update()
                self.assertTrue(ui.model_window.window.winfo_exists())
                self.assertTrue(ui.plugin_window.window.winfo_exists())
                self.assertTrue(ui.wechat_window.window.winfo_exists())
                ui.wechat_window.draw("https://example.invalid/native-qr")
                self.assertGreater(len(ui.wechat_window.canvas.find_all()), 200)
            finally:
                ui.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)

import base64
from datetime import datetime, timezone
import hashlib
import json
import queue
import secrets
import threading
import time
from urllib import error, request
from urllib.parse import urlencode, urlsplit
import uuid

from providers import HTTP, json_text


BASE = "https://ilinkai.weixin.qq.com"
BASE_INFO = {"channel_version": "3.0.1/MultiModelAgent-2.0.0", "bot_agent": "MultiModelAgent/2.0.0"}
MAX_MESSAGES = 80


def allowed_host(value):
    if not isinstance(value, str) or not value:
        raise ValueError("微信返回的服务地址无效。")
    url = urlsplit(value if "://" in value else "https://" + value)
    if (url.scheme != "https" or not url.hostname or not url.hostname.endswith(".weixin.qq.com")
            or url.username or url.password or url.port not in {None, 443} or url.query
            or url.fragment or url.path not in {"", "/"}):
        raise ValueError("微信返回了不受支持的服务地址。")
    return "https://" + url.hostname


def wx_request(base, endpoint, body=None, token=None, timeout=35):
    headers = {"Accept": "application/json", "iLink-App-Id": "bot",
               "iLink-App-ClientVersion": str((3 << 16) | 1)}
    if body is None:
        contents = None
    else:
        contents = json_text(body).encode("utf-8")
        headers.update({"Content-Type": "application/json", "AuthorizationType": "ilink_bot_token",
                        "X-WECHAT-UIN": base64.b64encode(str(secrets.randbits(32)).encode()).decode()})
    if token:
        headers["Authorization"] = "Bearer " + token
    try:
        req = request.Request(allowed_host(base) + "/ilink/bot/" + endpoint, headers=headers, data=contents)
        with HTTP.open(req, timeout=timeout) as result:
            data = json.loads(result.read(2_000_000))
    except error.HTTPError as exc:
        exc.close()
        raise RuntimeError(f"微信服务返回 HTTP {exc.code}。") from None
    except (error.URLError, OSError, ValueError) as exc:
        raise ConnectionError("暂时无法访问腾讯微信服务。") from exc
    if not isinstance(data, dict):
        raise RuntimeError("微信返回了无法识别的响应。")
    return data


def inbound(message, account):
    if (message.get("message_type") != 1 or message.get("group_id")
            or message.get("from_user_id") != account["user"]):
        return None
    parts = [item.get("text_item", {}).get("text") for item in message.get("item_list", [])
             if item.get("type") == 1]
    text = "\n".join(part.strip() for part in parts if isinstance(part, str) and part.strip())[:15000]
    if not text:
        return None
    raw_id = message.get("message_id") or message.get("client_id") or message.get("seq")
    stable = str(raw_id) if raw_id is not None else json_text(message)
    return {"id": hashlib.sha256((account["bot"] + ":" + stable).encode()).hexdigest()[:32], "text": text,
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "context": message.get("context_token", "")}


class WeChat:
    def __init__(self, store, transport=wx_request):
        self.store = store
        self.transport = transport
        self.events = queue.SimpleQueue()
        self.pin = queue.Queue(maxsize=1)
        self.bind_stop = threading.Event()
        self.poll_stop = threading.Event()
        self.lock = threading.RLock()
        self.account = store.unseal("wechat-account", None)
        self.status = "已保存微信连接" if self.account else "尚未绑定微信"
        self.binding = False
        self.inbox = store.unseal("wechat-inbox", [])
        self.duplicate_ids = {item["id"] for item in self.inbox}

    def update(self, status):
        with self.lock:
            self.status = status
        self.events.put(("status", status))

    def start_bind(self):
        with self.lock:
            if self.binding:
                return
            if self.account:
                raise ValueError("请先在工作台中断开已绑定账号。")
            self.binding = True
            self.bind_stop = threading.Event()
            self.pin = queue.Queue(maxsize=1)
            threading.Thread(target=self.bind, args=(self.bind_stop,), daemon=True).start()

    def set_pin(self, value):
        value = value.strip()
        if not value.isascii() or not value.isdigit() or not 4 <= len(value) <= 12:
            raise ValueError("请输入手机微信显示的数字 PIN。")
        try:
            self.pin.put_nowait(value)
        except queue.Full:
            pass

    def bind(self, stopped):
        try:
            self.update("正在向微信请求二维码…")
            issued = self.transport(BASE, "get_bot_qrcode?bot_type=3", {"local_token_list": []}, timeout=18)
            qr, payload = issued.get("qrcode"), issued.get("qrcode_img_content")
            if not isinstance(qr, str) or not isinstance(payload, str) or not qr or not 8 <= len(payload) <= 2500:
                raise RuntimeError("微信未返回有效的二维码。")
            if stopped.is_set():
                return
            self.events.put(("qr", payload))
            self.update("请用手机微信扫码，并在手机上确认连接。")
            base, pin = BASE, ""
            expires = time.monotonic() + 280
            while not stopped.is_set() and time.monotonic() < expires:
                query = {"qrcode": qr}
                if pin:
                    query["verify_code"] = pin
                try:
                    result = self.transport(base, "get_qrcode_status?" + urlencode(query))
                except ConnectionError:
                    if stopped.wait(1):
                        return
                    continue
                if stopped.is_set():
                    return
                status = result.get("status", "")
                if status == "confirmed":
                    token = result.get("bot_token")
                    user, bot = result.get("ilink_user_id"), result.get("ilink_bot_id")
                    if not all(isinstance(value, str) and value for value in (token, user, bot)):
                        raise RuntimeError("微信未返回完整的账户授权信息。")
                    account = {"token": token, "user": user, "bot": bot, "url": allowed_host(result.get("baseurl") or base),
                               "cursor": ""}
                    with self.lock:
                        if stopped.is_set():
                            return
                        self.store.seal("wechat-account", account)
                        self.account = account
                    self.events.put(("linked", None))
                    self.start_poll()
                    return
                if "need_verifycode" in status:
                    self.update("请在此窗口输入手机微信显示的绑定 PIN。")
                    self.events.put(("pin", None))
                    pin = ""
                    while not stopped.is_set() and time.monotonic() < expires:
                        try:
                            pin = self.pin.get(timeout=.25)
                            break
                        except queue.Empty:
                            continue
                    if not pin:
                        return
                elif status == "scaned_but_redirect":
                    base = allowed_host(result.get("redirect_host"))
                    pin = ""
                elif status == "scaned":
                    pin = ""
                    self.update("已扫码，等待在手机微信确认…")
                elif status in {"expired", "verify_code_blocked"}:
                    raise RuntimeError("本次二维码已失效，请点「生成微信二维码」重试。")
                elif status == "binded_redirect":
                    raise RuntimeError("微信提示已绑定。请先在手机微信的 ClawBot 管理中确认当前连接。")
                elif status not in {"", "wait"}:
                    raise RuntimeError("微信没有完成本次授权，请在手机上检查提示。")
                stopped.wait(.35)
            if not stopped.is_set():
                raise RuntimeError("等待授权超时，请重新生成二维码。")
        except (ValueError, RuntimeError, ConnectionError) as exc:
            self.update(str(exc))
        finally:
            with self.lock:
                self.binding = False
            self.events.put(("bind-ended", None))

    def start_poll(self):
        with self.lock:
            if not self.account or (getattr(self, "poll_thread", None) and self.poll_thread.is_alive()
                                    and not self.poll_stop.is_set()):
                return
            self.poll_stop = threading.Event()
            self.poll_thread = threading.Thread(target=self.poll, args=(self.account, self.poll_stop), daemon=True)
            self.poll_thread.start()

    def poll(self, account, stopped):
        self.update("已绑定微信，正在接收 ClawBot 消息。")
        timeout = 35
        while not stopped.is_set():
            try:
                result = self.transport(account["url"], "getupdates",
                                        {"get_updates_buf": account["cursor"], "base_info": BASE_INFO},
                                         account["token"], timeout=timeout + 5)
                if stopped.is_set():
                    return
                if result.get("ret", 0) == -14 or result.get("errcode", 0) == -14:
                    self.update("微信连接已过期，请断开后重新扫码。")
                    return
                if result.get("ret", 0) or result.get("errcode", 0):
                    raise RuntimeError("微信暂时未接受接收请求。")
                delay = result.get("longpolling_timeout_ms", 35000)
                timeout = min(60, max(5, delay / 1000)) if isinstance(delay, (int, float)) else 35
                cursor = result.get("get_updates_buf")
                received = False
                with self.lock:
                    if stopped.is_set() or self.account is not account:
                        return
                    for message in result.get("msgs", []):
                        item = inbound(message, account)
                        if item and item["id"] not in self.duplicate_ids:
                            received = True
                            self.duplicate_ids.add(item["id"])
                            self.inbox.append(item)
                    if received:
                        self.inbox = self.inbox[-MAX_MESSAGES:]
                        self.duplicate_ids = {item["id"] for item in self.inbox}
                        self.store.seal("wechat-inbox", self.inbox)
                    if isinstance(cursor, str) and cursor and cursor != account["cursor"]:
                        account["cursor"] = cursor
                        self.store.seal("wechat-account", account)
                if received:
                    self.events.put(("inbox", None))
                if self.status != "已绑定微信，正在接收 ClawBot 消息。":
                    self.update("已绑定微信，正在接收 ClawBot 消息。")
                stopped.wait(.4)
            except (RuntimeError, ValueError, ConnectionError, TypeError):
                if not stopped.is_set():
                    self.update("微信已绑定，网络暂不可用，正在重试。")
                    stopped.wait(4)

    def messages(self):
        with self.lock:
            return list(reversed(self.inbox))

    def reply(self, item, text):
        with self.lock:
            account = self.account
            if not account or not any(x["id"] == item["id"] for x in self.inbox) or not item.get("context"):
                raise ValueError("需要当前绑定账号的一条可回复消息。")
        text = text.strip()
        if not text or len(text) > 3500:
            raise ValueError("回复需要 1–3500 字符。")
        result = self.transport(account["url"], "sendmessage", {"base_info": BASE_INFO, "msg": {
            "from_user_id": "", "to_user_id": account["user"], "client_id": "mma-" + uuid.uuid4().hex,
            "message_type": 2, "message_state": 2, "context_token": item["context"],
            "item_list": [{"type": 1, "text_item": {"text": text}}],
        }}, account["token"], timeout=20)
        if result.get("ret", 0) or result.get("errcode", 0):
            raise RuntimeError("微信未接受回复，请确认连接状态。")

    def cancel_bind(self):
        self.bind_stop.set()

    def disconnect(self):
        with self.lock:
            self.poll_stop.set()
            self.bind_stop.set()
            self.account = None
            self.inbox = []
            self.duplicate_ids.clear()
            self.store.seal("wechat-account", None)
            self.store.seal("wechat-inbox", None)
        self.update("本机已断开微信。")
        self.events.put(("inbox", None))

    def close(self):
        self.poll_stop.set()
        self.bind_stop.set()

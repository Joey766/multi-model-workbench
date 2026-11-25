import json
import os
import queue
import re
import shutil
import subprocess
import threading
import time
from urllib import error, request
from urllib.parse import urlsplit

from providers import Cancelled, HTTP, json_text, sse
from outlook import LocalOutlook
from text_plugin import TOOLS as TEXT_TOOLS, execute_tool
from workspace import child_environment


MODERN = "2026-07-28"
LEGACY = "2025-11-25"
CLIENT = {"name": "MultiModelAgent", "version": "1.0.0"}
NAME = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,31}")
REFERENCE = re.compile(r"\$\{([a-zA-Z_][a-zA-Z_0-9]*)\}")


def expand(value):
    def environment(match):
        name = match.group(1)
        if not os.environ.get(name):
            raise ValueError(f"未设置插件环境变量 {name}。")
        return os.environ[name]
    return REFERENCE.sub(environment, value)


def validate(config):
    if not isinstance(config, dict) or len(config) > 15:
        raise ValueError("需要 MCP 服务器配置对象，最多 15 个。")
    for name, spec in config.items():
        if name == "builtin-text" or not NAME.fullmatch(name) or not isinstance(spec, dict):
            raise ValueError("插件名称须为 1–32 个英文字母、数字、点、横线或下划线。")
        if type(spec.get("enabled", True)) is not bool:
            raise ValueError(f"{name} 的 enabled 须为布尔值。")
        if name == "outlook" and spec.get("kind") == "outlook" and set(spec) <= {"kind", "enabled"}:
            continue
        if ("command" in spec) == ("url" in spec):
            raise ValueError(f"{name} 需要指定 command 或 url。")
        if "command" in spec:
            if not isinstance(spec["command"], str) or not spec["command"].strip():
                raise ValueError(f"{name} 缺少启动命令。")
            if not isinstance(spec.get("args", []), list) or not all(isinstance(x, str) for x in spec.get("args", [])):
                raise ValueError(f"{name} 的 args 须为字符串数组。")
            fields = ["env"]
        else:
            if not isinstance(spec["url"], str):
                raise ValueError(f"{name} 的 URL 无效。")
            url = urlsplit(spec["url"])
            if (not url.hostname or url.username or url.password or url.fragment or
                (url.scheme != "https" and not (url.scheme == "http" and url.hostname in {"127.0.0.1", "localhost", "::1"}))):
                raise ValueError(f"{name} 需要 HTTPS URL，或本机 HTTP URL。")
            fields = ["headers"]
        for field in fields:
            mapping = spec.get(field, {})
            if not isinstance(mapping, dict) or not all(isinstance(v, str) for v in mapping.values()):
                raise ValueError(f"{name} 的 {field} 须为字符串映射。")
    return config


class Rpc:
    def __init__(self, spec, directory):
        self.spec = spec
        self.directory = directory
        self.version = MODERN
        self.session = None
        self.current_id = 0
        self.process = None
        self.outbox = queue.Queue(maxsize=96)
        self.lock = threading.Lock()
        self.headers = {k: expand(v) for k, v in spec.get("headers", {}).items()}
        self.closed = threading.Event()

    def start(self):
        if "command" in self.spec:
            flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            env = {**child_environment(), **{k: expand(v) for k, v in self.spec.get("env", {}).items()}}
            executable = shutil.which(self.spec["command"], path=env.get("PATH")) or self.spec["command"]
            self.process = subprocess.Popen([executable, *self.spec.get("args", [])],
                                            cwd=self.directory, env=env, stdin=subprocess.PIPE,
                                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, creationflags=flags)
            threading.Thread(target=self.reader, daemon=True).start()
        try:
            discovered = self.call("server/discover", {}, timeout=3)
            if not isinstance(discovered, dict):
                raise ValueError("MCP discovery response is invalid.")
        except (OSError, ValueError, RuntimeError):
            self.version = LEGACY
            self.session = None
            result = self.call("initialize", {"protocolVersion": LEGACY, "capabilities": {}, "clientInfo": CLIENT},
                               timeout=15)
            self.version = result["protocolVersion"]
            self.call("notifications/initialized", {}, notify=True)

    def reader(self):
        try:
            while not self.closed.is_set():
                line = self.process.stdout.readline(1_000_001)
                if not line or len(line) > 1_000_000:
                    break
                try:
                    response = json.loads(line)
                except (ValueError, UnicodeError):
                    continue
                if isinstance(response, dict):
                    self.outbox.put(response, timeout=1)
        except (OSError, queue.Full, ValueError):
            pass
        finally:
            try:
                self.outbox.put_nowait(None)
            except queue.Full:
                pass

    def send(self, message):
        self.process.stdin.write((json_text(message) + "\n").encode())
        self.process.stdin.flush()

    def receive_stdio(self, packet, timeout, stop):
        self.send(packet)
        if "id" not in packet:
            return None
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            if self.closed.is_set() or stop and stop.is_set():
                raise Cancelled()
            try:
                item = self.outbox.get(timeout=.15)
            except queue.Empty:
                continue
            if item is None:
                raise RuntimeError("插件进程已退出。")
            if "method" in item and "id" in item:
                reply = {"jsonrpc": "2.0", "id": item["id"]}
                if item["method"] == "ping":
                    reply["result"] = {}
                else:
                    reply["error"] = {"code": -32601, "message": "Client method unavailable"}
                self.send(reply)
            elif item.get("id") == packet["id"]:
                return item
        raise RuntimeError("插件响应超时。")

    def receive_http(self, packet, timeout, stop):
        method = packet["method"]
        headers = {**self.headers, "Content-Type": "application/json", "Accept": "application/json, text/event-stream",
                   "MCP-Protocol-Version": self.version}
        if self.version == MODERN:
            headers["Mcp-Method"] = method
            if method == "tools/call":
                headers["Mcp-Name"] = packet["params"]["name"]
        elif self.session:
            headers["Mcp-Session-Id"] = self.session
        req = request.Request(self.spec["url"], data=json_text(packet).encode(), headers=headers)
        try:
            with HTTP.open(req, timeout=timeout) as res:
                if method == "initialize":
                    self.session = res.headers.get("Mcp-Session-Id")
                if "id" not in packet:
                    return None
                if res.headers.get_content_type() == "text/event-stream":
                    for item in sse(res):
                        if stop and stop.is_set():
                            raise Cancelled()
                        if item.get("id") == packet["id"]:
                            return item
                    raise RuntimeError("插件数据流未返回相应结果。")
                return json.loads(res.read(1_000_001))
        except error.HTTPError as exc:
            exc.close()
            raise RuntimeError(f"插件返回 HTTP {exc.code}。") from None

    def call(self, method, params, timeout=30, stop=None, notify=False):
        with self.lock:
            if self.closed.is_set() or stop and stop.is_set():
                raise Cancelled()
            self.current_id += 1
            if self.version == MODERN:
                params = {**params, "_meta": {
                    "io.modelcontextprotocol/protocolVersion": MODERN,
                    "io.modelcontextprotocol/clientInfo": CLIENT,
                    "io.modelcontextprotocol/clientCapabilities": {},
                }}
            packet = {"jsonrpc": "2.0", "method": method, "params": params}
            if not notify:
                packet["id"] = self.current_id
            receive = self.receive_stdio if "command" in self.spec else self.receive_http
            response = receive(packet, timeout, stop)
            if notify:
                return None
            if not isinstance(response, dict) or response.get("id") != self.current_id:
                raise RuntimeError("插件返回了无法识别的响应。")
            if "error" in response:
                raise RuntimeError(f"插件调用失败（JSON-RPC {response['error'].get('code', '错误')}）。")
            return response.get("result", {})

    def close(self):
        self.closed.set()
        if self.process:
            try:
                if os.name == "nt" and self.process.poll() is None:
                    subprocess.run(["taskkill", "/PID", str(self.process.pid), "/T", "/F"],
                                   creationflags=subprocess.CREATE_NO_WINDOW,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3, check=False)
                self.process.terminate()
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
            except OSError:
                pass
            finally:
                self.process.stdin.close()
                self.process.stdout.close()


class LocalText:
    def start(self):
        pass

    def call(self, method, params, timeout=30, stop=None):
        if stop and stop.is_set():
            raise Cancelled()
        if method == "tools/list":
            return {"tools": TEXT_TOOLS}
        return execute_tool(params["name"], params["arguments"]["text"])

    def close(self):
        pass


class Plugins:
    def __init__(self, directory):
        self.directory = directory
        self.connections = {}
        self.tools = {}
        self.results = {}
        self.lock = threading.RLock()
        self.generation = 0

    def configure(self, specs):
        combined = {"builtin-text": {}, **specs}
        with self.lock:
            self.generation += 1
            generation = self.generation
            old = list(self.connections.values())
            self.connections, self.tools = {}, {}
            self.results = {name: {"name": name, "state": "connecting", "tools": []}
                            for name, spec in combined.items() if spec.get("enabled", True)}
        for rpc in old:
            rpc.close()
        for name, spec in combined.items():
            if spec.get("enabled", True):
                threading.Thread(target=self.connect, args=(generation, name, spec), daemon=True).start()

    def connect(self, generation, name, spec):
        rpc = None
        try:
            if name == "builtin-text":
                rpc = LocalText()
            elif name == "outlook" and spec.get("kind") == "outlook":
                rpc = LocalOutlook()
            else:
                rpc = Rpc(spec, self.directory)
            rpc.start()
            offered = []
            params = {}
            for _ in range(10):
                result = rpc.call("tools/list", params, timeout=15)
                offered.extend(result.get("tools", []))
                if not result.get("nextCursor") or len(offered) >= 30:
                    break
                params = {"cursor": result["nextCursor"]}
            entries = {}
            for index, remote in enumerate(offered[:30], 1):
                schema = remote.get("inputSchema", {})
                if not isinstance(schema, dict) or schema.get("type") != "object":
                    continue
                safe_name = re.sub("[^a-zA-Z0-9_]", "_", name)
                identifier = f"mcp_{safe_name[:24]}_{index}_{name.encode().hex()[-12:]}"
                entries[identifier] = {"name": identifier, "description": (name + " / " + remote["name"] +
                                        " — " + remote.get("description", ""))[:1800], "schema": schema,
                                        "plugin": name, "remote": remote["name"], "rpc": rpc}
            with self.lock:
                if generation == self.generation:
                    self.connections[name] = rpc
                    self.tools.update(entries)
                    self.results[name] = {"name": name, "state": "connected",
                                          "tools": [tool["remote"] for tool in entries.values()]}
                    return
        except Exception as exc:
            with self.lock:
                if generation == self.generation:
                    message = str(exc) if isinstance(exc, (RuntimeError, ValueError)) else type(exc).__name__
                    self.results[name] = {"name": name, "state": "error", "tools": [], "error": message[:200]}
        if rpc:
            rpc.close()

    def snapshot(self):
        with self.lock:
            return list(self.results.values()), dict(self.tools)

    def close(self):
        with self.lock:
            self.generation += 1
            for connection in self.connections.values():
                connection.close()
            self.connections.clear()

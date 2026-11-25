from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.request import Request, urlopen
from urllib.error import HTTPError


ROOT = Path(__file__).resolve().parents[1]
SCRATCH = ROOT / "build" / "test-artifacts"
SCRATCH.mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(ROOT / "src"))
import app as backend
import model_catalog
import providers
from plugins import MODERN, Rpc
from store import Store
from workspace import TOOLS, Workspace


def final_openai(text):
    return [{"type": "response.output_text.delta", "delta": text},
            {"type": "response.completed", "response": {"status": "completed", "output": [
                {"type": "message", "content": [{"type": "output_text", "text": text}]}]}}]


def claude_text(text):
    return [{"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn"}},
            {"type": "message_stop"}]


def gemini_response(part):
    return [{"candidates": [{"content": {"parts": [part]}, "finishReason": "STOP"}]}]


def deepseek_text(text):
    return [{"choices": [{"delta": {"content": text}, "finish_reason": "stop"}]}]


FIRST = {
    "openai": [{"type": "response.completed", "response": {"status": "completed", "output": [
        {"type": "reasoning", "encrypted_content": "encrypted"},
        {"type": "function_call", "call_id": "o1", "name": "list_files", "arguments": "{}"}]}}],
    "claude": [
        {"type": "content_block_start", "index": 0,
         "content_block": {"type": "thinking", "thinking": "", "signature": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": "signed"}},
        {"type": "content_block_start", "index": 1,
         "content_block": {"type": "tool_use", "id": "c1", "name": "list_files", "input": {}}},
        {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": "{}"}},
        {"type": "message_delta", "delta": {"stop_reason": "tool_use"}}, {"type": "message_stop"}],
    "gemini": gemini_response({"functionCall": {"name": "list_files", "args": {}, "id": "g1"}, "thoughtSignature": "signed"}),
    "deepseek": [{"choices": [{"finish_reason": "tool_calls", "delta": {"reasoning_content": "private",
                 "tool_calls": [{"index": 0, "id": "d1", "function": {"name": "list_files", "arguments": "{}"}}]}}]}],
}
LAST = {"openai": final_openai, "claude": claude_text,
        "gemini": lambda text: gemini_response({"text": text}), "deepseek": deepseek_text}


@contextmanager
def serving(server):
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield "http://127.0.0.1:" + str(server.server_port)
    finally:
        server.shutdown()
        server.server_close()
        worker.join()


def eventually(getter, timeout=15):
    expires = time.monotonic() + timeout
    while time.monotonic() < expires:
        value = getter()
        if value:
            return value
        time.sleep(.04)
    raise AssertionError("Timed out waiting for state")


class Smoke(unittest.TestCase):
    def test_discovery_filters_and_paginates_official_catalogs(self):
        fixtures = {
            "openai": [{"data": [{"id": "gpt-4.1-mini"}, {"id": "ft:gpt-4.1:fixture::sample"}, {"id": "gpt-4o-audio-preview"}]}],
            "claude": [{"data": [{"id": "claude-haiku-fixture"}], "has_more": True, "last_id": "claude-haiku-fixture"},
                       {"data": [{"id": "claude-sonnet-fixture", "max_tokens": 8192}]}],
            "gemini": [{"models": [{"name": "models/gemini-flash-fixture", "supportedGenerationMethods": ["generateContent"]}],
                        "nextPageToken": "abc=def"},
                       {"models": [{"name": "models/gemini-pro-fixture", "supportedGenerationMethods": ["generateContent"]},
                                   {"name": "models/gemini-pro-image", "supportedGenerationMethods": ["generateContent"]}]}],
            "deepseek": [{"data": [{"id": "deepseek-fixture", "output_modalities": ["text"], "input_modalities": ["text", "image"],
                                    "effort": {"supported_levels": ["low", "high"], "default_level": "high"}},
                                   {"id": "deepseek-tts", "output_modalities": ["audio"]}]}],
        }
        for provider, pages in fixtures.items():
            with self.subTest(provider=provider), patch.object(model_catalog.HTTP, "open", side_effect=[
                BytesIO(json.dumps(page).encode()) for page in pages
            ]) as http:
                models = model_catalog.fetch_models(provider, "sample-api-key")
                self.assertEqual(len(models), 1 if provider == "deepseek" else 2)
                self.assertTrue(all(options["provider"] == provider for options in models.values()))
                request = http.call_args.args[0]
                self.assertEqual(request.get_method(), "GET")
                self.assertNotIn("sample-api-key", request.full_url)
                if provider == "gemini":
                    self.assertIn("pageToken=abc%3Ddef", request.full_url)
                if provider == "claude":
                    self.assertIn("after_id=claude-haiku-fixture", request.full_url)

    def test_discovered_models_use_exact_api_id_and_cache_matches_key(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as folder:
            base = Path(folder)
            root = base / "project"
            root.mkdir()
            with patch.dict(os.environ, {"OPENAI_API_KEY": "first-fixture-key"}), patch.object(model_catalog.HTTP, "open") as http:
                http.return_value = BytesIO(b'{"data":[{"id":"gpt-4.1-mini"}]}')
                engine = backend.App(base / "app")
                engine.catalog.refresh("openai")
                engine = backend.App(base / "app")
                self.assertIn("gpt-4.1-mini", engine.catalog.available())
                self.assertNotIn("gpt-6.2-sol", engine.catalog.available())
                http.side_effect = OSError("offline")
                with self.assertRaises(ValueError):
                    engine.catalog.refresh("openai")
                task = engine.create(str(root))
                self.assertEqual(task["model"], "gpt-4.1-mini")
                with patch.object(providers, "wire", return_value=final_openai("OK")) as wire:
                    engine.start({"task": task["id"], "model": task["model"], "effort": "auto", "prompt": "hi", "mode": "ask"})
                    eventually(lambda: not engine.active(task["id"]))
                    self.assertEqual(engine.detail(task["id"])["messages"][-1]["text"], "OK")
                    body = wire.call_args.args[2]
                    self.assertEqual(body["model"], "gpt-4.1-mini")
                    self.assertNotIn("reasoning", body)
                with patch.dict(os.environ, {"OPENAI_API_KEY": "different-fixture-key"}):
                    self.assertNotIn("gpt-4.1-mini", engine.catalog.available())
                    self.assertIn("gpt-6.2-sol", engine.catalog.available())
                    self.assertEqual(engine.previous_provider(engine.detail(task["id"])["messages"]), "openai")

    def test_all_models_tool_round_trips(self):
        for model, spec in providers.MODELS.items():
            provider = spec["provider"]
            if provider == "demo":
                continue
            with self.subTest(model=model):
                adapter = providers.ADAPTERS[provider]
                recorded = []
                def fake(url, headers, body, stop):
                    recorded.append((url, headers, body))
                    return iter(FIRST[provider] if len(recorded) == 1 else LAST[provider]("Verified"))
                with patch.object(providers, "wire", fake):
                    emit = []
                    args = (model, spec["efforts"][-1], "test-never-a-real-key", "system",
                            [{"role": "user", "text": "Hello"}], TOOLS)
                    reply = adapter(*args, [], emit.append, threading.Event())
                    self.assertEqual(reply.calls[0].name, "list_files")
                    answer = adapter(*args, [(reply, ["app.py"])], emit.append, threading.Event())
                    self.assertEqual(answer.text, "Verified")
                    self.assertEqual("".join(emit), "Verified")
                body = recorded[1][2]
                self.assertIn("app.py", json.dumps(body))
                if provider == "openai":
                    self.assertEqual(body["reasoning"]["effort"], "max")
                    self.assertEqual(body["input"][1]["encrypted_content"], "encrypted")
                    self.assertFalse(body["store"])
                elif provider == "claude":
                    self.assertEqual(body["messages"][1]["content"][0]["signature"], "signed")
                    self.assertEqual(body["messages"][2]["content"][0]["tool_use_id"], "c1")
                elif provider == "gemini":
                    self.assertEqual(body["contents"][1]["parts"][0]["thoughtSignature"], "signed")
                    self.assertEqual(body["contents"][2]["parts"][0]["functionResponse"]["id"], "g1")
                    self.assertEqual(":generateContent" in recorded[1][0], model.endswith("carbon"))
                else:
                    self.assertEqual(body["messages"][2]["reasoning_content"], "private")
                    self.assertEqual(body["messages"][3]["tool_call_id"], "d1")
                    self.assertEqual(body["reasoning_effort"], "max")

    def test_dpapi_and_edits(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as directory:
            base = Path(directory)
            data = Store(base / "data")
            data.set_key("openai", "unit-test-api-key-123")
            self.assertNotIn("unit-test-api-key-123", data.path.read_text(encoding="utf-8"))
            self.assertEqual(Store(data.directory).key("openai"), "unit-test-api-key-123")
            workspace = Workspace(base, [data.directory])
            (base / ".env").write_text("password=test", encoding="utf-8")
            with self.assertRaises(ValueError):
                workspace.resolve(".env")
            with self.assertRaises(ValueError):
                workspace.resolve("../escape")
            with self.assertRaises(ValueError):
                workspace.resolve("data/state.json")
            file = base / "source.py"
            file.write_bytes(b"\xef\xbb\xbfalpha\r\nbravo\nlast")
            change = workspace.proposal("edit_file", {"path": "source.py", "old_text": "alpha", "new_text": "hello"})
            workspace.apply(change)
            self.assertEqual(file.read_bytes(), b"\xef\xbb\xbfhello\r\nbravo\nlast")
            file.write_bytes(b"external")
            with self.assertRaises(ValueError):
                workspace.apply(change, reverse=True)
            self.assertEqual(file.read_bytes(), b"external")

    def test_mcp_http_and_stream(self):
        requests = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_POST(self):
                packet = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                requests.append((self.headers, packet))
                if self.path == "/events":
                    data = b'data: {"one":1}\n\ndata: [DONE]\n\n'
                    content_type = "text/event-stream"
                else:
                    data = json.dumps({"jsonrpc": "2.0", "id": packet["id"],
                                       "result": {"content": [{"type": "text", "text": "hello"}]}}).encode()
                    content_type = "application/json"
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(data)))
                if self.path == "/events":
                    self.send_header("X-RateLimit-Remaining-Requests", "17")
                self.end_headers()
                self.wfile.write(data)
        with serving(ThreadingHTTPServer(("127.0.0.1", 0), Handler)) as address:
            rpc = Rpc({"url": address + "/mcp"}, SCRATCH)
            rpc.start()
            response = rpc.call("tools/call", {"name": "example", "arguments": {}})
            self.assertEqual(response["content"][0]["text"], "hello")
            self.assertEqual(requests[-1][0]["Mcp-Name"], "example")
            self.assertEqual(requests[-1][0]["MCP-Protocol-Version"], MODERN)
            received = []
            token = providers.quota_listener.set(lambda status, headers: received.append((status, headers)))
            try:
                self.assertEqual(list(providers.wire(address + "/events", {}, {}, threading.Event())), [{"one": 1}])
            finally:
                providers.quota_listener.reset(token)
            self.assertEqual(received[0][0], 200)
            self.assertEqual(received[0][1]["X-RateLimit-Remaining-Requests"], "17")
            rpc.close()

    def test_demo_and_cross_provider_task(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as directory:
            directory = Path(directory)
            application = backend.App(directory / "data")
            try:
                with serving(backend.make_server(application)) as address:
                    with urlopen(address) as res:
                        html = res.read().decode()
                    token = re.search(r'name="agent-token" content="([^"]+)"', html).group(1)
                    def api(path, body=None, origin=None, auth=True):
                        headers = {"Authorization": "Bearer " + token} if auth else {}
                        if origin:
                            headers["Origin"] = origin
                        data = None
                        if body is not None:
                            headers["Content-Type"] = "application/json"
                            data = json.dumps(body).encode()
                        with urlopen(Request(address + "/api/" + path, headers=headers, data=data), timeout=10) as res:
                            return json.load(res)
                    with self.assertRaises(HTTPError) as no_token:
                        api("state", auth=False)
                    self.assertEqual(no_token.exception.code, 403)
                    no_token.exception.close()
                    with self.assertRaises(HTTPError) as no_origin:
                        api("task", {"demo": True}, origin="https://bad.example")
                    self.assertEqual(no_origin.exception.code, 403)
                    no_origin.exception.close()
                    eventually(lambda: api("state")["plugins"][0]["state"] == "connected")

                    demo = api("task", {"demo": True})
                    demo_id = demo["id"]
                    run = api("run", {"task": demo_id, "prompt": "演示", "model": "local-demo", "effort": "none"})["id"]
                    for kind in ["edit", "command", "plugin"]:
                        pending = eventually(lambda: (lambda a: a if a and a["kind"] == kind else None)(
                            api("run?task=" + demo_id)["approval"]))
                        if kind == "edit":
                            self.assertIn("你好", pending["diff"])
                            self.assertIn("Hello", (Path(demo["root"]) / "app.py").read_text(encoding="utf-8"))
                        api("decide", {"run": run, "id": pending["id"], "allow": True})
                    eventually(lambda: api("run?task=" + demo_id)["done"])
                    detail = api("task?id=" + demo_id)
                    output = detail["messages"][-1]
                    self.assertEqual(output["status"], "complete")
                    self.assertIn("退出码 0", output["text"])
                    self.assertIn('"valid": true', output["text"])
                    change = detail["changes"][0]
                    self.assertEqual(change["status"], "applied")
                    api("undo", {"task": demo_id, "change": change["id"]})
                    self.assertIn("Hello", (Path(demo["root"]) / "app.py").read_text(encoding="utf-8"))

                    project = directory / "sample-project"
                    project.mkdir()
                    task = api("task", {"root": str(project)})["id"]
                    for provider in ("openai", "gemini"):
                        api("key", {"provider": provider, "key": "test-placeholder-secret-" + provider})
                    def fake_adapter(model, effort, key, context, transcript, tools, history, emit, stop):
                        result = "ack: " + " | ".join(m["text"] for m in transcript)
                        emit(result)
                        return providers.Reply(result, [], None)
                    with patch.dict(backend.ADAPTERS, {"openai": fake_adapter, "gemini": fake_adapter}):
                        a = {"task": task, "model": "gpt-6.2-sol", "effort": "medium", "prompt": "first request"}
                        api("run", a)
                        eventually(lambda: api("run?task=" + task)["done"])
                        b = {"task": task, "model": "gemini-4-neon", "effort": "xhigh", "prompt": "follow up"}
                        with self.assertRaises(HTTPError) as no_share:
                            api("run", b)
                        self.assertEqual(no_share.exception.code, 400)
                        no_share.exception.close()
                        api("run", {**b, "share_context": True})
                        eventually(lambda: api("run?task=" + task)["done"])
                        result = api("task?id=" + task)["messages"][-1]
                        self.assertEqual(result["model"], "gemini-4-neon")
                        self.assertEqual(result["effort"], "xhigh")
                        self.assertIn("first request", result["text"])
                        self.assertIn("follow up", result["text"])
                    self.assertEqual(Store(directory / "data").state["tasks"][task]["model"], "gemini-4-neon")
            finally:
                application.plugins.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)

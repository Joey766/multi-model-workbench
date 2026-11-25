from dataclasses import dataclass
from contextvars import ContextVar
import json
import queue
import threading
from urllib import error, request


PROVIDERS = {
    "openai": {"name": "OpenAI", "env": "OPENAI_API_KEY", "key_url": "https://platform.openai.com/api-keys"},
    "gemini": {"name": "Google Gemini", "env": "GEMINI_API_KEY", "key_url": "https://aistudio.google.com/app/apikey"},
    "claude": {"name": "Anthropic Claude", "env": "ANTHROPIC_API_KEY", "key_url": "https://platform.claude.com/settings/keys"},
    "deepseek": {"name": "DeepSeek", "env": "DEEPSEEK_API_KEY", "key_url": "https://platform.deepseek.com/api_keys"},
}
LEVELS = ["low", "medium", "high", "xhigh", "max"]
MODELS = {
    "gpt-6.2-astra": {"provider": "openai", "name": "GPT-6.2 Astra", "efforts": LEVELS, "default": "medium"},
    "gpt-6.2-sol": {"provider": "openai", "name": "GPT-6.2 Sol", "efforts": LEVELS, "default": "medium"},
    "gemini-4-carbon": {"provider": "gemini", "name": "Gemini 4 Carbon", "efforts": LEVELS[:3], "default": "medium"},
    "gemini-4-neon": {"provider": "gemini", "name": "Gemini 4 Neon", "efforts": LEVELS[:4], "default": "high"},
    "claude-fable-5-2": {"provider": "claude", "name": "Claude Fable 5.2", "efforts": LEVELS, "default": "high"},
    "claude-opus-5-5": {"provider": "claude", "name": "Claude Opus 5.5", "efforts": LEVELS, "default": "medium"},
    "deepseek-v4-pro": {"provider": "deepseek", "name": "DeepSeek V4 Pro", "efforts": ["none", "low", "high", "max"], "default": "high"},
    "deepseek-flash": {"provider": "deepseek", "name": "DeepSeek V4.1 Flash", "efforts": ["none", "low", "high", "max"], "default": "high"},
    "local-demo": {"provider": "demo", "name": "本地演示", "efforts": ["none"], "default": "none"},
}


class Cancelled(Exception):
    pass


class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


HTTP = request.build_opener(NoRedirect())
quota_listener = ContextVar("quota_listener", default=None)
request_options = ContextVar("request_options", default={})


def json_text(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def sse(response):
    data = []
    for raw in response:
        line = raw.decode("utf-8").rstrip("\r\n")
        if line.startswith("data:"):
            data.append(line[5:].lstrip(" "))
        elif not line and data:
            payload = "\n".join(data)
            data.clear()
            if payload == "[DONE]":
                return
            yield json.loads(payload)
    if data and data != ["[DONE]"]:
        yield json.loads("\n".join(data))


def wire(url, headers, body, stop):
    """Keep a slow network read from holding the local task after Stop."""
    outgoing = queue.Queue(maxsize=64)
    closed = threading.Event()
    observer = quota_listener.get()

    def put(kind, payload):
        while not (stop.is_set() or closed.is_set()):
            try:
                outgoing.put((kind, payload), timeout=.2)
                return True
            except queue.Full:
                pass
        return False

    def fetch():
        try:
            req = request.Request(url, data=json_text(body).encode(), headers={
                "Content-Type": "application/json", "Accept": "text/event-stream, application/json", **headers})
            with HTTP.open(req, timeout=240) as res:
                if observer:
                    put("quota", (res.status, dict(res.headers.items())))
                packets = sse(res) if res.headers.get_content_type() == "text/event-stream" else [json.load(res)]
                for data in packets:
                    if not put("packet", data):
                        return
        except error.HTTPError as exc:
            if observer:
                put("quota", (exc.code, dict(exc.headers.items())))
            message = exc.read(4000).decode("utf-8", "replace")
            try:
                payload = json.loads(message)
                details = payload.get("error", payload)
                message = details.get("message", message) if isinstance(details, dict) else str(details)
            except (ValueError, AttributeError):
                pass
            for key in headers.values():
                if len(key) > 8:
                    message = message.replace(key, "[已隐藏]").replace(key.removeprefix("Bearer "), "[已隐藏]")
            put("error", RuntimeError(f"模型 API 返回 HTTP {exc.code}。{message[:550]}"))
        except Exception as exc:
            put("error", RuntimeError(f"模型连接中断或响应无法解析（{type(exc).__name__}）。"))
        finally:
            put("end", None)

    threading.Thread(target=fetch, daemon=True).start()
    try:
        while not stop.is_set():
            try:
                kind, payload = outgoing.get(timeout=.15)
            except queue.Empty:
                continue
            if kind == "error":
                raise payload
            if kind == "end":
                return
            if kind == "quota":
                observer(*payload)
                continue
            yield payload
        raise Cancelled()
    finally:
        closed.set()


@dataclass
class Call:
    name: str
    identifier: str
    args: object


@dataclass
class Reply:
    text: str
    calls: list
    raw: object
    warning: str = ""


def parsed_args(value):
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return None


def messages_for_chat(messages):
    # Keep adjacent user turns legal for providers that require alternating roles.
    result = []
    for msg in messages:
        role, content = msg["role"], msg["text"]
        if not content:
            continue
        if result and result[-1]["role"] == role:
            result[-1]["content"] += "\n\n" + content
        else:
            result.append({"role": role, "content": content})
        if msg.get("images"):
            result[-1].setdefault("images", []).extend(msg["images"])
    while result and result[0]["role"] != "user":
        result.pop(0)
    return result


def data_url(image):
    return f"data:{image['mime']};base64,{image['base64']}"


def image_content(items, provider):
    for item in items:
        pictures = item.pop("images", [])
        if not pictures:
            continue
        if provider == "openai":
            item["content"] = [{"type": "input_text", "text": item["content"]},
                               *({"type": "input_image", "image_url": data_url(pic), "detail": "auto"} for pic in pictures)]
        elif provider == "claude":
            item["content"] = [{"type": "text", "text": item["content"]},
                               *({"type": "image", "source": {"type": "base64", "media_type": pic["mime"], "data": pic["base64"]}}
                                 for pic in pictures)]
        else:
            item["content"] = [{"type": "text", "text": item["content"]},
                               *({"type": "image_url", "image_url": {"url": data_url(pic)}} for pic in pictures)]
    return items


def response_tools(tools):
    return [{"type": "function", "name": t["name"], "description": t["description"],
             "parameters": t["schema"], "strict": False} for t in tools]


def openai(model, effort, key, system, messages, tools, history, emit, stop):
    items = image_content(messages_for_chat(messages), "openai")
    for reply, outputs in history:
        items.extend(reply.raw)
        items.extend({"type": "function_call_output", "call_id": c.identifier, "output": out}
                     for c, out in zip(reply.calls, outputs))
    body = {"model": model, "instructions": system, "input": items, "stream": True, "store": False,
            "max_output_tokens": request_options.get().get("max_output", 32768), "tools": response_tools(tools)}
    if effort != "auto":
        body["reasoning"] = {"effort": effort}
    if not tools:
        body.pop("tools")
    final = None
    streamed = False
    for event in wire("https://api.openai.com/v1/responses", {"Authorization": "Bearer " + key}, body, stop):
        kind = event.get("type")
        if kind in ("response.output_text.delta", "response.refusal.delta"):
            delta = event.get("delta", "")
            streamed |= bool(delta)
            emit(delta)
        elif kind in ("response.completed", "response.incomplete"):
            final = event["response"]
        elif kind in ("response.failed", "error"):
            raise RuntimeError("OpenAI 未能完成这轮生成，请检查模型权限、额度或稍后重试。")
    if final is None:
        raise RuntimeError("OpenAI 数据流在完成前断开。")
    items = final.get("output", [])
    text = "\n".join(block.get("text", block.get("refusal", ""))
                     for item in items if item["type"] == "message" for block in item.get("content", []))
    if text and not streamed:
        emit(text)
    calls = [Call(item["name"], item["call_id"], parsed_args(item.get("arguments")))
             for item in items if item["type"] == "function_call"]
    warning = "模型达到本轮输出上限。" if final.get("status") == "incomplete" else ""
    return Reply(text, calls if not warning else [], items, warning)


def claude(model, effort, key, system, messages, tools, history, emit, stop):
    items = image_content(messages_for_chat(messages), "claude")
    for reply, outputs in history:
        items.append({"role": "assistant", "content": reply.raw})
        items.append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": c.identifier, "content": out}
            for c, out in zip(reply.calls, outputs)]})
    options = request_options.get()
    body = {"model": model, "system": system, "messages": items, "max_tokens": options.get("max_output", 32768), "stream": True,
            "tools": [{"name": t["name"], "description": t["description"], "input_schema": t["schema"]} for t in tools]}
    if effort != "auto":
        body["output_config"] = {"effort": effort}
        if options.get("adaptive", True):
            body["thinking"] = {"type": "adaptive"}
    if not tools:
        body.pop("tools")
    blocks, arguments = {}, {}
    reason, finished = None, False
    for event in wire("https://api.anthropic.com/v1/messages", {"x-api-key": key, "anthropic-version": "2023-06-01"}, body, stop):
        kind = event["type"]
        if kind == "content_block_start":
            index = event["index"]
            blocks[index] = dict(event["content_block"])
            if blocks[index].get("type") == "text":
                emit(blocks[index].get("text", ""))
        elif kind == "content_block_delta":
            index, delta = event["index"], event["delta"]
            field = {"text_delta": "text", "thinking_delta": "thinking", "signature_delta": "signature"}.get(delta["type"])
            if field:
                blocks[index][field] = blocks[index].get(field, "") + delta[field]
                if field == "text":
                    emit(delta[field])
            elif delta["type"] == "input_json_delta":
                arguments[index] = arguments.get(index, "") + delta["partial_json"]
        elif kind == "message_delta":
            reason = event["delta"].get("stop_reason")
        elif kind == "message_stop":
            finished = True
        elif kind == "error":
            raise RuntimeError("Claude 数据流返回错误，请稍后重试。")
    if not finished:
        raise RuntimeError("Claude 数据流在完成前断开。")
    for index, value in arguments.items():
        blocks[index]["input"] = parsed_args(value)
    ordered = [blocks[i] for i in sorted(blocks)]
    text = "\n".join(b["text"] for b in ordered if b["type"] == "text")
    calls = [Call(b["name"], b["id"], b.get("input")) for b in ordered if b["type"] == "tool_use"]
    warning = {"max_tokens": "模型达到本轮输出上限。", "refusal": "模型拒绝了此请求。"}.get(reason, "")
    return Reply(text, calls if not warning else [], ordered, warning)


def gemini(model, effort, key, system, messages, tools, history, emit, stop):
    contents = [{"role": "model" if m["role"] == "assistant" else "user",
                 "parts": [{"text": m["content"]},
                           *({"inlineData": {"mimeType": pic["mime"], "data": pic["base64"]}}
                             for pic in m.get("images", []))]}
                for m in messages_for_chat(messages)]
    for reply, outputs in history:
        contents.append({"role": "model", "parts": reply.raw})
        parts = []
        for call, output in zip(reply.calls, outputs):
            function = {"name": call.name, "response": {"output": output}}
            if call.identifier:
                function["id"] = call.identifier
            parts.append({"functionResponse": function})
        contents.append({"role": "user", "parts": parts})
    options = request_options.get()
    body = {"systemInstruction": {"parts": [{"text": system}]}, "contents": contents,
            "generationConfig": {"maxOutputTokens": options.get("max_output", 32768)},
            "tools": [{"functionDeclarations": [{"name": t["name"], "description": t["description"],
                                                  "parametersJsonSchema": t["schema"]} for t in tools]}]}
    if not tools:
        body.pop("tools")
    if effort != "auto":
        body["generationConfig"]["thinkingConfig"] = {"thinkingLevel": effort.upper()}
    stream_content = options.get("stream_content", model != "gemini-4-carbon")
    action = "streamGenerateContent?alt=sse" if stream_content else "generateContent"
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:{action}"
    parts, text, calls, reason = [], [], [], None
    for event in wire(url, {"x-goog-api-key": key}, body, stop):
        candidates = event.get("candidates", [])
        if not candidates and event.get("promptFeedback", {}).get("blockReason"):
            raise RuntimeError("Gemini 阻止了此请求。")
        for candidate in candidates[:1]:
            reason = candidate.get("finishReason", reason)
            for part in candidate.get("content", {}).get("parts", []):
                parts.append(part)
                if "text" in part and not part.get("thought"):
                    text.append(part["text"])
                    emit(part["text"])
                if "functionCall" in part:
                    call = part["functionCall"]
                    calls.append(Call(call["name"], call.get("id", ""), call.get("args", {})))
    if reason is None:
        raise RuntimeError("Gemini 数据流在完成前断开。")
    warning = "" if reason == "STOP" else f"Gemini 停止生成（{reason}）。"
    return Reply("".join(text), calls if not warning else [], parts, warning)


def deepseek(model, effort, key, system, messages, tools, history, emit, stop):
    items = [{"role": "system", "content": system}, *image_content(messages_for_chat(messages), "deepseek")]
    for reply, outputs in history:
        items.append(reply.raw)
        items.extend({"role": "tool", "tool_call_id": call.identifier, "content": out}
                     for call, out in zip(reply.calls, outputs))
    adapted_tools = [{"type": "function", "function": {k: v for k, v in t.items() if k not in {"strict", "type"}}}
                     for t in response_tools(tools)]
    body = {"model": model, "messages": items, "tools": adapted_tools, "stream": True,
            "max_tokens": request_options.get().get("max_output", 32768)}
    if not tools:
        body.pop("tools")
    if effort != "auto":
        body["thinking"] = {"type": "disabled" if effort == "none" else "enabled"}
        if effort != "none":
            body["reasoning_effort"] = effort
    text, reasoning, pieces, reason = [], [], {}, None
    for event in wire("https://api.deepseek.com/chat/completions", {"Authorization": "Bearer " + key}, body, stop):
        if event.get("error"):
            raise RuntimeError("DeepSeek 数据流返回错误，请稍后重试。")
        for choice in event.get("choices", [])[:1]:
            reason = choice.get("finish_reason") or reason
            delta = choice.get("delta", {})
            if delta.get("content"):
                text.append(delta["content"])
                emit(delta["content"])
            if delta.get("reasoning_content"):
                reasoning.append(delta["reasoning_content"])
            for change in delta.get("tool_calls") or []:
                tool = pieces.setdefault(change["index"], {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                tool["id"] += change.get("id") or ""
                for attr in ("name", "arguments"):
                    tool["function"][attr] += change.get("function", {}).get(attr) or ""
    if reason is None:
        raise RuntimeError("DeepSeek 数据流在完成前断开。")
    raw = {"role": "assistant", "content": "".join(text)}
    if reasoning or effort not in ("none", "auto"):
        raw["reasoning_content"] = "".join(reasoning)
    ordered = [pieces[i] for i in sorted(pieces)]
    if ordered:
        raw["tool_calls"] = ordered
    calls = [Call(item["function"]["name"], item["id"], parsed_args(item["function"]["arguments"])) for item in ordered]
    warning = "" if reason in {"stop", "tool_calls"} else f"DeepSeek 停止生成（{reason}）。"
    return Reply(raw["content"], calls if not warning else [], raw, warning)


ADAPTERS = {"openai": openai, "claude": claude, "gemini": gemini, "deepseek": deepseek}

IMAGE_MODELS = set(MODELS) - {"deepseek-v4-pro", "local-demo"}


def deepseek_balance(key):
    req = request.Request("https://api.deepseek.com/user/balance", headers={"Authorization": "Bearer " + key})
    try:
        with HTTP.open(req, timeout=15) as response:
            data = json.loads(response.read(30000))
    except error.HTTPError as exc:
        exc.close()
        raise ValueError(f"余额接口返回 HTTP {exc.code}。") from None
    except (OSError, ValueError) as exc:
        raise ValueError("无法读取 DeepSeek 余额。") from exc
    if not isinstance(data, dict) or not isinstance(data.get("balance_infos"), list):
        raise ValueError("DeepSeek 未返回可读的余额。")
    balances = []
    for item in data["balance_infos"]:
        if not isinstance(item, dict) or item.get("currency") not in {"CNY", "USD"}:
            continue
        amount = item.get("total_balance")
        if isinstance(amount, str) and len(amount) <= 30 and amount.replace(".", "", 1).isdecimal():
            balances.append((item["currency"], amount))
    return balances

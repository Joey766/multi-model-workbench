from datetime import date, datetime, timezone
from hashlib import sha256
import json
import re
from time import monotonic
from urllib import error, parse, request

from providers import HTTP, IMAGE_MODELS, LEVELS, MODELS, PROVIDERS


ENDPOINTS = {
    "openai": "https://api.openai.com/v1/models",
    "claude": "https://api.anthropic.com/v1/models",
    "gemini": "https://generativelanguage.googleapis.com/v1beta/models",
    "deepseek": "https://api.deepseek.com/models",
}
SPECIALIZED = re.compile(r"(?:^|[-.])(?:image|tts|audio|transcribe|realtime|live|search|embedding|moderation|dialogue|computer-use|nativeaudio|research)(?:$|[-.])")


def supported(value):
    return isinstance(value, dict) and value.get("supported") is True


def output_limit(value, default):
    return min(32768, value) if type(value) is int and value > 0 else default


def model_spec(provider, item):
    identifier = item.get("id")
    if provider == "gemini":
        resource = item.get("name")
        if not isinstance(resource, str) or not resource.startswith("models/"):
            return None
        identifier = resource.removeprefix("models/")
    if not isinstance(identifier, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9.:_\-]{0,239}", identifier):
        return None
    builtin = MODELS.get(identifier, {})
    if builtin and builtin["provider"] != provider:
        return None
    spec = {**builtin, "provider": provider, "discovered": True}
    default_limit = 32768 if builtin else 4096
    label = item.get({"claude": "display_name", "gemini": "displayName", "deepseek": "name"}.get(provider, "id"))
    spec["name"] = builtin.get("name") or (" ".join(label.split())[:120] if isinstance(label, str) else "") or identifier
    spec["max_output"] = default_limit
    spec["images"] = identifier in IMAGE_MODELS
    efforts = builtin.get("efforts", ["auto"])
    default = builtin.get("default", "auto")

    if provider == "openai":
        base = identifier.split(":", 2)[1] if identifier.startswith("ft:") else identifier
        # The OpenAI list gives IDs but does not declare which endpoint each model supports.
        if not builtin and (not re.match(r"^(?:gpt-[5-9](?:[.\-]|$)|gpt-4(?:o|\.\d)(?:[.\-]|$)|o[134](?:-|$)|chatgpt-4o(?:-|$))", base)
                            or SPECIALIZED.search(base)):
            return None
        shutdown = item.get("shutdown_date")
        try:
            if isinstance(shutdown, str) and date.fromisoformat(shutdown) <= datetime.now(timezone.utc).date():
                return None
        except ValueError:
            pass
        spec["images"] = spec["images"] or (not identifier.startswith("ft:") and base.startswith(("gpt-4", "gpt-5", "gpt-6", "o3", "o4")))
        if not builtin and (base.startswith(("gpt-5", "gpt-6", "o1", "o3", "o4"))):
            spec["max_output"] = 32768
    elif provider == "gemini":
        methods = item.get("supportedGenerationMethods")
        if (not identifier.startswith("gemini-") or SPECIALIZED.search(identifier)
                or not isinstance(methods, list) or "generateContent" not in methods):
            return None
        spec["stream_content"] = (identifier != "gemini-4-carbon" if builtin else "streamGenerateContent" in methods)
        spec["max_output"] = output_limit(item.get("outputTokenLimit"), default_limit)
        spec["images"] = True
        if item.get("thinking") is False:
            efforts = [default := "auto"]
    elif provider == "claude":
        if not identifier.startswith("claude-") or item.get("lifecycle") == "retired":
            return None
        cap = item.get("capabilities") or {}
        if isinstance(cap, dict) and cap:
            effort_cap = cap.get("effort")
            declared = [level for level in LEVELS if supported(effort_cap) and supported(effort_cap.get(level))]
            efforts = ["auto", *declared]
            thinking = cap.get("thinking") or {}
            types = thinking.get("types") or {} if isinstance(thinking, dict) else {}
            spec["adaptive"] = isinstance(types, dict) and supported(types.get("adaptive"))
            spec["images"] = supported(cap.get("image_input")) if "image_input" in cap else spec["images"]
        spec["max_output"] = output_limit(item.get("max_tokens"), default_limit)
    else:
        inputs, outputs = item.get("input_modalities"), item.get("output_modalities")
        if (not identifier.startswith("deepseek-") or SPECIALIZED.search(identifier)
                or (isinstance(inputs, list) and "text" not in inputs)
                or (isinstance(outputs, list) and "text" not in outputs)):
            return None
        spec["images"] = "image" in inputs if isinstance(inputs, list) else spec["images"]
        spec["max_output"] = output_limit(item.get("max_output_tokens"), default_limit)
        reasoning = item.get("effort")
        levels = reasoning.get("supported_levels") if isinstance(reasoning, dict) else None
        if isinstance(levels, list):
            efforts = ["auto", "none", *(level for level in LEVELS if level in levels)]
            default = reasoning.get("default_level", default)
    spec["efforts"] = efforts
    spec["default"] = default if default in efforts else efforts[0]
    return identifier, spec


def fetch_models(provider, key):
    headers = {
        "claude": {"x-api-key": key, "anthropic-version": "2026-10-21"},
        "gemini": {"x-goog-api-key": key},
    }.get(provider, {"Authorization": "Bearer " + key})
    params = {"limit": 200} if provider == "claude" else {"pageSize": 100} if provider == "gemini" else {}
    result = {}
    cursors = set()
    deadline = monotonic() + 45
    while True:
        remaining = deadline - monotonic()
        if remaining <= 0 or len(cursors) >= 30:
            break
        url = ENDPOINTS[provider]
        if params:
            url += "?" + parse.urlencode(params)
        query = request.Request(url, headers={"Accept": "application/json", **headers})
        try:
            with HTTP.open(query, timeout=min(12, remaining)) as response:
                body = response.read(4 * 1024 * 1024 + 1)
            if len(body) > 4 * 1024 * 1024:
                raise ValueError("catalog too large")
            payload = json.loads(body)
        except error.HTTPError as exc:
            exc.close()
            raise ValueError(f"读取模型失败（HTTP {exc.code}）。请检查 Key 的权限或稍后重试。") from None
        except (OSError, ValueError):
            raise ValueError("无法读取供应商的模型列表，请检查网络后重试。") from None

        items = payload.get("models" if provider == "gemini" else "data") if isinstance(payload, dict) else None
        if not isinstance(items, list) or ("error" in payload):
            raise ValueError("供应商返回了无法识别的模型列表。")
        for item in items:
            if isinstance(item, dict) and (entry := model_spec(provider, item)):
                result.setdefault(*entry)
        if provider == "gemini":
            cursor, param = payload.get("nextPageToken"), "pageToken"
            if not cursor:
                return result
        elif provider == "claude" and payload.get("has_more"):
            cursor, param = payload.get("last_id"), "after_id"
        else:
            return result
        if not isinstance(cursor, str) or not cursor or len(cursor) > 2048 or cursor in cursors:
            break
        cursors.add(cursor)
        params[param] = cursor
    raise ValueError("模型列表尚未读取完整，请重试。")


class ModelCatalog:
    def __init__(self, store):
        self.store = store
        self.inflight = {}
        self.saved_fingerprints = {}

    def snapshot(self, provider):
        with self.store.lock:
            cached = self.store.state.get("model_catalogs", {}).get(provider)
            if not cached:
                return None
            saved = self.store.state["keys"].get(provider)
            verified = self.saved_fingerprints.get(provider)
            if saved and verified and verified[0] == saved:
                fingerprint = verified[1]
            else:
                try:
                    key = self.store.key(provider)
                except ValueError:
                    return None
                fingerprint = sha256(key.encode()).hexdigest() if key else None
                if saved:
                    self.saved_fingerprints[provider] = (saved, fingerprint)
            if fingerprint == cached["credential"]:
                return cached["models"]
            return None

    def available(self):
        with self.store.lock:
            models = {}
            for provider in PROVIDERS:
                discovered = self.snapshot(provider)
                if discovered is None:
                    models.update((key, spec) for key, spec in MODELS.items() if spec["provider"] == provider)
                else:
                    models.update(discovered)
            models["local-demo"] = MODELS["local-demo"]
            return models

    def refresh(self, provider):
        with self.store.lock:
            key = self.store.key(provider)
            if not key:
                raise ValueError("请先保存 API Key。")
            ticket = self.inflight[provider] = object()
        try:
            models = fetch_models(provider, key)
            with self.store.lock:
                if self.inflight.get(provider) is not ticket or self.store.key(provider) != key:
                    return
                cache = self.store.state.setdefault("model_catalogs", {})
                previous = cache.get(provider)
                cache[provider] = {"credential": sha256(key.encode()).hexdigest(), "models": models}
                try:
                    self.store.save()
                except OSError:
                    if previous is None:
                        cache.pop(provider)
                    else:
                        cache[provider] = previous
                    raise
        finally:
            with self.store.lock:
                if self.inflight.get(provider) is ticket:
                    self.inflight.pop(provider)

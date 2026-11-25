from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from hashlib import sha256
import re
import threading


PANELS = {
    "openai": ("https://platform.openai.com/settings/organization/limits", "https://platform.openai.com/usage"),
    "claude": ("https://platform.claude.com/settings/limits", "https://platform.claude.com/settings/billing"),
    "gemini": ("https://aistudio.google.com/rate-limit?timeRange=last-28-days", "https://aistudio.google.com/billing"),
    "deepseek": ("https://api-docs.deepseek.com/quick_start/rate_limit/", "https://platform.deepseek.com/usage"),
}

NAMES = {
    "requests": "请求", "tokens": "Token", "project-tokens": "项目 Token",
    "input-tokens": "输入 Token", "output-tokens": "输出 Token",
}
OPENAI_FIELDS = ("requests", "tokens", "project-tokens")
CLAUDE_FIELDS = ("requests", "tokens", "input-tokens", "output-tokens")


def number(value):
    try:
        parsed = int(value)
        return parsed if parsed >= 0 else None
    except (ValueError, TypeError, OverflowError):
        return None


def reset_time(value, now, allow_seconds=False):
    if not value or len(value) > 128:
        return None
    if allow_seconds and value.isdecimal():
        return now + timedelta(seconds=min(int(value), 31_536_000))
    try:
        if "T" in value:
            end = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return end if end.tzinfo else None
        if "," in value:
            return parsedate_to_datetime(value)
    except (ValueError, TypeError, OverflowError):
        return None
    unit = r"(\d{1,8}(?:\.\d{1,4})?)(ms|s|m|h|d)"
    if not re.fullmatch(r"(?:" + unit + r")+", value):
        return None
    scale = {"ms": .001, "s": 1, "m": 60, "h": 3600, "d": 86400}
    seconds = sum(float(amount) * scale[kind] for amount, kind in re.findall(unit, value))
    return now + timedelta(seconds=min(seconds, 31_536_000))


def parse_usage(provider, status, headers, now=None):
    now = now or datetime.now(timezone.utc)
    headers = {name.lower(): value.strip() for name, value in headers.items() if isinstance(value, str)}
    buckets = []
    fields = OPENAI_FIELDS if provider == "openai" else CLAUDE_FIELDS if provider == "claude" else ()
    for kind in fields:
        def value(metric):
            key = f"anthropic-ratelimit-{kind}-{metric}" if provider == "claude" else f"x-ratelimit-{metric}-{kind}"
            return headers.get(key, "")

        available, limit = number(value("remaining")), number(value("limit"))
        resets = reset_time(value("reset"), now)
        if available is not None or limit is not None or resets:
            buckets.append({"name": NAMES[kind], "remaining": available, "limit": limit, "reset": resets})
    return {"seen": now, "status": status, "buckets": buckets,
            "retry": reset_time(headers.get("retry-after"), now, allow_seconds=True)}


class QuotaBook:
    def __init__(self):
        self.lock = threading.Lock()
        self.latest = {}

    def record(self, provider, model, key, status, headers):
        payload = parse_usage(provider, status, headers)
        owner = sha256(key.encode()).digest()
        with self.lock:
            self.latest[model, owner] = payload

    def current(self, model, key):
        owner = sha256(key.encode()).digest()
        with self.lock:
            return self.latest.get((model, owner))

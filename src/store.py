import base64
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import tempfile
import threading

from providers import PROVIDERS


def atomic_write(path, value):
    fd, name = tempfile.mkstemp(prefix=".mma-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as file:
            file.write(value)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class DataBlob(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_char))]


def dpapi(data, protect):
    if os.name != "nt":
        raise ValueError("此系统请通过环境变量设置 API Key。")
    crypt = ctypes.WinDLL("crypt32.dll", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32.dll", use_last_error=True)
    kernel.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel.LocalFree.restype = wintypes.HLOCAL
    operation = crypt.CryptProtectData if protect else crypt.CryptUnprotectData
    operation.argtypes = [ctypes.POINTER(DataBlob), ctypes.c_void_p, ctypes.c_void_p,
                          ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(DataBlob)]
    operation.restype = wintypes.BOOL
    buffer = ctypes.create_string_buffer(data)
    source = DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
    output = DataBlob()
    if not operation(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(output)):
        raise ValueError("Windows 无法读取或保存此 API Key。请使用原 Windows 用户，或重新填写。")
    try:
        return ctypes.string_at(output.data, output.size)
    finally:
        kernel.LocalFree(ctypes.cast(output.data, ctypes.c_void_p))


class Store:
    def __init__(self, directory):
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / "state.json"
        self.lock = threading.RLock()
        self.state = {"tasks": {}, "keys": {}, "plugins": {}, "sealed": {}}
        if self.path.exists():
            self.state.update(json.loads(self.path.read_text(encoding="utf-8")))

    def save(self):
        with self.lock:
            encoded = json.dumps(self.state, ensure_ascii=False, indent=2).encode("utf-8")
            atomic_write(self.path, encoded)

    def set_key(self, provider, value):
        with self.lock:
            if value is None:
                self.state["keys"].pop(provider, None)
            else:
                self.state["keys"][provider] = base64.b64encode(dpapi(value.encode(), True)).decode()
            self.save()

    def key(self, provider):
        with self.lock:
            stored = self.state["keys"].get(provider)
            if stored:
                return dpapi(base64.b64decode(stored), False).decode()
            return os.environ.get(PROVIDERS[provider]["env"], "").strip()

    def key_status(self, provider):
        if self.state["keys"].get(provider):
            return "saved"
        return "env" if os.environ.get(PROVIDERS[provider]["env"], "").strip() else "missing"

    def seal(self, name, value):
        with self.lock:
            if value is None:
                self.state["sealed"].pop(name, None)
            else:
                data = json.dumps(value, ensure_ascii=False).encode("utf-8")
                self.state["sealed"][name] = base64.b64encode(dpapi(data, True)).decode()
            self.save()

    def unseal(self, name, default=None):
        with self.lock:
            blob = self.state["sealed"].get(name)
            return json.loads(dpapi(base64.b64decode(blob), False)) if blob else default

    def redact(self, value):
        for provider in PROVIDERS:
            try:
                key = self.key(provider)
                if len(key) >= 8:
                    value = value.replace(key, "[API Key 已隐藏]")
            except ValueError:
                continue
        return value

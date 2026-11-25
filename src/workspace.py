import difflib
import locale
import os
from pathlib import Path, PureWindowsPath
import re
import shutil
import stat
import subprocess
import tempfile
import time

from providers import Cancelled
from store import atomic_write


MAX_FILE = 256 * 1024
SKIPPED = {".git", ".hg", ".svn", "__pycache__", "node_modules", "dist", "build", ".venv", "venv",
           ".idea", ".next", ".cache", ".pytest_cache", ".tox", ".aws", ".ssh", ".codex", ".agents"}
PRIVATE = {".npmrc", ".pypirc", ".netrc", ".git-credentials", "credentials.json", "id_rsa", "id_ed25519"}


def sensitive(part):
    name = part.casefold()
    return (name in PRIVATE or name == ".env" or
            (name.startswith(".env.") and not name.endswith((".example", ".sample", ".template"))) or
            name.endswith((".pem", ".p12", ".pfx", ".key", ".kdbx")) or
            any(marker in name for marker in ("service-account", "service_account", "client_secret")))


def child_environment():
    allowed = {"PATH", "PATHEXT", "TEMP", "TMP", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC",
               "LOCALAPPDATA", "APPDATA", "USERPROFILE", "HOME", "LANG", "LC_ALL", "PROGRAMFILES",
               "PROGRAMFILES(X86)", "PROGRAMDATA", "HOMEDRIVE", "HOMEPATH"}
    return {key: value for key, value in os.environ.items() if key.upper() in allowed}


def object_schema(required=(), **properties):
    return {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False}


def tool(name, description, schema):
    return {"name": name, "description": description, "schema": schema}


PATH = {"type": "string", "description": "Workspace-relative path, use forward slashes."}
STRING = {"type": "string"}
INT = {"type": "integer"}
TOOLS = [
    tool("list_files", "List project files under a relative directory. Omits credentials and dependency folders.",
         object_schema(path=PATH)),
    tool("read_file", "Read UTF-8 source with numbered lines. Defaults to the first 200 lines.",
         object_schema(["path"], path=PATH, start=INT, end=INT)),
    tool("search_project", "Case-insensitive literal text search in project files.",
         object_schema(["query"], query=STRING, path=PATH)),
    tool("git_status", "Show Git working tree status and current branch. Read-only.", object_schema()),
    tool("edit_file", "Propose replacing a unique exact UTF-8 text block. User reviews a diff before writing.",
         object_schema(["path", "old_text", "new_text"], path=PATH, old_text=STRING, new_text=STRING)),
    tool("create_file", "Propose creating a new relative UTF-8 file. User reviews a diff before writing.",
         object_schema(["path", "content"], path=PATH, content=STRING)),
    tool("run_command", "Propose a shell command such as a build or test. User must approve the exact command.",
         object_schema(["command"], command=STRING, cwd=PATH, timeout_seconds=INT)),
]


class Workspace:
    def __init__(self, root, protected=()):
        self.root = Path(root).resolve(strict=True)
        if not self.root.is_dir() or self.root == self.root.parent:
            raise ValueError("请选择一个具体项目文件夹。")
        self.protected = tuple(Path(path).resolve() for path in protected)
        if any(self.root.is_relative_to(path) for path in self.protected):
            raise ValueError("请选择工作台数据目录之外的项目。")

    def resolve(self, name=".", allow_root=False):
        if not isinstance(name, str) or not name or "\x00" in name:
            raise ValueError("需要有效的项目相对路径。")
        raw = PureWindowsPath(name)
        if raw.drive or raw.root or ".." in raw.parts or any(
            part.casefold() in SKIPPED or sensitive(part) or ":" in part or
            part.endswith((" ", ".")) or re.fullmatch(
                r"(?i)(?:con|prn|aux|nul|com[1-9¹²³]|lpt[1-9¹²³])(?:\..*)?", part)
            for part in raw.parts
        ):
            raise ValueError("路径超出项目范围，或文件被标记为敏感。")
        current = self.root
        for part in raw.parts:
            current = current / part
            try:
                info = current.lstat()
            except FileNotFoundError:
                continue
            if current.is_symlink() or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                raise ValueError("项目工具跳过符号链接和目录联接。")
            if stat.S_ISREG(info.st_mode) and info.st_nlink > 1:
                raise ValueError("项目工具跳过硬链接文件。")
        if (current == self.root and not allow_root or
            any(current.is_relative_to(path) for path in self.protected)):
            raise ValueError("此路径不可用于该项目工具。")
        return current

    def files(self, path, stop, limit=1500):
        folder = self.resolve(path, True)
        if folder.is_file():
            return [folder]
        if not folder.is_dir():
            raise ValueError("目录不存在。")
        files = []
        for base, directories, names in os.walk(folder, followlinks=False):
            if stop.is_set():
                raise Cancelled()
            for directory in list(directories):
                try:
                    self.resolve(str((Path(base) / directory).relative_to(self.root)))
                except (ValueError, OSError):
                    directories.remove(directory)
            directories.sort(key=str.casefold)
            for name in sorted(names, key=str.casefold):
                try:
                    entry = self.resolve(str((Path(base) / name).relative_to(self.root)))
                    if entry.is_file():
                        files.append(entry)
                except (ValueError, OSError):
                    pass
                if len(files) >= limit:
                    return files
        return files

    def read_bytes(self, path):
        if not path.is_file():
            raise ValueError("文件不存在，或不是普通文件。")
        with path.open("rb") as file:
            data = file.read(MAX_FILE + 1)
        if len(data) > MAX_FILE or b"\x00" in data:
            raise ValueError("此工具支持不超过 256 KiB 的 UTF-8 文本文件。")
        return data

    def read_text(self, path):
        return self.read_bytes(path).decode("utf-8-sig")

    def run_read(self, name, args, stop):
        if name == "list_files":
            items = self.files(args.get("path", "."), stop, 301)
            result = "\n".join(p.relative_to(self.root).as_posix() for p in items[:300])
            return result + ("\n（仅显示前 300 项）" if len(items) > 300 else "") or "没有符合条件的文件。"
        if name == "read_file":
            data = self.read_text(self.resolve(args["path"])).splitlines()
            start = max(1, int(args.get("start", 1)))
            end = min(int(args.get("end", start + 199)), start + 299)
            lines = "\n".join(f"{i}: {line}" for i, line in enumerate(data[start - 1:end], start))
            return f"{args['path']}（总行数 {len(data)}）\n{lines[:24000]}"
        if name == "search_project":
            needle = args["query"].casefold()
            if not needle.strip():
                raise ValueError("查询不能为空。")
            items = []
            for file in self.files(args.get("path", "."), stop):
                if stop.is_set():
                    raise Cancelled()
                try:
                    content = self.read_text(file)
                except (ValueError, OSError):
                    continue
                for index, line in enumerate(content.splitlines(), 1):
                    if needle in line.casefold():
                        items.append(f"{file.relative_to(self.root).as_posix()}:{index}: {line[:400]}")
                    if len(items) >= 75:
                        return "\n".join(items) + "\n（仅显示前 75 项）"
            return "\n".join(items) or "没有匹配。"
        if name == "git_status":
            git = shutil.which("git")
            if git is None:
                return "当前机器找不到 Git。"
            return execute([git, "--no-optional-locks", "-c", "core.fsmonitor=false", "status", "--short",
                            "--branch", "--untracked-files=normal", "--", "."], self.root, stop, 12)
        raise ValueError("未知项目工具。")

    def proposal(self, name, args):
        path = self.resolve(args["path"])
        if name == "create_file":
            if path.exists():
                raise ValueError("文件已存在，请先读取再编辑。")
            before, after = None, args["content"]
        else:
            data = self.read_bytes(path)
            before = data.decode("utf-8")
            bom = "\ufeff" if before.startswith("\ufeff") else ""
            body = before.removeprefix(bom) if bom else before
            newline = "\r\n" if "\r\n" in body and "\n" not in body.replace("\r\n", "") else "\n"
            normalized = body.replace("\r\n", "\n")
            old = args["old_text"].replace("\r\n", "\n")
            new = args["new_text"].replace("\r\n", "\n")
            if not old or normalized.count(old) != 1:
                raise ValueError("待替换文本需要恰好匹配一次。请重新读取指定行。")
            # Preserve all bytes outside the matched region, even for mixed line endings.
            offset = normalized.index(old)
            boundary = [0]
            cursor = 0
            while cursor < len(body):
                cursor += 2 if body.startswith("\r\n", cursor) else 1
                boundary.append(cursor)
            after = bom + body[:boundary[offset]] + new.replace("\n", newline) + body[boundary[offset + len(old)]:]
        if not isinstance(after, str) or len(after.encode("utf-8")) > MAX_FILE or "\0" in after:
            raise ValueError("只能写入不超过 256 KiB 的 UTF-8 文本。")
        if before == after:
            raise ValueError("没有需要应用的变化。")
        relative = path.relative_to(self.root).as_posix()
        old_label = "/dev/null" if before is None else "a/" + relative
        changes = list(difflib.unified_diff((before or "").splitlines(keepends=True), after.splitlines(keepends=True),
                                            fromfile=old_label, tofile="b/" + relative))
        diff = "".join(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n" for line in changes)
        if len(diff) > 500000:
            raise ValueError("差异过大，请分次修改。")
        return {"path": relative, "before": before, "after": after, "diff": diff}

    def apply(self, change, reverse=False):
        path = self.resolve(change["path"])
        expected, new = (change["after"], change["before"]) if reverse else (change["before"], change["after"])
        current = self.read_bytes(path).decode("utf-8") if path.exists() else None
        if current != expected:
            raise ValueError("文件从生成差异后已发生变化。已保留磁盘版本。")
        if new is None:
            path.unlink()
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(self.resolve(change["path"]), new.encode("utf-8"))

    def command(self, args):
        command = args["command"]
        if not isinstance(command, str) or not command.strip() or len(command) > 8000 or "\0" in command:
            raise ValueError("需要有效的命令。")
        folder = self.resolve(args.get("cwd", "."), True)
        if not folder.is_dir():
            raise ValueError("命令工作目录不存在。")
        timeout = min(180, max(1, int(args.get("timeout_seconds", 60))))
        program = (shutil.which("pwsh") or shutil.which("powershell")) if os.name == "nt" else shutil.which("sh")
        if not program:
            raise ValueError("找不到 PowerShell。")
        flags = ["-NoLogo", "-NoProfile", "-NonInteractive", "-Command"] if os.name == "nt" else ["-c"]
        return {"command": command, "cwd": str(folder), "timeout": timeout}, [program, *flags, command]


def execute(argv, cwd, stop, timeout):
    if stop.is_set():
        raise Cancelled()
    with tempfile.TemporaryFile() as output:
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        with subprocess.Popen(argv, cwd=cwd, env=child_environment(), stdin=subprocess.DEVNULL,
                              stdout=output, stderr=subprocess.STDOUT, creationflags=flags,
                              start_new_session=os.name != "nt") as process:
            expires = time.monotonic() + timeout
            timed_out = False
            while process.poll() is None:
                if stop.wait(.1) or time.monotonic() >= expires:
                    if os.name == "nt":
                        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], check=False,
                                       creationflags=flags, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    else:
                        import signal
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                    if process.poll() is None:
                        process.kill()
                    if stop.is_set():
                        raise Cancelled()
                    timed_out = True
                    break
            output.seek(0)
            data = output.read(30001)
            try:
                content = data[:30000].decode("utf-8")
            except UnicodeDecodeError:
                content = data[:30000].decode(locale.getpreferredencoding(False), "replace")
            status = "命令达到超时时间。" if timed_out else f"退出码 {process.wait()}"
            return status + "\n" + content + ("\n（输出已截断）" if len(data) > 30000 else "")

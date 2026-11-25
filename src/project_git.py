from pathlib import Path
import os
import shutil
import subprocess

from workspace import child_environment


def git(root, *args, timeout=15, checked=True, limit=400000):
    program = shutil.which("git")
    if not program:
        raise ValueError("找不到 Git。")
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    options = ["--no-optional-locks", "--no-pager", "--literal-pathspecs", "-c", "core.fsmonitor=false",
               "-c", "core.hooksPath=" + ("NUL" if os.name == "nt" else "/dev/null")]
    try:
        result = subprocess.run([program, *options, *args], cwd=root, capture_output=True, stdin=subprocess.DEVNULL,
                                env=child_environment(), creationflags=flags, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise ValueError("Git 操作超时。") from exc
    if result.returncode and checked:
        raise ValueError(result.stderr.decode("utf-8", "replace").strip()[:650] or "Git 操作失败。")
    output = result.stdout[:limit].decode("utf-8", "replace")
    return output + ("\n[输出已截断]" if len(result.stdout) > limit else "")


def inventory(root):
    inside = git(root, "rev-parse", "--is-inside-work-tree", checked=False).strip()
    if inside != "true":
        return None, []
    branch = git(root, "branch", "--show-current").strip() or "Detached HEAD"
    prefix = git(root, "rev-parse", "--show-prefix").rstrip("\r\n")
    records = iter(git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all", "--", ".").split("\0"))
    entries = []
    for record in records:
        if len(record) < 4:
            continue
        state, name = record[:2], record[3:]
        if "R" in state or "C" in state:
            previous = next(records, "")
        else:
            previous = ""
        if prefix and not name.startswith(prefix):
            continue
        entries.append((state, name.removeprefix(prefix),
                        previous.removeprefix(prefix) if previous.startswith(prefix) else ""))
    return branch, entries


def difference(workspace, path, state):
    file = workspace.resolve(path)
    relative = file.relative_to(workspace.root).as_posix()
    if state == "??":
        data = workspace.read_text(file)
        lines = data.splitlines()
        return f"--- /dev/null\n+++ b/{relative}\n@@ -0,0 +1,{len(lines)} @@\n" + "".join("+" + line + "\n" for line in lines)
    return git(workspace.root, "diff", "HEAD", "--no-ext-diff", "--no-textconv", "--", relative,
               checked=False) or git(workspace.root, "diff", "--no-ext-diff", "--no-textconv", "--", relative)


def add_worktree(root, branch, destination):
    path = Path(destination).resolve()
    source = Path(git(root, "rev-parse", "--show-toplevel").strip()).resolve()
    if path.exists() or path.is_relative_to(source):
        raise ValueError("工作树需要一个项目之外且尚不存在的文件夹。")
    if not branch or branch.startswith("-") or branch == "@{-1}" or git(root, "check-ref-format", "--branch", branch).strip() != branch:
        raise ValueError("请输入有效的新分支名。")
    path.parent.mkdir(parents=True, exist_ok=True)
    git(root, "worktree", "add", "-b", branch, "--", str(path), "HEAD", timeout=80)
    return path

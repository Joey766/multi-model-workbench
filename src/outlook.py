import base64
import json
import os
from pathlib import Path
import subprocess

from providers import Cancelled, json_text
from workspace import child_environment, object_schema, STRING, INT


TOOLS = [
    {"name": "search_inbox", "description": "Search recent messages by subject or sender in the default Outlook Inbox. Read-only.",
     "inputSchema": object_schema(query=STRING, limit=INT)},
    {"name": "read_inbox_message", "description": "Read a plaintext message from search_inbox. Read-only.",
     "inputSchema": object_schema(["message_id"], message_id=STRING)},
]


SCRIPT = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
try {
    $outlook = New-Object -ComObject Outlook.Application
    $session = $outlook.GetNamespace('MAPI')
    if ($session.Accounts.Count -lt 1) { throw 'no-profile' }
    $operation = $env:MMA_OUTLOOK_OPERATION
    if ($operation -eq 'status') {
        [Console]::WriteLine('{"ready":true}')
        exit 0
    }
    $inbox = $session.GetDefaultFolder(6)
    if ($operation -eq 'read') {
        $mail = $session.GetItemFromID($env:MMA_OUTLOOK_ID, $inbox.StoreID)
        if ($mail.Class -ne 43 -or $mail.Parent.EntryID -ne $inbox.EntryID) { throw 'not-in-inbox' }
        $body = [string]$mail.Body
        if ($body.Length -gt 10000) { $body = $body.Substring(0, 10000) + ' …' }
        $result = @{subject = [string]$mail.Subject; sender = [string]$mail.SenderName;
                    received = $mail.ReceivedTime.ToString('s'); body = $body}
    } else {
        $items = $inbox.Items
        $items.Sort('[ReceivedTime]', $true)
        $needle = [string]$env:MMA_OUTLOOK_QUERY
        $limit = [int]$env:MMA_OUTLOOK_LIMIT
        $hits = @()
        for ($index = 1; $index -le [Math]::Min(250, $items.Count) -and $hits.Count -lt $limit; $index++) {
            $mail = $items.Item($index)
            if ($mail.Class -ne 43) { continue }
            $subject = [string]$mail.Subject
            $sender = [string]$mail.SenderName
            $bySubject = $subject.IndexOf($needle, [StringComparison]::OrdinalIgnoreCase)
            $bySender = $sender.IndexOf($needle, [StringComparison]::OrdinalIgnoreCase)
            if ($needle -and $bySubject -lt 0 -and $bySender -lt 0) { continue }
            $hits += @{subject = $subject; sender = $sender; received = $mail.ReceivedTime.ToString('s');
                       message_id = [string]$mail.EntryID}
        }
        $result = @{matches = $hits; search_scope = 'default Inbox, latest 250 items'}
    }
    [Console]::WriteLine((ConvertTo-Json -InputObject $result -Depth 4 -Compress))
} catch {
    exit 3
}
"""


class LocalOutlook:
    def start(self):
        self.run("status")

    def close(self):
        pass

    def call(self, method, params, timeout=30, stop=None):
        if stop and stop.is_set():
            raise Cancelled()
        if method == "tools/list":
            return {"tools": TOOLS}
        arguments = params["arguments"]
        if params["name"] == "read_inbox_message":
            value = arguments.get("message_id", "")
            if not isinstance(value, str) or len(value) > 2048 or not value.isascii() or not value.isalnum():
                raise ValueError("请选择搜索结果中的邮件 ID。")
            result = self.run("read", MMA_OUTLOOK_ID=value, stop=stop)
        elif params["name"] == "search_inbox":
            value = arguments.get("query", "")
            if not isinstance(value, str) or len(value) > 160 or "\0" in value:
                raise ValueError("请提供不超过 160 字符的关键词。")
            limit = arguments.get("limit", 8)
            if type(limit) is not int:
                raise ValueError("结果数量须为整数。")
            result = self.run("search", MMA_OUTLOOK_QUERY=value, MMA_OUTLOOK_LIMIT=str(max(1, min(20, limit))),
                              stop=stop)
        else:
            raise ValueError("未知 Outlook 工具。")
        return {"content": [{"type": "text", "text": json_text(result)}]}

    def run(self, operation, stop=None, **variables):
        if os.name != "nt":
            raise ValueError("此插件需要 Windows 上的经典 Outlook。")
        exe = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        encoded = base64.b64encode(SCRIPT.encode("utf-16le")).decode()
        argv = [str(exe), "-NoLogo", "-NoProfile", "-NonInteractive", "-STA", "-EncodedCommand", encoded]
        env = {**child_environment(), "MMA_OUTLOOK_OPERATION": operation, **variables}
        with subprocess.Popen(argv, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW) as process:
            for _ in range(100):
                try:
                    result, _ = process.communicate(timeout=.25)
                    break
                except subprocess.TimeoutExpired:
                    if stop and stop.is_set():
                        process.kill()
                        process.communicate()
                        raise Cancelled()
            else:
                process.kill()
                process.communicate()
                raise RuntimeError("Outlook 未响应。请在经典 Outlook 中完成账户设置后重试。")
        if process.returncode:
            raise RuntimeError("无法读取经典 Outlook 的默认邮箱。请确认已登录并按 Outlook 提示授权。")
        return json.loads(result.decode("utf-8-sig"))

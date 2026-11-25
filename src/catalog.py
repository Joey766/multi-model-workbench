from copy import deepcopy


PRESETS = {
    "outlook": {
        "title": "Outlook · 只读",
        "description": "使用已登录的经典 Outlook 查询默认收件箱。每次读取由你批准。",
        "docs": "https://learn.microsoft.com/en-us/office/vba/api/outlook.namespace.getdefaultfolder",
        "config": {"kind": "outlook"},
    },
    "xiaohongshu": {
        "title": "小红书",
        "description": "接入你已有授权的 MCP 服务。官方账号授权需应用审核，目前公开的范围为基础资料。",
        "docs": "https://openaccount.xiaohongshu.com/docs/quick-start",
        "config": {},
        "endpoint": True,
    },
    "microsoft-learn": {
        "title": "Microsoft Learn",
        "description": "搜索微软官方文档和代码示例。",
        "docs": "https://learn.microsoft.com/en-us/training/support/mcp-developer-reference",
        "config": {"url": "https://learn.microsoft.com/api/mcp"},
    },
    "deepwiki": {
        "title": "DeepWiki",
        "description": "查询公开 GitHub 仓库的文档和代码背景。",
        "docs": "https://docs.devin.ai/work-with-devin/deepwiki-mcp",
        "config": {"url": "https://mcp.deepwiki.com/mcp"},
    },
    "context7": {
        "title": "Context7",
        "description": "查询框架和依赖的版本文档，可选 API Key。",
        "docs": "https://context7.com/dashboard",
        "token": "Context7 API Key（可选）",
        "config": {"url": "https://mcp.context7.com/mcp"},
    },
    "github": {
        "title": "GitHub · 只读",
        "description": "查询仓库、Issue 和 Pull Request，需填 GitHub 访问令牌。",
        "docs": "https://github.com/settings/personal-access-tokens",
        "token": "GitHub 访问令牌",
        "required": True,
        "config": {"url": "https://api.githubcopilot.com/mcp/",
                   "headers": {"X-MCP-Readonly": "true", "X-MCP-Toolsets": "repos,issues,pull_requests"}},
    },
    "figma-desktop": {
        "title": "Figma Desktop",
        "description": "读取设计上下文。先在 Figma 桌面端的 Dev Mode 启用 MCP。",
        "docs": "https://developers.figma.com/docs/figma-mcp-server/local-server-installation/",
        "config": {"url": "http://127.0.0.1:3845/mcp"},
    },
    "playwright": {
        "title": "Playwright",
        "description": "启动隔离的 Microsoft Edge 测试会话。首次启用会运行 npm 下载官方插件。",
        "docs": "https://github.com/microsoft/playwright-mcp",
        "config": {"command": "npx", "args": ["-y", "@playwright/mcp@latest", "--isolated",
                                             "--browser", "msedge", "--image-responses", "omit"]},
    },
}


def plugin_configuration(store):
    with store.lock:
        return deepcopy(store.state["plugins"])


def effective_configuration(store):
    config = plugin_configuration(store)
    for name, preset in PRESETS.items():
        spec = config.get(name)
        if not spec or not preset.get("token") or spec.get("url") != preset["config"].get("url"):
            continue
        try:
            token = store.unseal("mcp:" + name)
        except ValueError:
            # GUI can recover by allowing the operator to replace an old Windows credential.
            token = None
        if token:
            headers = spec.setdefault("headers", {})
            headers.update(preset["config"].get("headers", {}))
            headers["Authorization"] = "Bearer " + token
    return config


def set_preset(store, name, enabled, token=None, endpoint=None):
    with store.lock:
        preset = PRESETS[name]
        if token is not None:
            if token and (len(token) > 2048 or any(char.isspace() for char in token)):
                raise ValueError("令牌格式无效。")
            if not preset.get("token"):
                raise ValueError("此插件不需要令牌。")
            store.seal("mcp:" + name, token or None)
        if enabled and preset.get("required") and not store.unseal("mcp:" + name):
            raise ValueError("请填写此插件的访问令牌。")
        config = deepcopy(preset["config"])
        if preset.get("endpoint"):
            address = (endpoint or store.state["plugins"].get(name, {}).get("url", "")).strip()
            if not address:
                raise ValueError("需要填写现有的已授权 MCP 服务地址。")
            config["url"] = address
            from plugins import validate
            validate({name: config})
        store.state["plugins"][name] = {**config, "enabled": enabled}
        store.save()

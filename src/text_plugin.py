import json
import sys


TOOLS = [{
    "name": "validate_json",
    "description": "Parse a JSON string and report its top-level type, keys, or the exact syntax error.",
    "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
}, {
    "name": "count_text",
    "description": "Count lines, characters and UTF-8 bytes in a text string.",
    "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
}]


def execute_tool(name, text):
    if name == "validate_json":
        try:
            value = json.loads(text)
            output = {"valid": True, "type": type(value).__name__,
                      "keys": list(value)[:50] if isinstance(value, dict) else []}
        except ValueError as exc:
            output = {"valid": False, "message": str(exc)}
    elif name == "count_text":
        output = {"lines": len(text.splitlines()), "characters": len(text), "utf8_bytes": len(text.encode())}
    else:
        raise KeyError("Unknown tool")
    return {"content": [{"type": "text", "text": json.dumps(output, ensure_ascii=False)}]}


def main():
    for line in sys.stdin.buffer:
        try:
            msg = json.loads(line)
            if "id" not in msg:
                continue
            method, params = msg["method"], msg.get("params", {})
            response = {"jsonrpc": "2.0", "id": msg["id"]}
            if method in {"server/discover", "initialize"}:
                result = {"protocolVersion": params.get("protocolVersion", "2026-07-28"),
                          "serverInfo": {"name": "builtin-text", "version": "1.0"}, "capabilities": {"tools": {}}}
            elif method == "tools/list":
                result = {"tools": TOOLS}
            elif method == "tools/call":
                result = execute_tool(params["name"], params["arguments"]["text"])
            elif method == "ping":
                result = {}
            else:
                response["error"] = {"code": -32601, "message": "Method not found"}
            if "error" not in response:
                response["result"] = result
        except (TypeError, ValueError, KeyError):
            response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32602, "message": "Invalid request"}}
        print(json.dumps(response, ensure_ascii=True), flush=True)


if __name__ == "__main__":
    main()

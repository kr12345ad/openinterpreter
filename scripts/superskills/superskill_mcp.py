#!/usr/bin/env python3
"""MCP stdio server for one Omnia superskill.

Omnia superskills pair instructions with a JSON-RPC bridge. Open Interpreter
already has skills and MCP, so this adapter exposes each bridge method as an MCP
tool and lets the importer ship the instructions as a normal SKILL.md.
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

DEFAULT_TIMEOUT_MS = 30_000
MAX_LINE_BYTES = 4 * 1024 * 1024
PROTOCOL_VERSION = "2025-03-26"


class BridgeError(RuntimeError):
    pass


def log(message: str) -> None:
    print(f"superskill-mcp: {message}", file=sys.stderr, flush=True)


def split_command(command: str) -> List[str]:
    tokens: List[str] = []
    current: List[str] = []
    quote: Optional[str] = None
    started = False
    escaped = False
    for char in command.strip():
        if escaped:
            current.append(char)
            escaped = False
            started = True
            continue
        if char == "\\" and quote != "'":
            escaped = True
            started = True
            continue
        if quote is not None:
            if char == quote:
                quote = None
            else:
                current.append(char)
            continue
        if char in ('"', "'"):
            quote = char
            started = True
            continue
        if char in (" ", "\t"):
            if started:
                tokens.append("".join(current))
                current = []
                started = False
            continue
        current.append(char)
        started = True
    if escaped:
        current.append("\\")
    if started:
        tokens.append("".join(current))
    return tokens


def load_skill(path: str, skill_id: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    skills = data.get("superskills") if isinstance(data, dict) else None
    if not isinstance(skills, list):
        raise BridgeError(f"{path} is not an Omnia superskill registry")
    for item in skills:
        if isinstance(item, dict) and item.get("id") == skill_id:
            return item
    raise BridgeError(f"superskill {skill_id!r} was not found in {path}")


def method_schema(method: Dict[str, Any]) -> Dict[str, Any]:
    declared = method.get("parameters")
    properties: Dict[str, Any] = {}
    required: List[str] = []
    if isinstance(declared, dict):
        for name, rule in declared.items():
            if not isinstance(name, str) or not name:
                continue
            rule = rule if isinstance(rule, dict) else {}
            prop: Dict[str, Any] = {}
            kind = rule.get("type")
            if kind in ("string", "number", "integer", "boolean", "array", "object"):
                prop["type"] = kind
            else:
                prop["type"] = "string"
            if isinstance(rule.get("description"), str):
                prop["description"] = rule["description"]
            properties[name] = prop
            if rule.get("required") is True:
                required.append(name)
    schema: Dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema


def validate_args(method: Dict[str, Any], arguments: Dict[str, Any]) -> None:
    declared = method.get("parameters")
    if not isinstance(declared, dict) or not declared:
        return
    required = [name for name, rule in declared.items() if isinstance(rule, dict) and rule.get("required") is True]
    missing = [name for name in required if name not in arguments]
    if missing:
        raise BridgeError(f"{method.get('name')} requires: {', '.join(missing)}")
    unknown = [name for name in arguments if name not in declared]
    if unknown:
        raise BridgeError(f"{method.get('name')} does not accept: {', '.join(unknown)}")


class StdioBridge:
    def __init__(self, skill: Dict[str, Any]) -> None:
        self.skill = skill
        self.bridge = skill.get("bridge") or {}
        self.child: Optional[subprocess.Popen[str]] = None
        self.lines: "queue.Queue[Optional[str]]" = queue.Queue()
        self.lock = threading.Lock()
        self.sequence = 0

    def _start(self) -> subprocess.Popen[str]:
        if self.child is not None and self.child.poll() is None:
            return self.child
        tokens = split_command(str(self.bridge.get("command") or ""))
        if not tokens:
            raise BridgeError("the superskill bridge command is empty")
        env = os.environ.copy()
        extra = self.bridge.get("env")
        if isinstance(extra, dict):
            env.update({str(key): str(value) for key, value in extra.items()})
        self.lines = queue.Queue()
        child = subprocess.Popen(
            tokens,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
            env=env,
        )
        self.child = child

        def read() -> None:
            assert child.stdout is not None
            for line in child.stdout:
                if len(line) > MAX_LINE_BYTES:
                    self.lines.put(None)
                    return
                self.lines.put(line)
            self.lines.put(None)

        threading.Thread(target=read, name="superskill-bridge-reader", daemon=True).start()
        return child

    def close(self) -> None:
        child = self.child
        self.child = None
        if child is not None and child.poll() is None:
            try:
                child.kill()
            except OSError:
                pass

    def call(self, method: str, params: Any) -> Any:
        with self.lock:
            child = self._start()
            assert child.stdin is not None
            self.sequence += 1
            request_id = self.sequence
            payload = json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
            try:
                child.stdin.write(payload + "\n")
                child.stdin.flush()
            except (BrokenPipeError, OSError) as error:
                self.close()
                raise BridgeError(f"could not write to the superskill bridge: {error}") from error
            timeout = (self.bridge.get("timeoutMs") or DEFAULT_TIMEOUT_MS) / 1000.0
            deadline = time.monotonic() + timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self.close()
                    raise BridgeError(f"{method} did not answer within {int(timeout * 1000)}ms")
                try:
                    line = self.lines.get(timeout=remaining)
                except queue.Empty:
                    continue
                if line is None:
                    self.close()
                    raise BridgeError("the superskill bridge exited before answering")
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(message, dict) or message.get("id") != request_id:
                    continue
                if message.get("error") is not None:
                    error = message.get("error")
                    text = error.get("message") if isinstance(error, dict) else str(error)
                    raise BridgeError(text or "the superskill bridge returned an error")
                return message.get("result")


class HttpBridge:
    def __init__(self, skill: Dict[str, Any]) -> None:
        self.bridge = skill.get("bridge") or {}
        self.url = str(self.bridge.get("url") or "")
        self.sequence = 0
        if not self.url:
            raise BridgeError("the superskill HTTP bridge has no URL")

    def close(self) -> None:
        return None

    def call(self, method: str, params: Any) -> Any:
        self.sequence += 1
        body = json.dumps({"jsonrpc": "2.0", "id": self.sequence, "method": method, "params": params}).encode()
        timeout = (self.bridge.get("timeoutMs") or DEFAULT_TIMEOUT_MS) / 1000.0
        request = urllib.request.Request(self.url, data=body, headers={"content-type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                message = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
            raise BridgeError(f"the superskill HTTP bridge failed: {error}") from error
        if isinstance(message, dict) and message.get("error") is not None:
            error = message.get("error")
            text = error.get("message") if isinstance(error, dict) else str(error)
            raise BridgeError(text or "the superskill HTTP bridge returned an error")
        return message.get("result") if isinstance(message, dict) else message


def build_tools(skill: Dict[str, Any]) -> List[Dict[str, Any]]:
    tools: List[Dict[str, Any]] = []
    for method in skill.get("methods") or []:
        if not isinstance(method, dict) or not isinstance(method.get("name"), str):
            continue
        name = method["name"]
        description = str(method.get("description") or f"Call {name} on the {skill.get('name', skill.get('id'))} bridge.")
        tools.append(
            {
                "name": name,
                "description": description,
                "inputSchema": method_schema(method),
            }
        )
    return tools


def respond(request_id: Any, result: Any) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def error(request_id: Any, code: int, message: str) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def serve(skill: Dict[str, Any]) -> int:
    bridge_type = str((skill.get("bridge") or {}).get("type") or "stdio")
    bridge: Any = HttpBridge(skill) if bridge_type == "http" else StdioBridge(skill)
    methods = {method.get("name"): method for method in skill.get("methods") or [] if isinstance(method, dict)}
    tools = build_tools(skill)
    try:
        for raw in sys.stdin:
            line = raw.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(message, dict):
                continue
            request_id = message.get("id")
            method = message.get("method")
            params = message.get("params") or {}
            if method == "initialize":
                result = {
                    "protocolVersion": params.get("protocolVersion") or PROTOCOL_VERSION,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": f"superskill-{skill.get('id')}", "version": str(skill.get("version") or "0.1.0")},
                }
                print(json.dumps(respond(request_id, result)), flush=True)
            elif method == "notifications/initialized":
                continue
            elif method == "ping":
                print(json.dumps(respond(request_id, {})), flush=True)
            elif method == "tools/list":
                print(json.dumps(respond(request_id, {"tools": tools})), flush=True)
            elif method == "tools/call":
                name = params.get("name")
                arguments = params.get("arguments") or {}
                target = methods.get(name)
                if target is None:
                    print(json.dumps(respond(request_id, {"content": [{"type": "text", "text": f"Unknown method: {name}"}], "isError": True})), flush=True)
                    continue
                if not isinstance(arguments, dict):
                    print(json.dumps(respond(request_id, {"content": [{"type": "text", "text": "Arguments must be an object."}], "isError": True})), flush=True)
                    continue
                try:
                    validate_args(target, arguments)
                    result = bridge.call(str(name), arguments)
                    text = json.dumps(result, ensure_ascii=False) if result is not None else "null"
                    print(json.dumps(respond(request_id, {"content": [{"type": "text", "text": text}], "isError": False})), flush=True)
                except BridgeError as failure:
                    print(json.dumps(respond(request_id, {"content": [{"type": "text", "text": str(failure)}], "isError": True})), flush=True)
            elif request_id is not None:
                print(json.dumps(error(request_id, -32601, f"Method not found: {method}")), flush=True)
    finally:
        bridge.close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Expose one Omnia superskill as an MCP stdio server.")
    parser.add_argument("--file", required=True, help="Path to the Omnia superskills JSON registry")
    parser.add_argument("--id", required=True, help="Superskill id to expose")
    args = parser.parse_args()
    try:
        skill = load_skill(args.file, args.id)
    except (OSError, json.JSONDecodeError, BridgeError) as failure:
        log(str(failure))
        return 2
    if skill.get("enabled") is False:
        log(f"superskill {args.id!r} is disabled")
        return 2
    return serve(skill)


if __name__ == "__main__":
    raise SystemExit(main())

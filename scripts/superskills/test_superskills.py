from __future__ import annotations

import json
import os
import select
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SHIM = HERE / "superskill_mcp.py"
IMPORTER = HERE / "import_omnia.py"

ECHO_BRIDGE = '''import json
import sys
for line in sys.stdin:
    request = json.loads(line)
    if request.get("method") == "fail":
        print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "error": {"message": "intentional failure"}}), flush=True)
    else:
        print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": request.get("params")}), flush=True)
'''


class SuperskillMcpTest(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        root = Path(self.dir.name)
        self.bridge = root / "echo.py"
        self.bridge.write_text(ECHO_BRIDGE, encoding="utf-8")
        self.registry = root / "superskills.json"
        self.registry.write_text(
            json.dumps(
                {
                    "superskills": [
                        {
                            "id": "echo-bridge",
                            "name": "Echo bridge",
                            "description": "Echoes parameters for tests.",
                            "instructions": "Use ping when you need to prove the bridge works.",
                            "enabled": True,
                            "bridge": {"type": "stdio", "command": f"{sys.executable} {self.bridge}", "timeoutMs": 5000},
                            "methods": [
                                {
                                    "name": "ping",
                                    "description": "Echo params.",
                                    "parameters": {"value": {"type": "string", "required": True}},
                                },
                                {"name": "fail", "description": "Always fails."},
                            ],
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        self.process = subprocess.Popen(
            [sys.executable, str(SHIM), "--file", str(self.registry), "--id", "echo-bridge"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )

    def tearDown(self) -> None:
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            if stream is not None:
                stream.close()
        if self.process.poll() is None:
            self.process.kill()
        self.process.wait(timeout=5)
        self.dir.cleanup()

    def send(self, message: dict) -> dict:
        assert self.process.stdin is not None
        assert self.process.stdout is not None
        self.process.stdin.write(json.dumps(message) + "\n")
        self.process.stdin.flush()
        ready, _, _ = select.select([self.process.stdout], [], [], 5)
        self.assertFalse(not ready, "superskill MCP server did not answer")
        line = self.process.stdout.readline()
        self.assertTrue(line, "superskill MCP server closed stdout")
        return json.loads(line)

    def test_initialize_list_and_call(self) -> None:
        init = self.send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}})
        self.assertEqual(init["result"]["serverInfo"]["name"], "superskill-echo-bridge")
        self.assertIn("tools", init["result"]["capabilities"])

        listed = self.send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        names = {tool["name"] for tool in listed["result"]["tools"]}
        self.assertEqual(names, {"ping", "fail"})
        ping = next(tool for tool in listed["result"]["tools"] if tool["name"] == "ping")
        self.assertEqual(ping["inputSchema"]["required"], ["value"])

        called = self.send(
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "ping", "arguments": {"value": "hello"}}}
        )
        self.assertEqual(called["result"]["isError"], False)
        self.assertEqual(json.loads(called["result"]["content"][0]["text"]), {"value": "hello"})

    def test_bridge_error_and_validation(self) -> None:
        failed = self.send(
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "fail", "arguments": {}}}
        )
        self.assertEqual(failed["result"]["isError"], True)
        self.assertIn("intentional failure", failed["result"]["content"][0]["text"])

        missing = self.send(
            {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "ping", "arguments": {}}}
        )
        self.assertEqual(missing["result"]["isError"], True)
        self.assertIn("requires", missing["result"]["content"][0]["text"])

    def test_unknown_method(self) -> None:
        response = self.send({"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": {"name": "nope", "arguments": {}}})
        self.assertEqual(response["result"]["isError"], True)


class ImporterTest(unittest.TestCase):
    def test_writes_skill_and_mentions_mcp(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "superskills.json"
            source.write_text(
                json.dumps(
                    {
                        "superskills": [
                            {
                                "id": "echo-bridge",
                                "name": "Echo bridge",
                                "description": "Echoes parameters for tests.",
                                "instructions": "Use ping to echo.",
                                "enabled": True,
                                "bridge": {"type": "stdio", "command": f"{sys.executable} -c pass"},
                                "methods": [{"name": "ping", "description": "Echo params."}],
                            },
                            {
                                "id": "disabled",
                                "name": "Disabled",
                                "description": "Off.",
                                "enabled": False,
                                "bridge": {"type": "http", "url": "http://127.0.0.1:1"},
                                "methods": [{"name": "x", "description": "x"}],
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )
            skills = root / "skills"
            result = subprocess.run(
                [sys.executable, str(IMPORTER), "--source", str(source), "--skills-dir", str(skills)],
                text=True,
                capture_output=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            skill = (skills / "echo-bridge" / "SKILL.md").read_text(encoding="utf-8")
            self.assertIn("Use ping to echo.", skill)
            self.assertIn("superskill-echo-bridge", skill)
            self.assertIn("interpreter mcp add", result.stdout)
            self.assertFalse((skills / "disabled").exists())


if __name__ == "__main__":
    unittest.main()

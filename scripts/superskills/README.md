# Omnia Superskills for Open Interpreter

Omnia superskills combine model instructions with a JSON-RPC bridge. Open
Interpreter already provides portable skills and MCP, so this adapter maps each
superskill onto both:

- `SKILL.md` in `~/.agents/skills/<id>/` teaches the model when and how to use
  the skill.
- An MCP server named `superskill-<id>` exposes every bridge method as a tool.

The bridge protocol stays the Omnia one: newline-delimited JSON-RPC 2.0 over a
stdio child process or HTTP. No Omnia runtime is required.

## Import

From the repository:

```bash
python3 scripts/superskills/import_omnia.py --source ~/.local/share/omnia/superskills.json --install
```

`--install` runs `interpreter mcp add` for each enabled skill. Without it, the
importer writes the skill files and prints the commands to run.

Useful flags:

- `--skills-dir` writes skills somewhere other than `~/.agents/skills`.
- `--all` imports disabled skills too.
- `--force` replaces existing MCP entries.
- `--dry-run` shows what would happen.

## What gets exposed

Each method becomes an MCP tool. Declared Omnia parameters become a JSON Schema
object, and required parameters are enforced before the call leaves the adapter.
A bridge error is returned as a tool error, so the model can recover instead of
losing the thread.

The adapter also bounds bridge output and kills a bridge that stops answering,
matching the Omnia timeout behavior.

## Approvals

Imported servers use the normal Open Interpreter MCP approval policy. Treat
superskill bridges as code execution: set `default_tools_approval_mode` to
`prompt` unless the bridge is trusted and read-only.

## Security

The registry can contain bridge environment secrets. The importer reads the
registry in place and never copies environment values into `SKILL.md` or prints
them.

## Tests

```bash
python3 -m unittest discover -s scripts/superskills -p 'test_*.py' -v
```

## Next

The adapter is deliberately a standalone bridge. The next step is a native
`interpreter superskill import` command in the Rust CLI, with the same file
format and MCP compatibility.

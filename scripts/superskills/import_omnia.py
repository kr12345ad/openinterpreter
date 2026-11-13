#!/usr/bin/env python3
"""Import Omnia superskills into Open Interpreter.

Each enabled superskill becomes:

1. A portable skill at ``~/.agents/skills/<id>/SKILL.md`` holding its
   instructions.
2. An MCP server named ``superskill-<id>`` that exposes the bridge methods.

The bridge file is read in place. Environment secrets in the superskill are
never copied into the skill or printed.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

DEFAULT_SOURCE = Path.home() / ".local" / "share" / "omnia" / "superskills.json"
DEFAULT_SKILLS = Path.home() / ".agents" / "skills"
SHIM = Path(__file__).with_name("superskill_mcp.py")


def load_registry(path: Path) -> List[Dict[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    skills = data.get("superskills") if isinstance(data, dict) else None
    if not isinstance(skills, list):
        raise SystemExit(f"{path} is not an Omnia superskill registry")
    return [skill for skill in skills if isinstance(skill, dict) and isinstance(skill.get("id"), str)]


def skill_markdown(skill: Dict[str, Any]) -> str:
    name = str(skill.get("name") or skill["id"])
    description = str(skill.get("description") or "").strip()
    instructions = str(skill.get("instructions") or "").strip()
    lines = ["---", f"name: {json.dumps(skill['id'])}", f"description: {json.dumps(description)}", "---", ""]
    if instructions:
        lines.extend([instructions, ""])
    lines.append("## Superskill methods")
    lines.append("")
    lines.append(f"Call these through the `superskill-{skill['id']}` MCP server.")
    lines.append("")
    for method in skill.get("methods") or []:
        if not isinstance(method, dict) or not isinstance(method.get("name"), str):
            continue
        summary = str(method.get("description") or "").strip()
        lines.append(f"- `{method['name']}`: {summary}".rstrip())
    lines.append("")
    return "\n".join(lines)


def write_skill(skill: Dict[str, Any], skills_dir: Path) -> Path:
    target = skills_dir / str(skill["id"])
    target.mkdir(parents=True, exist_ok=True)
    path = target / "SKILL.md"
    path.write_text(skill_markdown(skill), encoding="utf-8")
    return path


def mcp_command(skill: Dict[str, Any], registry: Path) -> List[str]:
    return [
        "interpreter",
        "mcp",
        "add",
        f"superskill-{skill['id']}",
        "--",
        sys.executable,
        str(SHIM),
        "--file",
        str(registry),
        "--id",
        str(skill["id"]),
    ]


def install_mcp(skill: Dict[str, Any], registry: Path, force: bool) -> bool:
    name = f"superskill-{skill['id']}"
    if shutil.which("interpreter") is None:
        print(f"  skip MCP install for {name}: interpreter is not on PATH", file=sys.stderr)
        return False
    if force:
        subprocess.run(["interpreter", "mcp", "remove", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    result = subprocess.run(mcp_command(skill, registry), text=True, capture_output=True)
    if result.returncode != 0:
        message = (result.stderr or result.stdout).strip()
        print(f"  MCP install failed for {name}: {message}", file=sys.stderr)
        return False
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Import Omnia superskills as skills and MCP servers.")
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE, help=f"Omnia registry (default: {DEFAULT_SOURCE})")
    parser.add_argument("--skills-dir", type=Path, default=DEFAULT_SKILLS, help=f"Skill directory (default: {DEFAULT_SKILLS})")
    parser.add_argument("--install", action="store_true", help="Run interpreter mcp add for each skill")
    parser.add_argument("--force", action="store_true", help="Replace existing MCP entries when installing")
    parser.add_argument("--all", action="store_true", help="Import disabled skills too")
    parser.add_argument("--dry-run", action="store_true", help="Show actions without writing")
    args = parser.parse_args()

    if not args.source.is_file():
        raise SystemExit(f"Omnia superskill registry not found: {args.source}")
    skills = load_registry(args.source)
    selected = [skill for skill in skills if args.all or skill.get("enabled") is not False]
    if not selected:
        print("No enabled superskills to import.")
        return 0

    for skill in selected:
        skill_id = str(skill["id"])
        print(f"{'would import' if args.dry_run else 'importing'} {skill_id}")
        if args.dry_run:
            print(f"  skill: {args.skills_dir / skill_id / 'SKILL.md'}")
            print("  mcp:", " ".join(mcp_command(skill, args.source)))
            continue
        path = write_skill(skill, args.skills_dir)
        print(f"  skill: {path}")
        if args.install:
            if install_mcp(skill, args.source, args.force):
                print(f"  mcp: superskill-{skill_id} registered")
    if not args.install:
        print("\nRegister each bridge with:")
        for skill in selected:
            print("  " + " ".join(mcp_command(skill, args.source)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

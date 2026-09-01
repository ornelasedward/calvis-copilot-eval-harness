"""Short Calvis eval CLI.

Examples:
  py calvis.py              list recipes
  py calvis.py t b          run Variant B recipe
  py calvis.py t b -n       dry-run (no API)
  py calvis.py t a3
  py calvis.py t smoke
  py calvis.py t quiet
  py calvis.py why a3       analyze prompt diff (LLM)
  py calvis.py why b -n     analyze without LLM

Or from repo root on Windows:  .\\cx t b
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")


def _help() -> None:
    print(
        """calvis - short eval commands

  cx | cx ls                 list codes
  cx t <code> [-n]           run test recipe
  cx go -n                   plan only (coverage table, save, exit)
  cx go [variant] [--yes]    plan, confirm, execute in order
  cx why <code> [-n]         analyze prompt vs baseline

  Test codes:   wl=welcome  cl=claims  es=escalation  qt=quietness  vo=voice  ag=photo-gamer
  Why codes:    va=A  v2=A2  v3=A3  vb=B  vc=C

Examples:
  .\\cx t cl -n
  .\\cx go -n --files scheduled_check_in.md
  .\\cx go v3 --intent "escalation" --yes
  .\\cx t es
  .\\cx why v3 -n
"""
    )


def main(argv: list[str] | None = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help", "help"):
        _help()
        # also show recipe table
        from cli import cmd_recipes
        import argparse

        cmd_recipes(argparse.Namespace())
        return

    cmd = argv[0]
    rest = argv[1:]

    if cmd in ("ls", "list", "r", "recipes"):
        from cli import cmd_recipes
        import argparse

        cmd_recipes(argparse.Namespace())
        return

    if cmd in ("t", "test"):
        if not rest:
            sys.exit("usage: calvis t <wl|cl|es|qt|ag> [-n]")
        recipe = rest[0]
        dry = "-n" in rest or "--dry-run" in rest
        from cli import cmd_test
        import argparse

        cmd_test(
            argparse.Namespace(
                recipe=recipe,
                adapter=None,
                model=None,
                variant=None,
                control=None,
                repeat=None,
                dry_run=dry,
            )
        )
        return

    if cmd == "go":
        from cli import main as cli_main

        cli_main(["go", *rest])
        return

    if cmd in ("why", "analyze", "a"):
        if not rest:
            sys.exit("usage: calvis why <va|v2|v3|vb> [-n]")
        from harness.recipes import resolve_analyze_target
        from cli import cmd_analyze
        import argparse

        target = resolve_analyze_target(rest[0])
        no_llm = "-n" in rest or "--no-llm" in rest
        cmd_analyze(
            argparse.Namespace(
                diff=["variants/baseline", target],
                control_variant=None,
                variant=None,
                control_run=None,
                variant_run=None,
                shift=None,
                focus_turns=None,
                adapter="openai",
                model="gpt-5.6-sol",
                no_llm=no_llm,
            )
        )
        return

    sys.exit(f"unknown command: {cmd}\nTry: py calvis.py help")


if __name__ == "__main__":
    main()

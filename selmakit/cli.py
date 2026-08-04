"""``selmakit`` console entry point.

Three subcommands, mirroring what start.sh does:

    selmakit init        — create .selmakit/ (config + workspace files)
    selmakit gateway     — run the gateway (channels, worker, schedules, cron)
    selmakit dashboard   — run the Streamlit dashboard

The repo's ``gateway.py`` / ``dashboard.py`` stay as editable reference entry
points; these subcommands are the equivalent for a pip-installed selmakit,
where those files do not exist.
"""
from __future__ import annotations

import argparse
import importlib.util
import subprocess
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path


def _version() -> str:
    try:
        return version("selmakit")
    except PackageNotFoundError:  # running from a source tree without an install
        return "unknown"


def _cmd_init(args: argparse.Namespace) -> int:
    from selmakit.init import init

    init(state_dir=args.state_dir)
    return 0


def _cmd_gateway(args: argparse.Namespace) -> int:
    from dotenv import load_dotenv

    load_dotenv()

    from selmakit import Gateway

    Gateway.from_config(state_dir=args.state_dir).run()
    return 0


def _cmd_dashboard(args: argparse.Namespace) -> int:
    # Check before shelling out: `python -m streamlit` would otherwise start
    # fine and fail with a bare "No module named streamlit".
    if importlib.util.find_spec("streamlit") is None:
        print(
            "The dashboard needs streamlit, which is not installed.\n"
            "Install it with: pip install 'selmakit[dashboard]'",
            file=sys.stderr,
        )
        return 1

    entry = Path(__file__).parent / "dashboard" / "_entry.py"
    cmd = [sys.executable, "-m", "streamlit", "run", str(entry)]
    if args.port is not None:
        cmd += ["--server.port", str(args.port)]
    return subprocess.call(cmd)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="selmakit",
        description="Minimal multi-channel agent framework built on pydantic-ai.",
    )
    parser.add_argument("--version", action="version", version=f"selmakit {_version()}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="create .selmakit/ with config and workspace files")
    p_init.add_argument("--state-dir", default=".selmakit", help="state directory (default: .selmakit)")
    p_init.set_defaults(func=_cmd_init)

    p_gw = sub.add_parser("gateway", help="run the gateway")
    p_gw.add_argument("--state-dir", default=".selmakit", help="state directory (default: .selmakit)")
    p_gw.set_defaults(func=_cmd_gateway)

    p_db = sub.add_parser("dashboard", help="run the Streamlit dashboard")
    p_db.add_argument("--port", type=int, default=None, help="port for the Streamlit server")
    p_db.set_defaults(func=_cmd_dashboard)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

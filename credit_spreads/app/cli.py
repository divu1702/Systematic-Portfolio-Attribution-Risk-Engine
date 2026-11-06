"""Command-line entry point: `python -m app <command>`."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import List, Optional

from .config import ConfigError, load_config
from .logging_setup import setup_logging

log = logging.getLogger("app")


def _cmd_doctor(args: argparse.Namespace) -> int:
    from .doctor import run_doctor

    return run_doctor(args.config, sys.stdout)


def _cmd_migrate(args: argparse.Namespace) -> int:
    from .data import migrations

    cfg = load_config(args.config)
    setup_logging(cfg.log_dir)
    applied = migrations.upgrade(cfg.db_path)
    if applied:
        for m in applied:
            print(f"applied {m.version:04d}_{m.name}")
    else:
        print("database already up to date")
    print(f"database: {cfg.db_path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m app", description="NSE stock credit spread system")
    p.add_argument("--config", type=Path, default=None, help="path to config.yaml")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor", help="print config and database status").set_defaults(func=_cmd_doctor)
    sub.add_parser("migrate", help="create or upgrade the SQLite database").set_defaults(func=_cmd_migrate)
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 2

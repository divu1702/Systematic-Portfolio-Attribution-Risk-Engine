"""`python -m platform_core doctor`: checks the laptop setup and prints what is missing. It never
prints a secret: for each key in .env it says only whether a value is set."""

from __future__ import annotations

import time
from pathlib import Path
from typing import TextIO

from .config import PlatformConfigError, load_platform_config

KEYS = ("KITE_API_KEY", "KITE_API_SECRET", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "CONTROL_PIN_HASH", "CONTROL_SECRET")


def _env(path: Path) -> dict:
    out = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
    return out


def run(config_path, out: TextIO, env_path: Path) -> int:
    errors, warnings = [], []

    def row(k, v):
        out.write(f"  {k:30s} {v}\n")

    out.write("Credit spreads platform: doctor (paper trading only)\n\nConfig\n------\n")
    try:
        cfg = load_platform_config(config_path)
    except PlatformConfigError as exc:
        out.write(f"{exc}\n")
        return 1
    row("platform.yaml", Path(config_path).resolve())
    row("go_live_date", cfg.go_live_date or "not set: nothing trades until the owner sets it")
    row("capital per account", f"{cfg.capital:,.0f}")
    row("accounts", ", ".join(cfg.accounts))
    row("earlier system", f"{cfg.oi_system.config}: " + ", ".join(f"{b} -> {a}" for a, b in cfg.oi_system.books.items()))
    row("exchange charge", f"{cfg.costs.exchange_txn_pct * 100:.5f}% each side")
    row("control centre", f"{cfg.control_centre.host}:{cfg.control_centre.port}, PIN required: {cfg.control_centre.pin_required}")
    try:
        from app.config import load_config

        ocfg = load_config(cfg.resolve(cfg.oi_system.config))
        row("earlier system mode", ocfg.mode)
        if ocfg.mode != "paper":
            errors.append("config.yaml mode must be paper: the platform is paper trading only")
        if abs(ocfg.costs.exchange_txn_pct - cfg.costs.exchange_txn_pct) > 1e-12:
            warnings.append("config.yaml and platform.yaml charge different exchange rates")
        missing = [b for b in cfg.oi_system.books.values() if b not in ocfg.strategy_names]
        if missing:
            errors.append(f"config.yaml lacks the books {missing} named in platform.yaml oi_system.books")
    except Exception as exc:
        errors.append(f"earlier system's config.yaml: {exc}")

    out.write("\nSecrets (.env, values never shown)\n----------------------------------\n")
    env = _env(env_path)
    row(".env", env_path if env_path.exists() else f"{env_path} (missing: copy .env.example)")
    for k in KEYS:
        row(k, "set" if env.get(k) else "NOT SET")
    if not (env.get("KITE_API_KEY") and env.get("KITE_API_SECRET")):
        errors.append("Kite API key and secret are needed (developers.kite.trade, paid Connect plan)")
    if not (env.get("TELEGRAM_BOT_TOKEN") and env.get("TELEGRAM_CHAT_ID")):
        warnings.append("Telegram is not configured: alerts and the 08:45 login link go to the log only")
    if cfg.control_centre.pin_required and not (env.get("CONTROL_PIN_HASH") and env.get("CONTROL_SECRET")):
        errors.append("no control-centre PIN: run `python -m platform_core set-pin`")

    out.write("\nData\n----\n")
    db = cfg.db_path
    row("platform database", f"{db} ({'exists' if db.exists() else 'created on first run'})")
    fixtures = cfg.resolve(cfg.paths.fixtures)
    have = sorted(p.stem for p in fixtures.glob("*.json")) if fixtures.exists() else []
    row("real-day fixtures", ", ".join(have) if have else "not recorded: run `python -m platform_core record-fixtures` "
        "after the first login")
    if not have:
        warnings.append("the two real-day detector tests skip until the fixtures are recorded")
    sectors = cfg.resolve(cfg.paths.sectors_file)
    if sectors.exists():
        age = (time.time() - sectors.stat().st_mtime) / 86400
        row("sectors.csv", f"{age:.0f} days old (fresh for {cfg.data.sectors_file_fresh_days})")
    else:
        row("sectors.csv", "absent (NSE's classification is used while fresh)")
    try:
        from app.config import load_config

        ocfg = load_config(cfg.resolve(cfg.oi_system.config))
        ev = ocfg.resolve(ocfg.paths.imports) / "events"
        files = list(ev.glob("*.csv")) if ev.exists() else []
        if files:
            age = (time.time() - max(f.stat().st_mtime for f in files)) / 86400
            row("newest events file", f"{age:.1f} days old (reminder after {cfg.data.events_file_max_age_days})")
        else:
            row("newest events file", "none yet (upload from the phone: control centre, Upload)")
    except Exception:
        pass

    out.write("\nRunning on its own\n------------------\n")
    try:
        from .service import Service

        svc = Service(Path(config_path))
        row("service", "installed (starts with the laptop)" if svc.installed() else
            "not installed: python -m platform_core service install")
    except Exception as exc:
        row("service", f"could not check ({exc})")
    out.write("\nPhone access\n------------\n")
    row("Tailscale", "on the laptop: tailscale serve --bg --https=443 "
                     f"http://{cfg.control_centre.host}:{cfg.control_centre.port} (see README)")
    out.write("\n")
    for w in warnings:
        out.write(f"WARNING: {w}\n")
    for e in errors:
        out.write(f"ERROR: {e}\n")
    out.write(f"\nResult: {'OK' if not errors else 'NOT READY'} ({len(errors)} errors, {len(warnings)} warnings)\n")
    return 0 if not errors else 1

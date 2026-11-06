"""`python -m app record SYMBOL ...`: save real Kite responses for a few stocks as offline
fixtures (instrument rows, one quote snapshot of their chains, daily candles). Run it on the
machine with a Kite session; FixtureBroker replays the files in tests."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import List

from . import market
from .broker.mock import encode
from .data import instruments as inst


def record_fixtures(ctx, symbols: List[str], out_dir: Path, days: int = 400) -> Path:
    out_dir = out_dir / ctx.clock.now().strftime("%Y%m%d_%H%M")
    (out_dir / "historical").mkdir(parents=True, exist_ok=True)
    want = set(symbols)
    nse = [r for r in ctx.broker.instruments("NSE") if r.get("tradingsymbol") in want]
    nfo = [r for r in ctx.broker.instruments("NFO") if r.get("name") in want]
    dump = lambda name, obj: (out_dir / name).write_text(json.dumps(obj, default=encode), encoding="utf-8")  # noqa: E731
    dump("instruments_NSE.json", nse)
    dump("instruments_NFO.json", nfo)
    today = market.ensure_instruments(ctx)
    keys, hist_tokens = [], []
    for sym in symbols:
        exps = inst.expiries(ctx.engine, today, sym)[:2]
        for e in exps:
            ch = inst.load_chain(ctx.engine, today, sym, e)
            keys += ch.keys() + ([ch.future.key] if ch.future else [])
            if ch.future:
                hist_tokens.append(ch.future.token)
        tok = inst.equity_token(ctx.engine, today, sym)
        if tok:
            keys.append(f"NSE:{sym}")
            hist_tokens.append(tok)
    dump("quotes.json", ctx.broker.quote_raw(sorted(set(keys))))
    now = ctx.clock.now()
    for tok in sorted(set(hist_tokens)):
        rows = ctx.broker.historical(tok, now - timedelta(days=days), now, "day")
        (out_dir / "historical" / f"{tok}_day.json").write_text(json.dumps(rows, default=encode), encoding="utf-8")
    dump("meta.json", {"symbols": symbols, "recorded_at": now.isoformat(), "broker": ctx.broker.name})
    return out_dir

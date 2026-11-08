"""Record the configuration behind every scan and trade (config_versions + config_hash columns)."""

from __future__ import annotations

from sqlalchemy.dialects.sqlite import insert

from .models import ConfigVersion


def record(ctx) -> str:
    """Hash of the rules of this context's strategy, stored once in config_versions."""
    h = ctx.cfg.strategy_hash()
    seen = ctx.cache.setdefault("config_hashes_recorded", set())
    if h not in seen:
        stmt = insert(ConfigVersion).values(hash=h, first_seen=ctx.clock.now(),
                                            config_json=ctx.cfg.model_dump_json(exclude={"base_dir"}))
        with ctx.engine.begin() as conn:
            conn.execute(stmt.on_conflict_do_nothing())
        seen.add(h)
    return h

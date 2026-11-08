"""Record the configuration behind every scan and trade (config_versions + config_hash columns)."""

from __future__ import annotations

from sqlalchemy.dialects.sqlite import insert

from .models import ConfigVersion


def record(ctx) -> str:
    h = ctx.cfg.strategy_hash()
    if ctx.cache.get("config_hash_recorded") != h:
        stmt = insert(ConfigVersion).values(hash=h, first_seen=ctx.clock.now(),
                                            config_json=ctx.cfg.model_dump_json(exclude={"base_dir"}))
        with ctx.engine.begin() as conn:
            conn.execute(stmt.on_conflict_do_nothing())
        ctx.cache["config_hash_recorded"] = h
    return h

from pathlib import Path

import pytest
import yaml

from app.config import DEFAULT_CONFIG_PATH, load_config


@pytest.fixture
def raw_config() -> dict:
    """The shipped config.yaml as a dict, for tests that tweak one key."""
    return yaml.safe_load(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))


@pytest.fixture
def write_config(tmp_path: Path):
    """Write a config dict to a temp dir and return its path. DB and logs land in tmp_path."""

    def _write(raw: dict) -> Path:
        raw = dict(raw)
        raw["paths"] = {**raw.get("paths", {}), "db": "data/test.db", "logs": "logs"}
        p = tmp_path / "config.yaml"
        p.write_text(yaml.safe_dump(raw), encoding="utf-8")
        return p

    return _write


@pytest.fixture
def cfg(raw_config, write_config):
    return load_config(write_config(raw_config))

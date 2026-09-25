from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"


@pytest.fixture
def config_dir() -> Path:
    """The repository's example configuration directory."""
    return CONFIG_DIR


@pytest.fixture
def example_config() -> dict[str, Any]:
    """The production example configuration as a mutable dictionary."""
    path = CONFIG_DIR / "config.example.yaml"
    data: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data


@pytest.fixture
def write_config(tmp_path: Path) -> Callable[[dict[str, Any]], Path]:
    """Write a configuration dictionary to a temporary YAML file and return its path."""

    def write(data: dict[str, Any]) -> Path:
        path = tmp_path / "config.yaml"
        path.write_text(yaml.safe_dump(data), encoding="utf-8")
        return path

    return write

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from pulse.config import domain_of, load_config
from pulse.errors import ConfigError

WriteConfig = Callable[[dict[str, Any]], Path]


@pytest.mark.parametrize("name", ["config.example.yaml", "config.dev.example.yaml"])
def test_example_configs_load(config_dir: Path, name: str) -> None:
    config = load_config(config_dir / name)
    assert config.sections[0].category == "customer_win"


@pytest.mark.parametrize("field", ["reviewers", "operator_alerts"])
def test_recipient_outside_allowed_domains_is_rejected(
    example_config: dict[str, Any], write_config: WriteConfig, field: str
) -> None:
    example_config[field] = ["someone@example.com"]
    with pytest.raises(ConfigError, match="outside allowed_recipient_domains"):
        load_config(write_config(example_config))


@pytest.mark.parametrize(
    "address",
    [
        "someone@techary.ai.example.com",  # suffix match must not pass
        "someone@evil-techary.ai",
        "someone@sub.techary.ai",  # subdomains must be listed explicitly
    ],
)
def test_recipient_domain_must_match_exactly(
    example_config: dict[str, Any], write_config: WriteConfig, address: str
) -> None:
    example_config["reviewers"] = [address]
    with pytest.raises(ConfigError, match="outside allowed_recipient_domains"):
        load_config(write_config(example_config))


def test_recipient_domain_match_ignores_case(
    example_config: dict[str, Any], write_config: WriteConfig
) -> None:
    example_config["reviewers"] = ["Reviewer@TECHARY.AI"]
    example_config["allowed_recipient_domains"] = ["Techary.AI"]
    load_config(write_config(example_config))


def test_missing_file_is_a_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="cannot read"):
        load_config(tmp_path / "missing.yaml")


def test_non_mapping_is_a_config_error(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("- just\n- a list\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="YAML mapping"):
        load_config(path)


@pytest.mark.parametrize(
    ("address", "domain"),
    [("a@Techary.AI", "techary.ai"), (" a@b.com ", "b.com"), ("a.b@c.d.e", "c.d.e")],
)
def test_domain_of(address: str, domain: str) -> None:
    assert domain_of(address) == domain


@pytest.mark.parametrize("address", ["", "a", "@b.com", "a@", "a@b@c.com"])
def test_domain_of_rejects_non_addresses(address: str) -> None:
    with pytest.raises(ValueError, match="not an email address"):
        domain_of(address)

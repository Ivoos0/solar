"""Alert e-mail wiring: the tracked files hold placeholders and !secret only."""
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CONFIG_YAML = (ROOT / "configuration.yaml").read_text(encoding="utf-8")


def _notify_block():
    m = re.search(r"^notify:\n((?:[ \t]+.*\n|[ \t]*\n)+)", CONFIG_YAML, re.M)
    assert m, "configuration.yaml has no notify: block"
    return m.group(1)


def test_notify_platform_matches_config_default():
    import config
    block = _notify_block()
    assert "platform: smtp" in block
    assert re.search(r"name:\s*%s\b" % config.SiteConfig.notify_service, block)


@pytest.mark.parametrize("key,secret", [
    ("server", "smtp_server"), ("port", "smtp_port"),
    ("encryption", "smtp_encryption"),
    ("sender", "smtp_sender"), ("username", "smtp_sender"),
    ("password", "smtp_password"), ("recipient", "smtp_recipient")])
def test_credentials_come_from_secret_tags(key, secret):
    block = _notify_block()
    assert re.search(r"^\s+%s:\s*!secret %s\s*$" % (key, secret), block, re.M)


def test_no_literal_credentials_or_addresses_in_tracked_yaml():
    for name in ("configuration.yaml", "secrets.example.yaml",
                 "battery_planner/user_config.example.yaml",
                 "examples/cost_simulation.yaml"):
        text = (ROOT / name).read_text(encoding="utf-8")
        for addr in re.findall(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", text):
            assert addr.lower().endswith("@example.com"), (name, addr)
    assert not re.search(r"(?im)^\s*password:\s*(?!!secret)\S", CONFIG_YAML)


def test_cost_simulation_example_is_optional_and_personal_data_free():
    text = (ROOT / "examples" / "cost_simulation.yaml").read_text(encoding="utf-8")
    assert "NOT loaded by configuration.yaml" in text
    assert "://" not in text
    assert "!secret" not in text
    assert not re.search(r"-?\d+(?:\.\d+)?/-?\d+(?:\.\d+)?/-?\d+", text)
    assert not re.search(r"(?im)^\s*(?:password|username):", text)


def test_configuration_yaml_holds_only_what_the_planner_needs():
    assert not re.search(r"^(?:template|utility_meter|input_number):",
                         CONFIG_YAML, re.M)
    for word in ("C-014", "OPEN QUESTION", "research.md", "NFR-", "FR-"):
        assert word not in CONFIG_YAML, word


def _load_yaml(name):
    yaml = pytest.importorskip("yaml")

    class Loader(yaml.SafeLoader):
        pass

    def scalar(loader, node):
        return loader.construct_scalar(node)

    for tag in ("!secret", "!include", "!include_dir_merge_named"):
        Loader.add_constructor(tag, scalar)
    return yaml.load((ROOT / name).read_text(encoding="utf-8"), Loader)


def test_configuration_yaml_parses_with_expected_top_level_keys():
    keys = set(_load_yaml("configuration.yaml"))
    assert {"pyscript", "rest", "notify"} <= keys
    assert {"automation", "script", "scene"} <= keys


def test_cost_simulation_example_parses():
    assert set(_load_yaml("examples/cost_simulation.yaml")) == {
        "input_number", "template", "utility_meter"}


SECRETS_EXAMPLE = (ROOT / "secrets.example.yaml").read_text(encoding="utf-8")
PROVIDER_KEYS = {"smtp_server", "smtp_port", "smtp_encryption"}


def _active_secrets():
    """Uncommented top-level `key: value` lines of secrets.example.yaml."""
    return re.findall(r"^(\w+):[ \t]*(\S.*?)[ \t]*(?:#.*)?$", SECRETS_EXAMPLE, re.M)


def _referenced_secrets():
    return set(re.findall(r"!secret\s+(\w+)", CONFIG_YAML))


def test_rest_resource_is_a_secret():
    m = re.search(r"^rest:\n((?:[ \t]+.*\n|[ \t]*\n)+)", CONFIG_YAML, re.M)
    assert m, "configuration.yaml has no rest: block"
    assert re.search(r"^\s+- resource:\s*!secret forecast_solar_url\s*$",
                     m.group(1), re.M)


def test_configuration_yaml_has_no_coordinates_or_hostnames():
    assert "://" not in CONFIG_YAML
    assert not re.search(r"(?i)\b(?:smtp|imap)\.\w", CONFIG_YAML)
    assert not re.search(r"(?i)gmail|office365|outlook|yahoo|icloud|me\.com",
                         CONFIG_YAML)
    # lat/lon/declination/azimuth/kwp style path segments
    assert not re.search(r"-?\d+(?:\.\d+)?/-?\d+(?:\.\d+)?/-?\d+", CONFIG_YAML)
    assert not re.search(r"\b51\.12\b|\b3\.85\b", CONFIG_YAML)


def test_secrets_example_covers_every_secret_configuration_yaml_uses():
    assert _referenced_secrets() >= PROVIDER_KEYS | {"forecast_solar_url"}
    missing = _referenced_secrets() - {k for k, _ in _active_secrets()}
    assert not missing, missing


def test_secrets_example_has_no_unused_keys():
    assert {k for k, _ in _active_secrets()} == _referenced_secrets()


def test_secrets_example_contains_only_placeholders():
    values = dict(_active_secrets())
    assert values["smtp_password"].endswith("placeholder")
    for k in ("smtp_sender", "smtp_recipient"):
        assert values[k].endswith("@example.com"), k
    url = values["forecast_solar_url"].strip("\"'")
    assert url.startswith("https://api.forecast.solar/estimate/")
    # generic example coordinates only (Brussels), never a real roof
    assert url.endswith("/50.85/4.35/35/0/5.0")


def test_secrets_example_has_exactly_one_uncommented_preset():
    active = [k for k, _ in _active_secrets() if k in PROVIDER_KEYS]
    assert sorted(active) == sorted(PROVIDER_KEYS)  # each exactly once
    values = dict(_active_secrets())
    assert values["smtp_encryption"] in {"starttls", "tls", "none"}
    assert values["smtp_port"].isdigit()
    # the other presets stay present, fully commented out
    commented = re.findall(r"^#\s*smtp_server:", SECRETS_EXAMPLE, re.M)
    assert len(commented) == 4  # Outlook, Yahoo, iCloud, CUSTOM
    for name in ("Gmail", "Outlook", "Yahoo", "iCloud", "CUSTOM"):
        assert "PRESET: %s" % name in SECRETS_EXAMPLE


def test_secrets_example_says_presets_are_unverified():
    assert re.search(r"NOT\s+been verified", SECRETS_EXAMPLE.replace("\n# ", " "))

def _ignored(path):
    r = subprocess.run(["git", "check-ignore", "-q", path], cwd=ROOT)
    return r.returncode == 0


def test_gitignore_hides_secrets_but_not_the_example():
    if not (ROOT / ".git").exists():
        pytest.skip("not a git checkout")
    assert _ignored("secrets.yaml")
    assert not _ignored("secrets.example.yaml")

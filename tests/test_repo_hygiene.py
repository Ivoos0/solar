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


def test_smtp_block_uses_gmail_starttls():
    block = _notify_block()
    assert "server: smtp.gmail.com" in block
    assert re.search(r"port:\s*587\b", block)
    assert "encryption: starttls" in block


@pytest.mark.parametrize("key,secret", [
    ("sender", "smtp_sender"), ("username", "smtp_sender"),
    ("password", "smtp_password"), ("recipient", "smtp_recipient")])
def test_credentials_come_from_secret_tags(key, secret):
    block = _notify_block()
    assert re.search(r"^\s+%s:\s*!secret %s\s*$" % (key, secret), block, re.M)


def test_no_literal_credentials_or_addresses_in_tracked_yaml():
    for name in ("configuration.yaml", "secrets.example.yaml",
                 "battery_planner/user_config.example.yaml"):
        text = (ROOT / name).read_text(encoding="utf-8")
        for addr in re.findall(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", text):
            assert addr.lower().endswith("@example.com"), (name, addr)
    assert not re.search(r"(?im)^\s*password:\s*(?!!secret)\S", CONFIG_YAML)


def test_secrets_example_has_the_keys_and_only_placeholders():
    text = (ROOT / "secrets.example.yaml").read_text(encoding="utf-8")
    keys = re.findall(r"^(\w+):\s*(\S+)", text, re.M)
    assert {k for k, _ in keys} == {"smtp_sender", "smtp_password",
                                    "smtp_recipient"}
    assert dict(keys)["smtp_password"].endswith("placeholder")
    for k in ("smtp_sender", "smtp_recipient"):
        assert dict(keys)[k].endswith("@example.com")


def _ignored(path):
    r = subprocess.run(["git", "check-ignore", "-q", path], cwd=ROOT)
    return r.returncode == 0


def test_gitignore_hides_secrets_but_not_the_example():
    if not (ROOT / ".git").exists():
        pytest.skip("not a git checkout")
    assert _ignored("secrets.yaml")
    assert not _ignored("secrets.example.yaml")

import sys
from pathlib import Path

import pytest

_MODULES = Path(__file__).resolve().parent.parent / "pyscript" / "modules"
if str(_MODULES) not in sys.path:
    sys.path.insert(0, str(_MODULES))

import config  # noqa: E402


@pytest.fixture
def site_config():
    """Valid SiteConfig with this site's real values."""
    return config.from_dict({
        "battery": {"capacity_kwh": 10.0},
        "alerts": {"address": "owner@example.com"},
    })

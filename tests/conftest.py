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
        # The shipped default is 80; the suite's hand-derived budgets are
        # against the full ceiling. The 80 % semantics are tested explicitly.
        "capacity_tariff": {"stay_under_percent": 100},
    })

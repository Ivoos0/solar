"""Run the driver conformance check on real drivers, and check the check itself.

    python3 -m pytest tests/test_driver_conformance.py --driver pyscript/modules/inverter_<name>.py

Every pyscript/modules/inverter_*.py is checked automatically; --driver adds
files from anywhere (see tests/driver_conformance.py for what is checked).
"""
import re
import textwrap
from pathlib import Path

import pytest

import driver_conformance as dc

ROOT = Path(__file__).resolve().parent.parent
SHIPPED = sorted((ROOT / "pyscript" / "modules").glob("inverter_*.py"))


def pytest_generate_tests(metafunc):
    if "driver_path" in metafunc.fixturenames:
        extra = [Path(p).resolve() for p in metafunc.config.getoption("driver")]
        paths = SHIPPED + [p for p in extra if p not in SHIPPED]
        metafunc.parametrize("driver_path", paths, ids=[p.name for p in paths])


def test_driver_conforms(driver_path):
    dc.check_driver(driver_path)


def test_logging_driver_is_shipped():
    assert ROOT / "pyscript" / "modules" / "inverter_logging.py" in SHIPPED


# ---- the check itself: a good driver passes, each broken one fails clearly ----

GOOD = '''
SOC_IS_STUB = False

def send(action, target_power_kw):
    return True

def read_charge_percent():
    return 42.5
'''


def write_driver(tmp_path, source=GOOD, name="inverter_fake.py"):
    path = tmp_path / name
    path.write_text(textwrap.dedent(source), encoding="utf-8")
    return path


def failure(path, **kwargs):
    with pytest.raises(dc.ConformanceError) as info:
        dc.check_driver(path, **kwargs)
    return str(info.value)


def test_good_fake_driver_passes(tmp_path):
    dc.check_driver(write_driver(tmp_path))


def test_driver_without_soc_flag_passes(tmp_path):
    dc.check_driver(write_driver(tmp_path, GOOD.replace("SOC_IS_STUB = False", "")))


def test_good_driver_may_import_third_party_names_that_exist(tmp_path):
    dc.check_driver(write_driver(tmp_path, "import json\n" + GOOD))


def test_missing_send_fails(tmp_path):
    src = GOOD.replace("def send(", "def sendd(")
    assert "send(action, target_power_kw) is missing" in failure(
        write_driver(tmp_path, src))


def test_missing_read_fails(tmp_path):
    src = GOOD.replace("def read_charge_percent", "def read_charge")
    assert "read_charge_percent() is missing" in failure(
        write_driver(tmp_path, src))


def test_send_returning_none_fails(tmp_path):
    src = GOOD.replace("return True", "pass")
    msg = failure(write_driver(tmp_path, src))
    assert "send('idle', 0.0) returned None" in msg
    assert "return True" in msg


def test_send_returning_false_fails(tmp_path):
    msg = failure(write_driver(tmp_path, GOOD.replace("return True", "return False")))
    assert "returned False" in msg


def test_read_above_100_fails(tmp_path):
    msg = failure(write_driver(tmp_path, GOOD.replace("42.5", "150")))
    assert "read_charge_percent() returned 150" in msg


def test_read_negative_fails(tmp_path):
    assert "returned -1" in failure(write_driver(tmp_path, GOOD.replace("42.5", "-1")))


def test_read_nan_fails(tmp_path):
    src = GOOD.replace("42.5", "float('nan')")
    assert "returned nan" in failure(write_driver(tmp_path, src))


def test_read_bool_fails(tmp_path):
    msg = failure(write_driver(tmp_path, GOOD.replace("42.5", "True")))
    assert "returned True (bool)" in msg


def test_read_string_fails(tmp_path):
    msg = failure(write_driver(tmp_path, GOOD.replace("42.5", "'80'")))
    assert "(str)" in msg


def test_read_raising_fails(tmp_path):
    src = GOOD.replace("return 42.5", "raise RuntimeError('bus down')")
    msg = failure(write_driver(tmp_path, src))
    assert "read_charge_percent() failed: RuntimeError: bus down" in msg


def test_send_that_blocks_too_long_fails(tmp_path):
    src = GOOD.replace("    return True", "    import time\n    time.sleep(3)\n    return True")
    msg = failure(write_driver(tmp_path, src), timeout=0.3)
    assert "no answer within 0.3 s" in msg


def test_send_raising_on_idle_fails(tmp_path):
    src = GOOD.replace(
        "    return True",
        "    if action == 'idle':\n        raise ValueError('idle not supported')\n    return True")
    msg = failure(write_driver(tmp_path, src))
    assert "send('idle', 0.0) failed on valid input: ValueError: idle not supported" in msg


def test_send_rejecting_one_action_fails(tmp_path):
    src = GOOD.replace(
        "    return True",
        "    if action == 'export':\n        raise KeyError(action)\n    return True")
    assert "send('export'" in failure(write_driver(tmp_path, src))


def test_send_not_idempotent_fails(tmp_path):
    src = '''
    calls = []
    def send(action, target_power_kw):
        calls.append(action)
        if len(calls) > 7:
            raise RuntimeError("second charge refused")
        return True
    def read_charge_percent():
        return 10
    '''
    assert "second charge refused" in failure(write_driver(tmp_path, src))


@pytest.mark.parametrize("name", [
    "logging.py", "inverter-foo.py", "inverter_Foo.py", "inverter_.py",
    "inverter_foo bar.py", "driver_inverter_foo.py"])
def test_bad_file_name_fails(tmp_path, name):
    msg = failure(write_driver(tmp_path, name=name))
    assert "must be inverter_<name>.py" in msg


def test_soc_flag_not_a_bool_fails(tmp_path):
    msg = failure(write_driver(tmp_path, GOOD.replace("SOC_IS_STUB = False", "SOC_IS_STUB = 'no'")))
    assert "SOC_IS_STUB is 'no'; it must be a bool" in msg


def test_hold_minutes_absent_none_or_positive_passes(tmp_path):
    for line in ("COMMAND_HOLD_MINUTES = None", "COMMAND_HOLD_MINUTES = 10",
                 "COMMAND_HOLD_MINUTES = 2.5"):
        dc.check_driver(write_driver(tmp_path, GOOD + line + "\n"))


@pytest.mark.parametrize("bad", ["0", "-1", "'10'", "True", "[10]",
                                 "float('nan')", "float('inf')"])
def test_hold_minutes_not_a_positive_number_fails(tmp_path, bad):
    msg = failure(write_driver(tmp_path, GOOD + "COMMAND_HOLD_MINUTES = %s\n" % bad))
    assert "COMMAND_HOLD_MINUTES is" in msg and "positive number" in msg


def test_network_at_import_fails(tmp_path):
    src = "import socket\nsocket.create_connection(('192.0.2.1', 502))\n" + GOOD
    msg = failure(write_driver(tmp_path, src))
    assert "network access at import time" in msg
    assert "socket.create_connection" in msg


def test_network_at_import_swallowed_by_driver_is_still_reported(tmp_path):
    src = ("import socket\n"
           "try:\n    socket.create_connection(('192.0.2.1', 502))\n"
           "except OSError:\n    pass\n" + GOOD)
    assert "network access at import time" in failure(write_driver(tmp_path, src))


def test_import_error_fails(tmp_path):
    msg = failure(write_driver(tmp_path, "import no_such_package_xyz\n" + GOOD))
    assert "importing the file failed" in msg
    assert "no_such_package_xyz" in msg


def test_missing_file_fails(tmp_path):
    assert "does not exist" in failure(tmp_path / "inverter_nope.py")


def test_all_problems_are_listed_together(tmp_path):
    src = GOOD.replace("42.5", "150").replace("return True", "return None")
    msg = failure(write_driver(tmp_path, src))
    assert re.search(r"returned None", msg) and "returned 150" in msg


def test_driver_option_runs_the_file(tmp_path):
    import subprocess
    import sys
    good = write_driver(tmp_path)
    bad = write_driver(tmp_path, GOOD.replace("42.5", "150"), name="inverter_bad.py")
    run = lambda p: subprocess.run(  # noqa: E731
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
         str(ROOT / "tests" / "test_driver_conformance.py"), "-k", "test_driver_conforms",
         "--driver", str(p)], capture_output=True, text=True, cwd=ROOT)
    ok = run(good)
    assert ok.returncode == 0, ok.stdout + ok.stderr
    assert "2 passed" in ok.stdout          # shipped logging driver + the given file
    failed = run(bad)
    assert failed.returncode != 0
    assert "read_charge_percent() returned 150" in failed.stdout

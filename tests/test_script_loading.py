"""Loading a standalone script must not leave sys.path mutated.

login_setup.py does an unconditional sys.path.insert(0, .../src) at module
scope. A loader that does not restore sys.path grows it once per load, and a
polluted sys.path is how a suite quietly becomes order-dependent.
"""

import sys

from tests.conftest import LOGIN_SETUP, load_script


def test_load_script_restores_sys_path():
    before = list(sys.path)

    with load_script(LOGIN_SETUP) as module:
        assert module._CANONICAL_LIMIT > 0  # actually used the module
        assert list(sys.path) != before, "expected the script to mutate sys.path"

    assert list(sys.path) == before


def test_repeated_loads_do_not_accumulate():
    before = list(sys.path)
    for _ in range(3):
        with load_script(LOGIN_SETUP):
            pass
    assert len(sys.path) == len(before)
    assert list(sys.path) == before

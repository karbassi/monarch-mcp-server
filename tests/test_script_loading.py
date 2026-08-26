"""Loading a standalone script must not leave sys.path mutated.

login_setup.py does an unconditional sys.path.insert(0, .../src) at module
scope. A loader that does not restore sys.path grows it once per load, and a
polluted sys.path is how a suite quietly becomes order-dependent.
"""

import sys

import pytest

from tests.conftest import LOGIN_SETUP, load_script


@pytest.fixture
def path_mutating_script(tmp_path):
    """A script that mutates sys.path, so the restoration test owns its own
    precondition instead of depending on login_setup.py continuing to do it."""
    script = tmp_path / "mutates_path.py"
    script.write_text(
        "import sys\n"
        "sys.path.insert(0, '/sentinel/added/by/script')\n"
        "loaded = True\n",
        encoding="utf-8",
    )
    return script


def test_load_script_restores_sys_path(path_mutating_script):
    before = list(sys.path)

    with load_script(path_mutating_script) as module:
        assert module.loaded is True  # the script really executed
        assert "/sentinel/added/by/script" in sys.path

    assert list(sys.path) == before


def test_load_script_executes_login_setup():
    """Separate concern: login_setup.py is loadable and usable. Deliberately
    says nothing about whether it touches sys.path -- if it is refactored to
    stop doing that, this should keep passing."""
    with load_script(LOGIN_SETUP) as module:
        assert module._CANONICAL_LIMIT > 0


def test_repeated_loads_do_not_accumulate(path_mutating_script):
    before = list(sys.path)
    for _ in range(3):
        with load_script(path_mutating_script):
            pass
    # Length, not just membership: an unconditional insert of an entry that is
    # already present still grows the list, which is how the original leak hid.
    assert len(sys.path) == len(before)
    assert list(sys.path) == before

def test_load_script_raises_a_clear_error_for_an_unloadable_path(tmp_path):
    """spec_from_file_location returns None for a path it cannot handle (an
    unrecognised extension, for one). Reaching through that gives an opaque
    AttributeError on `spec.loader`; the helper should say what went wrong."""
    from tests.conftest import load_script

    bad = tmp_path / "not_python.txt"
    bad.write_text("x = 1", encoding="utf-8")

    with pytest.raises(ImportError, match="not_python.txt"):
        with load_script(bad):
            pass


def test_load_script_restores_sys_path_even_when_loading_fails(tmp_path):
    bad = tmp_path / "not_python.txt"
    bad.write_text("x = 1", encoding="utf-8")

    before = list(sys.path)
    with pytest.raises(ImportError):
        with load_script(bad):
            pass
    assert list(sys.path) == before

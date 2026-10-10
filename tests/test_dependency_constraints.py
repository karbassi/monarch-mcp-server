"""The declared dependency constraints must exclude releases we cannot run.

These are not style checks. The server imports its MCP entry point at module
scope, so a constraint admitting an unrunnable version means a fresh install
resolves to a version where the server cannot start.

Two surfaces are checked, because they answer different questions:

* ``pyproject.toml`` declares version *ranges*. It floors mcp at 1.10 for dual
  1.x/2.x support (the entry point imports work under both, via a try/except),
  and floors the two vulnerable transitives at their first patched release.
* ``requirements-lock.txt`` is the hash-verified *exact* pin set the documented
  ``pip install`` path resolves to. ``requirements.txt`` itself is only a shim
  (``-r requirements-lock.txt``) with no specifiers of its own, so the pins are
  asserted against the lock. The lock resolves mcp to 2.x, which is the major
  the suite actually runs on.
"""

import tomllib
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.version import Version

_ROOT = Path(__file__).resolve().parent.parent

# Empirically established mcp releases, not derived from the constraint under
# test:
#   2.1.1  -- a 2.x release the server runs on (mcp.server.mcpserver present).
#   1.29.1 -- a 1.x release. Still supported: the entry point falls back to
#             mcp.server.fastmcp under 1.x, which is why the floor is 1.10, not
#             2.x. Deliberately not "the newest 1.x" -- that goes stale.
#   1.9.0  -- below the declared floor; must be rejected.
MCP_RUNNABLE = "2.1.1"
MCP_DUAL_SUPPORTED = "1.29.1"
MCP_BELOW_FLOOR = "1.9.0"


def _declared(name: str, requirements: list[str]) -> Requirement:
    for raw in requirements:
        req = Requirement(raw)
        if req.name == name:
            return req
    pytest.fail(f"{name} is not declared in {requirements}")


def _pyproject_requirements() -> list[str]:
    with (_ROOT / "pyproject.toml").open("rb") as f:
        return tomllib.load(f)["project"]["dependencies"]


def _lock_versions() -> dict[str, Version]:
    """Exact pinned versions from requirements-lock.txt, name -> Version."""
    versions: dict[str, Version] = {}
    for raw in (_ROOT / "requirements-lock.txt").read_text(encoding="utf-8").splitlines():
        line = raw.strip().rstrip("\\").strip()
        if not line or line.startswith("#") or "==" not in line:
            continue
        name, _, rest = line.partition("==")
        version = rest.split(";")[0].strip()
        if name.strip() and version:
            versions[name.strip().lower()] = Version(version)
    return versions


def test_pyproject_mcp_supports_both_majors_and_floors_out_old_1x():
    spec = _declared("mcp", _pyproject_requirements()).specifier
    assert spec.contains(MCP_RUNNABLE), f"constraint rejects mcp {MCP_RUNNABLE}"
    assert spec.contains(
        MCP_DUAL_SUPPORTED
    ), f"constraint rejects mcp {MCP_DUAL_SUPPORTED}, which dual support requires"
    assert not spec.contains(MCP_BELOW_FLOOR), (
        f"constraint admits mcp {MCP_BELOW_FLOOR}, which is below the declared "
        "floor"
    )


def test_lock_pins_a_runnable_mcp():
    mcp = _lock_versions().get("mcp")
    assert mcp is not None, "mcp is not pinned in requirements-lock.txt"
    # The suite runs on 2.x; the lock must resolve there, not to a 1.x release.
    assert mcp >= Version("2.1"), f"lock pins mcp {mcp}, below the runnable 2.x line"


# Transitive packages we pin a floor on purely to keep known-vulnerable versions
# out of a fresh resolve. Each entry is (package, version we must reject,
# advisory, first patched version) taken from the GitHub Advisory Database.
VULNERABLE_TRANSITIVES = [
    pytest.param("idna", "3.10", "GHSA-65pc-fj4g-8rjx", "3.15", id="idna"),
    pytest.param("aiohttp", "3.14.1", "GHSA-cq5v-8q36-5273", "3.14.3", id="aiohttp"),
]


@pytest.mark.parametrize("package, vulnerable, advisory, patched", VULNERABLE_TRANSITIVES)
def test_pyproject_floors_out_vulnerable_transitives(
    package, vulnerable, advisory, patched
):
    spec = _declared(package, _pyproject_requirements()).specifier
    assert not spec.contains(vulnerable), (
        f"constraint admits {package} {vulnerable}, affected by {advisory} "
        f"(first patched: {patched})"
    )
    assert spec.contains(patched), f"constraint rejects {package} {patched}"


@pytest.mark.parametrize("package, vulnerable, advisory, patched", VULNERABLE_TRANSITIVES)
def test_lock_pins_a_non_vulnerable_transitive(package, vulnerable, advisory, patched):
    pinned = _lock_versions().get(package)
    assert pinned is not None, f"{package} is not pinned in requirements-lock.txt"
    assert pinned != Version(vulnerable), (
        f"lock pins {package} {pinned}, affected by {advisory} "
        f"(first patched: {patched})"
    )
    assert pinned >= Version(patched), (
        f"lock pins {package} {pinned}, older than the first patched {patched}"
    )

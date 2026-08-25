"""The declared dependency constraints must exclude releases we cannot run.

These are not style checks. `mcp` 2.x removed `mcp.server.fastmcp`, which
`monarch_mcp_server.app` imports at module scope, so an unbounded `mcp` floor
means a fresh install resolves to a version where the server cannot start.
"""

import tomllib
from pathlib import Path

import pytest
from packaging.requirements import Requirement

_ROOT = Path(__file__).resolve().parent.parent

# Known-good and known-bad mcp releases, established empirically rather than
# derived from the constraint under test:
#   1.13.1 -- what the lockfile resolves to today; the suite passes on it.
#   2.1.0  -- `from mcp.server.fastmcp import FastMCP` raises ModuleNotFoundError.
MCP_RUNNABLE = "1.13.1"
MCP_BROKEN = "2.1.0"


def _declared(name: str, requirements: list[str]) -> Requirement:
    for raw in requirements:
        req = Requirement(raw)
        if req.name == name:
            return req
    pytest.fail(f"{name} is not declared in {requirements}")


def _pyproject_requirements() -> list[str]:
    with (_ROOT / "pyproject.toml").open("rb") as f:
        return tomllib.load(f)["project"]["dependencies"]


def _requirements_txt() -> list[str]:
    lines = (_ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
    return [ln.strip() for ln in lines if ln.strip() and not ln.startswith("#")]


@pytest.mark.parametrize(
    "source",
    [
        pytest.param(_pyproject_requirements, id="pyproject.toml"),
        pytest.param(_requirements_txt, id="requirements.txt"),
    ],
)
def test_mcp_constraint_excludes_releases_without_fastmcp(source):
    spec = _declared("mcp", source()).specifier
    assert spec.contains(
        MCP_RUNNABLE
    ), f"constraint rejects mcp {MCP_RUNNABLE}, which the server runs on"
    assert not spec.contains(MCP_BROKEN), (
        f"constraint admits mcp {MCP_BROKEN}, which has no mcp.server.fastmcp; "
        "a fresh install would resolve to a server that cannot start"
    )


# Transitive packages we pin a floor on purely to keep known-vulnerable
# versions out of a fresh resolve. Each entry is (package, version we must
# reject, advisory, first patched version) taken from the GitHub Advisory
# Database, not from the constraint under test.
VULNERABLE_TRANSITIVES = [
    pytest.param(
        "idna",
        "3.10",
        "GHSA-65pc-fj4g-8rjx",
        "3.15",
        id="idna-GHSA-65pc-fj4g-8rjx",
    ),
    pytest.param(
        "aiohttp",
        "3.14.1",
        "GHSA-cq5v-8q36-5273",
        "3.14.3",
        id="aiohttp-GHSA-cq5v-8q36-5273",
    ),
]


@pytest.mark.parametrize(
    "package, vulnerable, advisory, patched", VULNERABLE_TRANSITIVES
)
@pytest.mark.parametrize(
    "source",
    [
        pytest.param(_pyproject_requirements, id="pyproject.toml"),
        pytest.param(_requirements_txt, id="requirements.txt"),
    ],
)
def test_vulnerable_transitive_versions_are_excluded(
    source, package, vulnerable, advisory, patched
):
    spec = _declared(package, source()).specifier
    assert not spec.contains(vulnerable), (
        f"constraint admits {package} {vulnerable}, which is affected by "
        f"{advisory} (first patched: {patched})"
    )
    assert spec.contains(
        patched
    ), f"constraint rejects {package} {patched}, the first patched release"

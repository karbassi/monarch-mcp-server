"""Tool exposure policy.

This server holds credentials to a real personal-finance account and hands them
to an LLM. 24 of its 49 tools mutate that account -- create and delete
transactions, set budget amounts, rewrite auto-categorisation rules -- with no
undo. Gating happens at registration time, so a withheld tool is never
advertised to the model and cannot be called even by name.

Allowlist, not denylist. Classifying by name or by which client method a tool
calls both fail, in different places:

    split_transaction          reads as a query, calls update_transaction_splits
    categorize_transaction     reads as a query, calls update_transaction
    mark_transaction_reviewed  reads as a query, calls update_transaction
    review_recurring_stream    reads as a query, no write-shaped call at all --
                               a mutation only if you read the GraphQL operation
    refresh_accounts           reads as a query, triggers a sync at the user's
                               institutions
    update_category            named as a write, reaches Monarch by raw GraphQL

A denylist inherits that unreliability forever: the next tool added upstream is
exposed by default, silently. An allowlist fails the other way -- an unlisted
tool stays hidden, which surfaces as a missing feature rather than as an LLM
writing to the account.
"""

import pytest

from monarch_mcp_server import tool_policy


class TestLoadEnabled:
    def test_only_literal_true_enables_a_tool(self, tmp_path, monkeypatch):
        cfg = tmp_path / "tools.toml"
        cfg.write_text(
            "[tools]\n"
            "get_accounts = true\n"
            'delete_transaction = "true"\n'  # a string must not grant permission
            "create_transaction = 1\n"  # nor a truthy int
            "update_transaction = false\n",
            encoding="utf-8",
        )
        monkeypatch.setenv(tool_policy.CONFIG_ENV_VAR, str(cfg))

        assert tool_policy.load_enabled() == {"get_accounts"}

    def test_missing_config_falls_back_to_reads_only(self, tmp_path, monkeypatch):
        monkeypatch.setenv(tool_policy.CONFIG_ENV_VAR, str(tmp_path / "absent.toml"))
        enabled = tool_policy.load_enabled()
        assert enabled == set(tool_policy.FALLBACK_READ_TOOLS)
        assert not (enabled & tool_policy.WRITE_TOOLS)

    def test_unparseable_config_falls_back_to_reads_only(self, tmp_path, monkeypatch):
        cfg = tmp_path / "broken.toml"
        cfg.write_text("[tools\nthis is not toml", encoding="utf-8")
        monkeypatch.setenv(tool_policy.CONFIG_ENV_VAR, str(cfg))

        enabled = tool_policy.load_enabled()
        assert enabled == set(tool_policy.FALLBACK_READ_TOOLS)
        assert not (enabled & tool_policy.WRITE_TOOLS)

    def test_config_without_a_tools_table_falls_back(self, tmp_path, monkeypatch):
        cfg = tmp_path / "empty.toml"
        cfg.write_text("[something_else]\nx = 1\n", encoding="utf-8")
        monkeypatch.setenv(tool_policy.CONFIG_ENV_VAR, str(cfg))

        assert tool_policy.load_enabled() == set(tool_policy.FALLBACK_READ_TOOLS)

    def test_a_config_enabling_a_write_is_honoured(self, tmp_path, monkeypatch):
        """The point is a deliberate opt-in, not a lock."""
        cfg = tmp_path / "tools.toml"
        cfg.write_text("[tools]\ncategorize_transaction = true\n", encoding="utf-8")
        monkeypatch.setenv(tool_policy.CONFIG_ENV_VAR, str(cfg))

        assert tool_policy.load_enabled() == {"categorize_transaction"}


class TestShippedConfigMatchesReality:
    """These are the tests that keep the classification honest over time."""

    def test_every_registered_tool_is_classified(self):
        """A tool missing from the policy is the drift this design exists to
        catch. It fails closed, but silently -- so assert it loudly."""
        registered = _registered_tool_names()
        classified = set(tool_policy.READ_TOOLS) | set(tool_policy.WRITE_TOOLS)

        assert (
            registered - classified == set()
        ), f"unclassified tools: {sorted(registered - classified)}"
        assert classified - registered == set(), (
            f"classified but not registered (renamed upstream?): "
            f"{sorted(classified - registered)}"
        )

    def test_read_and_write_sets_are_disjoint(self):
        assert not (set(tool_policy.READ_TOOLS) & set(tool_policy.WRITE_TOOLS))

    def test_fallback_is_exactly_the_read_set(self):
        assert set(tool_policy.FALLBACK_READ_TOOLS) == set(tool_policy.READ_TOOLS)

    def test_the_shipped_config_enables_only_reads(self, monkeypatch):
        monkeypatch.delenv(tool_policy.CONFIG_ENV_VAR, raising=False)
        enabled = tool_policy.load_enabled()
        assert enabled == set(tool_policy.READ_TOOLS), (
            f"shipped config drifted: unexpected={sorted(enabled - set(tool_policy.READ_TOOLS))}, "
            f"missing={sorted(set(tool_policy.READ_TOOLS) - enabled)}"
        )

    def test_shipped_config_lists_every_tool(self, monkeypatch):
        """Every tool should appear in the file, so enabling one is a flip rather
        than remembering its exact name."""
        monkeypatch.delenv(tool_policy.CONFIG_ENV_VAR, raising=False)
        listed = tool_policy.load_declared()
        assert listed == _registered_tool_names(), (
            f"config missing: {sorted(_registered_tool_names() - listed)}; "
            f"config extra: {sorted(listed - _registered_tool_names())}"
        )


class TestNoReadToolPerformsAMutation:
    """A static audit of the read classification.

    If someone adds a mutating call to a tool marked read-only, the gate keeps
    exposing it and nothing else would notice. This reads the source of every
    read tool and fails on a write-shaped call.
    """

    WRITE_MARKERS = (
        "MUTATION",
        "client.update_",
        "client.create_",
        "client.delete_",
        "client.set_",
        "client.upload_",
        "client.request_accounts_refresh",
    )

    def test_read_tools_contain_no_write_markers(self):
        import inspect

        offenders = {}
        for name in sorted(tool_policy.READ_TOOLS):
            fn = _resolve_tool(name)
            if fn is None:
                continue  # covered by test_every_registered_tool_is_classified
            src = inspect.getsource(fn)
            hits = [m for m in self.WRITE_MARKERS if m in src]
            if hits:
                offenders[name] = hits
        assert (
            not offenders
        ), f"read-classified tools with write-shaped calls: {offenders}"


def _tool_modules():
    from monarch_mcp_server.tools import (
        accounts,
        auth,
        budgets,
        categories,
        financial,
        merchants,
        rules,
        splits,
        summaries,
        tags,
        transactions,
    )

    return (
        accounts,
        auth,
        budgets,
        categories,
        financial,
        merchants,
        rules,
        splits,
        summaries,
        tags,
        transactions,
    )


def _resolve_tool(name):
    for mod in _tool_modules():
        fn = getattr(mod, name, None)
        if fn is not None:
            return fn
    return None


def _advertised_tool_names(mcp):
    """Tool names the server actually advertises, via the public async API.

    FastMCP exposes list_tools() publicly; mcp._tool_manager is private and can
    change under us on an upstream bump. Kept in one place either way.
    """
    import asyncio

    return {t.name for t in asyncio.run(mcp.list_tools())}


def _registered_tool_names():
    """The full tool inventory, not the exposed subset.

    The MCP registry only holds enabled tools, so reading it here would make the
    coverage tests pass trivially -- with gating on, an unclassified tool is
    absent from the registry precisely because it was withheld. enforce() sees
    every registration attempt, so withheld + registered is the real inventory.
    """
    from monarch_mcp_server.app import tool_inventory

    return set(tool_inventory())


class TestGatingActuallyApplies:
    """End-to-end: the MCP registry must hold only the enabled tools."""

    def test_registry_exposes_reads_and_withholds_writes(self):
        from monarch_mcp_server.app import (
            mcp,
            registered_tools,
            tool_inventory,
            withheld_tools,
        )

        REGISTERED_TOOLS, WITHHELD_TOOLS = registered_tools(), withheld_tools()
        TOOL_INVENTORY = tool_inventory()
        advertised = _advertised_tool_names(mcp)
        assert advertised == set(REGISTERED_TOOLS)
        # The thing that matters: nothing that mutates the account is advertised.
        assert not (advertised & tool_policy.WRITE_TOOLS), (
            f"write tools advertised to the model: "
            f"{sorted(advertised & tool_policy.WRITE_TOOLS)}"
        )
        assert set(WITHHELD_TOOLS) == tool_policy.WRITE_TOOLS
        assert set(TOOL_INVENTORY) == set(REGISTERED_TOOLS) | set(WITHHELD_TOOLS)

    def test_withheld_tools_remain_importable(self):
        """Withheld means "not an MCP tool", not "deleted" -- they stay callable
        in-process so tests and scripts still work."""
        assert callable(_resolve_tool("delete_transaction"))


class TestConfigDiscovery:
    """Finding the config must work for an installed copy, not just a checkout.

    DEFAULT_CONFIG pointed two directories above the module -- the repo root,
    which exists under the documented `--with-editable` install but not in a
    wheel, and pyproject ships no package data. An installed copy therefore
    found no config, fell back to reads-only, and could never enable a write.
    """

    def test_env_var_expands_a_user_path(self, tmp_path, monkeypatch):
        cfg = tmp_path / "tools.toml"
        cfg.write_text("[tools]\nget_accounts = true\n", encoding="utf-8")
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv(tool_policy.CONFIG_ENV_VAR, "~/tools.toml")

        assert tool_policy.config_path() == cfg
        assert tool_policy.load_enabled() == {"get_accounts"}

    def test_env_var_expands_a_variable(self, tmp_path, monkeypatch):
        cfg = tmp_path / "tools.toml"
        cfg.write_text("[tools]\nget_budgets = true\n", encoding="utf-8")
        monkeypatch.setenv("MY_CFG_DIR", str(tmp_path))
        monkeypatch.setenv(tool_policy.CONFIG_ENV_VAR, "$MY_CFG_DIR/tools.toml")

        assert tool_policy.config_path() == cfg

    def test_falls_back_to_a_config_beside_the_package(self, tmp_path, monkeypatch):
        """The location a wheel can actually ship."""
        monkeypatch.delenv(tool_policy.CONFIG_ENV_VAR, raising=False)
        beside = tmp_path / "beside" / "tools.toml"
        beside.parent.mkdir()
        beside.write_text("[tools]\nget_accounts = true\n", encoding="utf-8")
        monkeypatch.setattr(
            tool_policy, "_CANDIDATES", (tmp_path / "absent.toml", beside)
        )

        assert tool_policy.config_path() == beside

    def test_reports_every_path_tried_when_none_exist(
        self, tmp_path, monkeypatch, caplog
    ):
        monkeypatch.delenv(tool_policy.CONFIG_ENV_VAR, raising=False)
        a, b = tmp_path / "a.toml", tmp_path / "b.toml"
        monkeypatch.setattr(tool_policy, "_CANDIDATES", (a, b))

        with caplog.at_level("WARNING", logger=tool_policy.logger.name):
            enabled = tool_policy.load_enabled()

        assert enabled == set(tool_policy.FALLBACK_READ_TOOLS)
        logged = " ".join(r.getMessage() for r in caplog.records)
        assert str(a) in logged and str(b) in logged
        assert tool_policy.CONFIG_ENV_VAR in logged


def test_exactly_one_tools_toml_is_tracked():
    """One config in the tree, or the two copies drift.

    It lives inside the package so a wheel ships it. A repo-root tools.toml and
    MONARCH_TOOLS_CONFIG both still override it, in that order -- but neither is
    committed.
    """
    import shutil
    import subprocess
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    if shutil.which("git") is None or not (root / ".git").exists():
        pytest.skip("needs a git checkout; this guards drift, not runtime behaviour")

    result = subprocess.run(
        ["git", "ls-files", "*tools.toml"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.skip(f"git ls-files failed: {result.stderr.strip()[:80]}")
    assert result.stdout.split() == ["src/monarch_mcp_server/tools.toml"], result.stdout


class TestNotFoundWarningIsAccurate:
    """The warning must name the path actually attempted.

    It claimed to have "tried" every candidate while the code opens only
    config_path() -- so with MONARCH_TOOLS_CONFIG set, it listed paths it never
    touched and omitted the one that was actually missing.
    """

    def test_names_the_override_path_when_set(self, tmp_path, monkeypatch, caplog):
        absent = tmp_path / "nope.toml"
        monkeypatch.setenv(tool_policy.CONFIG_ENV_VAR, str(absent))

        with caplog.at_level("WARNING", logger=tool_policy.logger.name):
            tool_policy.load_enabled()

        logged = " ".join(r.getMessage() for r in caplog.records)
        assert str(absent) in logged, "the missing path was not named"
        # The unsearched shipped candidates must not be presented as "tried".
        for candidate in tool_policy._CANDIDATES:
            assert (
                str(candidate) not in logged
            ), f"claimed to try {candidate}, which it never opened"

    def test_names_the_candidates_when_no_override(self, tmp_path, monkeypatch, caplog):
        monkeypatch.delenv(tool_policy.CONFIG_ENV_VAR, raising=False)
        a, b = tmp_path / "a.toml", tmp_path / "b.toml"
        monkeypatch.setattr(tool_policy, "_CANDIDATES", (a, b))

        with caplog.at_level("WARNING", logger=tool_policy.logger.name):
            tool_policy.load_enabled()

        logged = " ".join(r.getMessage() for r in caplog.records)
        assert str(a) in logged and str(b) in logged


class TestCandidatePrecedence:
    """Pin the precedence, so a comment cannot drift from the behaviour.

    _CANDIDATES is written in *search* order (first existing wins), which is the
    reverse of priority order. A reviewer read the two as contradictory, so the
    ordering is asserted here rather than only described in prose.
    """

    def test_repo_root_overrides_the_shipped_copy(self, monkeypatch, tmp_path):
        monkeypatch.delenv(tool_policy.CONFIG_ENV_VAR, raising=False)
        shipped = tmp_path / "pkg" / "tools.toml"
        shipped.parent.mkdir()
        shipped.write_text("[tools]\nget_accounts = true\n", encoding="utf-8")
        root = tmp_path / "tools.toml"
        root.write_text("[tools]\nget_budgets = true\n", encoding="utf-8")
        monkeypatch.setattr(tool_policy, "_CANDIDATES", (root, shipped))

        assert tool_policy.config_path() == root
        assert tool_policy.load_enabled() == {"get_budgets"}

    def test_shipped_copy_is_used_when_no_repo_root_file(self, monkeypatch, tmp_path):
        monkeypatch.delenv(tool_policy.CONFIG_ENV_VAR, raising=False)
        shipped = tmp_path / "pkg" / "tools.toml"
        shipped.parent.mkdir()
        shipped.write_text("[tools]\nget_accounts = true\n", encoding="utf-8")
        monkeypatch.setattr(
            tool_policy, "_CANDIDATES", (tmp_path / "tools.toml", shipped)
        )

        assert tool_policy.config_path() == shipped
        assert tool_policy.load_enabled() == {"get_accounts"}

    def test_env_var_overrides_both(self, monkeypatch, tmp_path):
        shipped = tmp_path / "pkg" / "tools.toml"
        shipped.parent.mkdir()
        shipped.write_text("[tools]\nget_accounts = true\n", encoding="utf-8")
        root = tmp_path / "tools.toml"
        root.write_text("[tools]\nget_budgets = true\n", encoding="utf-8")
        override = tmp_path / "mine.toml"
        override.write_text("[tools]\nget_cashflow = true\n", encoding="utf-8")
        monkeypatch.setattr(tool_policy, "_CANDIDATES", (root, shipped))
        monkeypatch.setenv(tool_policy.CONFIG_ENV_VAR, str(override))

        assert tool_policy.config_path() == override
        assert tool_policy.load_enabled() == {"get_cashflow"}

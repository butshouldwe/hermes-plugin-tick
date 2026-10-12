"""Behaviour tests for tick. Each test fails if the feature it names is removed."""
from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
from datetime import datetime, timezone

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load():
    name = "tick_under_test"
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)]
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


tick = _load()

# 2026-10-09 23:00:00 UTC == 19:00 EDT (UTC-04:00, daylight time)
T0 = datetime(2026, 10, 9, 23, 0, tzinfo=timezone.utc).timestamp()
MIN = 60.0
HOUR = 3600.0


class FakeState:
    def __init__(self):
        self.data = {}

    def get(self, key, default=None):
        return json.loads(json.dumps(self.data.get(key, default)))

    def set(self, key, value):
        self.data[key] = json.loads(json.dumps(value))


class FakeCtx:
    def __init__(self, settings=None):
        self.settings = settings or {}
        self.state = FakeState()
        self.hooks = {}
        self.tools = {}

    def get_config(self, key, default=None):
        return self.settings.get(key, default)

    def register_hook(self, name, cb):
        self.hooks[name] = cb

    def register_tool(self, name, toolset, schema, handler, **kw):
        self.tools[name] = (toolset, schema, handler, kw)

    def register_command(self, name, handler, **kw):
        self.commands = getattr(self, "commands", {})
        self.commands[name] = handler


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def env(monkeypatch):
    def make(settings=None, now=T0):
        base = {"timezone": "America/New_York"}
        base.update(settings or {})
        ctx = FakeCtx(base)
        clock = Clock(now)
        monkeypatch.setattr(tick, "_now", clock)
        tick.register(ctx)
        return ctx, clock

    return make


def turn(ctx, sid="s1", model="claude-opus-5-5", parent="", history=None):
    out = ctx.hooks["pre_llm_call"](
        session_id=sid,
        user_message="hi",
        conversation_history=history or [],
        is_first_turn=not history,
        model=model,
        platform="desktop",
        parent_session_id=parent,
    )
    return (out or {}).get("context", "")


def tool(ctx, name, args, sid="s1", status="ok", result="{}"):
    ctx.hooks["post_tool_call"](
        tool_name=name, args=args, result=result, session_id=sid, status=status
    )


def call_tool(ctx, sid="s1", **args):
    handler = ctx.tools["tick"][2]
    return json.loads(handler(args, session_id=sid))


# --- registration and manifest -------------------------------------------------


def test_registers_declared_hooks_and_tool(env):
    ctx, _ = env()
    manifest = (ROOT / "plugin.yaml").read_text()
    for hook in ctx.hooks:
        assert f"- {hook}" in manifest
    assert "- tick" in manifest
    assert set(ctx.hooks) == {"pre_llm_call", "post_tool_call", "pre_auxiliary_call"}


def test_every_setting_is_declared_in_config_schema_and_readme(env):
    manifest = (ROOT / "plugin.yaml").read_text()
    readme = (ROOT / "README.md").read_text()
    for key in tick.DEFAULTS:
        assert f"  {key}:" in manifest, key
        assert f"`{key}`" in readme, key


# --- clock ---------------------------------------------------------------------


def test_clock_line_uses_configured_timezone_with_utc_offset(env):
    ctx, _ = env()
    text = turn(ctx)
    assert "Fri 2026-10-09 19:00 EDT (UTC-04:00)" in text
    assert "2026-10-09T23:00Z" in text


def test_clock_handles_standard_time_offset(env):
    winter = datetime(2026, 1, 15, 17, 30, tzinfo=timezone.utc).timestamp()
    ctx, _ = env(now=winter)
    assert "12:30 EST (UTC-05:00)" in turn(ctx)


def test_invalid_timezone_falls_back_to_utc_instead_of_failing(env):
    ctx, _ = env({"timezone": "Mars/Olympus"})
    assert "19:00" not in turn(ctx)
    assert "23:00 UTC" in turn(ctx, sid="s2")


def test_first_turn_says_new_session(env):
    ctx, _ = env()
    assert "new session" in turn(ctx)


def test_gap_reported_since_previous_turn(env):
    ctx, clock = env()
    turn(ctx)
    clock.t += 3 * HOUR + 12 * MIN
    text = turn(ctx)
    assert "last message 3h12m ago" in text
    assert "(19:00)" in text


def test_gap_survives_restart_through_plugin_state(env, monkeypatch):
    ctx, clock = env()
    turn(ctx)
    saved = ctx.state.data
    ctx2 = FakeCtx({"timezone": "America/New_York"})
    ctx2.state.data = saved
    tick.register(ctx2)
    clock.t += 2 * HOUR
    assert "last message 2h00m ago" in turn(ctx2)


def test_gap_falls_back_to_history_timestamps_when_state_is_empty(env):
    ctx, clock = env()
    # Host shape: the history passed to pre_llm_call ends with the current message.
    history = [
        {"role": "user", "content": "x", "timestamp": T0 - 26 * HOUR - 60},
        {"role": "assistant", "content": "y", "timestamp": T0 - 26 * HOUR},
        {"role": "user", "content": "hi", "timestamp": T0},
    ]
    text = turn(ctx, history=history)
    assert "last message 1d02h ago" in text


def test_current_message_timestamp_is_not_the_previous_message(env):
    """Regression: a first live turn reported 'last message 0s ago'."""
    ctx, _ = env()
    text = turn(ctx, history=[{"role": "user", "content": "hi", "timestamp": T0}])
    assert "last message" not in text
    assert "[tick] now" in text


def test_resumed_session_gap_triggers_reconcile_on_first_tick_turn(env):
    ctx, _ = env()
    history = [
        {"role": "assistant", "content": "y", "timestamp": T0 - 5 * HOUR},
        {"role": "user", "content": "back", "timestamp": T0},
    ]
    text = turn(ctx, history=history)
    assert "last message 5h00m ago" in text
    assert "re-check" in text.lower() or "stale" in text.lower()


def test_day_label_for_previous_day(env):
    ctx, clock = env()
    turn(ctx)
    clock.t += 20 * HOUR
    assert "(yesterday 19:00)" in turn(ctx)


def test_cutoff_line_matches_model_by_substring(env):
    ctx, _ = env({"cutoffs": {"claude-opus-5": "2026-03"}})
    assert "training cutoff 2026-03" in turn(ctx, model="anthropic/claude-opus-5-5")
    assert "training cutoff" not in turn(ctx, sid="s2", model="gpt-6")


def test_label_is_configurable(env):
    ctx, _ = env({"label": "clock"})
    assert turn(ctx).startswith("[clock]")


def test_clock_off_and_not_stale_injects_nothing(env):
    ctx, clock = env({"clock": False})
    turn(ctx)
    clock.t += 5 * MIN
    assert turn(ctx) == ""


def test_disabled_plugin_injects_nothing(env):
    ctx, _ = env({"enabled": False})
    assert turn(ctx) == ""


# --- stale gap and re-check -----------------------------------------------------


def test_short_gap_has_no_recheck(env):
    ctx, clock = env()
    turn(ctx)
    clock.t += 10 * MIN
    assert "re-check" not in turn(ctx).lower()


def test_stale_gap_adds_configured_recheck_hint(env):
    ctx, clock = env({"recheck_hint": "Re-read the realm state; ticks have advanced."})
    turn(ctx)
    clock.t += 2 * HOUR
    text = turn(ctx)
    assert "stale" in text
    assert "Re-read the realm state; ticks have advanced." in text


def test_stale_threshold_is_configurable(env):
    ctx, clock = env({"stale_after_minutes": 5})
    turn(ctx)
    clock.t += 6 * MIN
    assert "stale" in turn(ctx)


# --- ledger: observations ------------------------------------------------------


def test_observations_listed_after_stale_gap(env):
    # observation_details opts into argument text (#6); default is metadata only.
    ctx, clock = env({"observation_details": True})
    turn(ctx)
    tool(ctx, "terminal", {"command": "gh pr view 19 --repo acme/app"})
    clock.t += 3 * HOUR
    text = turn(ctx)
    assert "gh pr view 19" in text
    assert "3h00m ago" in text


def test_observation_patterns_are_configurable(env):
    ctx, clock = env({"observe": ["browser_*"], "observation_details": True})
    turn(ctx)
    tool(ctx, "terminal", {"command": "ls"})
    tool(ctx, "browser_exec", {"code": "# Open game realm page\nnew_tab(x)"})
    clock.t += 3 * HOUR
    text = turn(ctx)
    assert "ls" not in text.split("Observed")[1]
    assert "Open game realm page" in text


def test_observation_records_failure_status(env):
    ctx, clock = env({"observation_details": True})
    turn(ctx)
    tool(ctx, "terminal", {"command": "make deploy"}, status="error")
    clock.t += 3 * HOUR
    text = turn(ctx)
    # recorded text is JSON-quoted, and the status renders quoted too
    assert "make deploy" in text and "error" in text


def test_repeat_observation_keeps_only_latest(env):
    ctx, clock = env({"observation_details": True})
    turn(ctx)
    tool(ctx, "terminal", {"command": "git status"})
    clock.t += 30 * MIN
    tool(ctx, "terminal", {"command": "git status"})
    clock.t += 2 * HOUR
    text = turn(ctx)
    assert text.count("git status") == 1
    assert "2h00m ago" in text


def test_observations_are_capped(env):
    ctx, clock = env({"max_observations": 3, "reconcile_lines": 10,
                      "observation_details": True})
    turn(ctx)
    for i in range(6):
        tool(ctx, "terminal", {"command": f"cmd{i}"})
    clock.t += 3 * HOUR
    text = turn(ctx)
    assert "cmd0" not in text and "cmd5" in text and "cmd3" in text


def test_tick_tool_calls_are_not_observations(env):
    ctx, clock = env({"observe": ["*"]})
    turn(ctx)
    tool(ctx, "tick", {"action": "list"})
    clock.t += 3 * HOUR
    assert "Observed" not in turn(ctx)


def test_ledger_off_records_nothing(env):
    ctx, clock = env({"ledger": False})
    turn(ctx)
    tool(ctx, "terminal", {"command": "tea pr view 19"})
    clock.t += 3 * HOUR
    assert "tea pr view" not in turn(ctx)
    assert "tick" not in ctx.tools


# --- ledger: commitments (tick tool) ---------------------------------------------


def test_tick_tool_adds_and_lists_items(env):
    ctx, _ = env()
    r = call_tool(ctx, action="add", kind="decision", text="Use tick as the plugin name")
    assert r["ok"] and r["id"] == 1
    items = call_tool(ctx, action="list")["items"]
    assert items[0]["text"] == "Use tick as the plugin name"


def test_open_items_shown_after_stale_gap_and_done_items_hidden(env):
    ctx, clock = env()
    turn(ctx)
    call_tool(ctx, action="add", kind="open", text="review verdict on PR 3")
    call_tool(ctx, action="add", kind="open", text="already closed thing")
    call_tool(ctx, action="done", id=2)
    clock.t += 3 * HOUR
    text = turn(ctx)
    assert "review verdict on PR 3" in text
    assert "already closed thing" not in text


def test_due_check_surfaces_even_without_stale_gap(env):
    ctx, clock = env()
    turn(ctx)
    call_tool(ctx, action="add", kind="check", text="PR 19 CI", after_minutes=10)
    clock.t += 5 * MIN
    assert "PR 19 CI" not in turn(ctx)
    clock.t += 6 * MIN
    text = turn(ctx)
    assert "Due" in text and "PR 19 CI" in text


def test_tool_rejects_bad_input_with_message(env):
    ctx, _ = env()
    assert call_tool(ctx, action="add", kind="nope", text="x")["ok"] is False
    assert call_tool(ctx, action="add", kind="open", text="")["ok"] is False
    assert call_tool(ctx, action="done", id=99)["ok"] is False
    assert call_tool(ctx, action="explode")["ok"] is False


def test_tool_schema_lists_actions_and_kinds(env):
    ctx, _ = env()
    schema = ctx.tools["tick"][1]
    props = schema["parameters"]["properties"]
    assert set(props["action"]["enum"]) == {"add", "done", "list", "tz"}
    assert set(props["kind"]["enum"]) == {"decision", "open", "check"}


def test_sessions_are_isolated(env):
    ctx, _ = env()
    call_tool(ctx, sid="a", action="add", kind="open", text="only in a")
    assert call_tool(ctx, sid="b", action="list")["items"] == []


# --- compaction re-grounding ---------------------------------------------------


def test_compaction_triggers_reconcile_on_next_turn_only(env):
    ctx, clock = env()
    turn(ctx)
    call_tool(ctx, action="add", kind="decision", text="Ship v0.1 first")
    ctx.hooks["pre_auxiliary_call"](aux_task="compression", session_id="s1")
    clock.t += 1 * MIN
    text = turn(ctx)
    assert "compacted" in text and "Ship v0.1 first" in text
    clock.t += 1 * MIN
    assert "Ship v0.1 first" not in turn(ctx)


def test_other_aux_tasks_do_not_trigger_reconcile(env):
    ctx, clock = env()
    turn(ctx)
    call_tool(ctx, action="add", kind="decision", text="Ship v0.1 first")
    ctx.hooks["pre_auxiliary_call"](aux_task="title_generation", session_id="s1")
    clock.t += 1 * MIN
    assert "Ship v0.1 first" not in turn(ctx)


def test_ledger_carries_into_child_session_after_rotation(env):
    ctx, clock = env()
    turn(ctx, sid="old")
    call_tool(ctx, sid="old", action="add", kind="open", text="carry me")
    ctx.hooks["pre_auxiliary_call"](aux_task="compression", session_id="old")
    clock.t += 1 * MIN
    text = turn(ctx, sid="new", parent="old")
    assert "carry me" in text
    assert "last message 1m ago" in text
    assert call_tool(ctx, sid="new", action="list")["items"][0]["text"] == "carry me"


# --- fail-open ----------------------------------------------------------------


def test_hooks_fail_open_on_broken_state(env):
    ctx, clock = env()

    def boom(*a, **k):
        raise RuntimeError("disk gone")

    ctx.state.get = boom
    ctx.state.set = boom
    assert turn(ctx) is not None
    tool(ctx, "terminal", {"command": "ls"})
    ctx.hooks["pre_auxiliary_call"](aux_task="compression", session_id="s1")
    assert call_tool(ctx, action="list")["ok"] is False


def test_odd_session_ids_are_stored_safely(env):
    ctx, clock = env()
    turn(ctx, sid="telegram:-100123/thread 7")
    clock.t += 2 * HOUR
    assert "last message 2h00m ago" in turn(ctx, sid="telegram:-100123/thread 7")
    for key in ctx.state.data:
        assert len(key) <= 128 and "/" not in key and " " not in key


def test_manifest_defaults_match_code_defaults():
    yaml = pytest.importorskip("yaml")
    schema = yaml.safe_load((ROOT / "plugin.yaml").read_text())["config_schema"]
    assert set(schema) == set(tick.DEFAULTS)
    for key, spec in schema.items():
        assert spec["default"] == tick.DEFAULTS[key], key


# --- Review regressions (v0.1) ------------------------------------------------------


class RacyState(FakeState):
    """Snapshot reads with individually atomic get/set: the shape that loses updates
    when the plugin's load-modify-save is not serialized. A barrier forces overlap."""

    def __init__(self, parties):
        super().__init__()
        import threading

        self.barrier = threading.Barrier(parties, timeout=0.3)

    def get(self, key, default=None):
        snap = super().get(key, default)
        try:
            self.barrier.wait()
        except Exception:
            pass  # serialized callers never meet: proceed
        return snap


def test_concurrent_observations_and_adds_all_survive(env):
    import threading

    ctx, _ = env({"observation_details": True})
    ctx.state = RacyState(4)
    tick.register(ctx)  # rebind to the racy state
    jobs = [
        lambda: tool(ctx, "terminal", {"command": "one"}),
        lambda: tool(ctx, "terminal", {"command": "two"}),
        lambda: call_tool(ctx, action="add", kind="open", text="A"),
        lambda: call_tool(ctx, action="add", kind="open", text="B"),
    ]
    threads = [threading.Thread(target=j) for j in jobs]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    data = ctx.state.data["s:s1"]
    assert sorted(o["what"] for o in data["obs"]) == ["one", "two"]
    assert sorted(i["text"] for i in data["items"]) == ["A", "B"]
    assert sorted(i["id"] for i in data["items"]) == [1, 2]


def test_inheritance_keeps_child_entries_written_before_first_turn(env):
    ctx, clock = env({"observation_details": True})
    turn(ctx, sid="old")
    call_tool(ctx, sid="old", action="add", kind="open", text="parent item")
    ctx.hooks["pre_auxiliary_call"](aux_task="compression", session_id="old")
    tool(ctx, "terminal", {"command": "child obs"}, sid="new")
    call_tool(ctx, sid="new", action="add", kind="check", text="child check", after_minutes=0)
    clock.t += 60
    text = turn(ctx, sid="new", parent="old")
    assert "parent item" in text and "child obs" in text and "child check" in text
    items = call_tool(ctx, sid="new", action="list")["items"]
    assert sorted(i["id"] for i in items) == [1, 2]
    assert call_tool(ctx, sid="new", action="add", kind="open", text="next")["id"] == 3


def test_inheritance_happens_once(env):
    ctx, clock = env()
    turn(ctx, sid="old")
    call_tool(ctx, sid="old", action="add", kind="open", text="once")
    turn(ctx, sid="new", parent="old")
    turn(ctx, sid="new", parent="old")
    assert [i["text"] for i in call_tool(ctx, sid="new", action="list")["items"]] == ["once"]


def test_hashed_and_raw_session_keys_cannot_alias():
    odd = "telegram:-100123/thread 7"
    hashed = tick._key(odd)
    alias = hashed.split(":", 1)[1]
    assert tick._key(alias) != hashed
    assert tick._key("h:" + alias) != hashed


def test_master_switch_disables_tool(env):
    ctx, _ = env({"enabled": False})
    assert "tick" not in ctx.tools
    handler = tick.Tick(ctx).tool
    assert json.loads(handler({"action": "add", "kind": "open", "text": "x"}, session_id="s1"))["ok"] is False


def test_acknowledged_child_id_survives_inheritance(env):
    """Review regression: done(id) after inheritance must close the item that add returned."""
    ctx, clock = env()
    turn(ctx, sid="old")
    parent_id = call_tool(ctx, sid="old", action="add", kind="open", text="parent obligation")["id"]
    ctx.hooks["pre_auxiliary_call"](aux_task="compression", session_id="old")
    child_id = call_tool(ctx, sid="new", action="add", kind="check", text="child check",
                         after_minutes=0)["id"]
    assert child_id != parent_id
    clock.t += 60
    turn(ctx, sid="new", parent="old")
    assert call_tool(ctx, sid="new", action="done", id=child_id)["ok"]
    open_items = call_tool(ctx, sid="new", action="list")["items"]
    assert [(i["id"], i["text"]) for i in open_items] == [(parent_id, "parent obligation")]


def test_item_ids_are_unique_across_sessions(env):
    ctx, _ = env()
    a = call_tool(ctx, sid="a", action="add", kind="open", text="x")["id"]
    b = call_tool(ctx, sid="b", action="add", kind="open", text="y")["id"]
    assert a != b

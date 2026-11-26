"""Behaviour tests for tick. Each test fails if the feature it names is removed."""
from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import sys
import threading
import time
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


# housekeeping ----------------------------------------------------------------------


class FileState:
    """File-backed like the real ctx.state: every get() parses the whole file."""

    def __init__(self, path):
        self.path = path
        self.gets = 0
        path.write_text("{}")

    def _all(self):
        return json.loads(self.path.read_text())

    def get(self, key, default=None):
        self.gets += 1
        return self._all().get(key, default)

    def set(self, key, value):
        data = self._all()
        if value is None:
            data.pop(key, None)
        else:
            data[key] = value
        self.path.write_text(json.dumps(data))


def _gc_env(env, tmp_path, fresh=200, expired=3):
    ctx, clock = env(now=T0)
    st = FileState(tmp_path / "state.json")
    old = T0 - 31 * 86400
    data = {f"s:fresh{i}": {"touched": T0 - HOUR} for i in range(fresh)}
    data.update({f"s:old{i}": {"touched": old} for i in range(expired)})
    data["cfg:other"] = {"touched": old}  # not a session record: never collected
    st.path.write_text(json.dumps(data))
    ctx.state = st
    return ctx, st


def test_gc_drops_only_expired_session_records(env, tmp_path):
    ctx, st = _gc_env(env, tmp_path)
    turn(ctx, sid="s:trigger")
    left = st._all()
    assert not [k for k in left if k.startswith("s:old")]
    assert sum(k.startswith("s:fresh") for k in left) == 200
    assert "cfg:other" in left


def test_gc_reads_the_store_once_not_once_per_key(env, tmp_path):
    # Before 0.2.3 gc called state.get() for every key, and each get re-parses the
    # file: on a store with thousands of sessions that was minutes of CPU under
    # the ledger lock, long enough to stall every hook in the process.
    ctx, st = _gc_env(env, tmp_path, fresh=200, expired=3)
    turn(ctx, sid="s:trigger")
    assert st.gets <= 3 + 5  # the expired candidates, plus the turn's own reads


def test_gc_keeps_a_candidate_touched_after_the_snapshot(env, tmp_path):
    ctx, st = _gc_env(env, tmp_path, fresh=0, expired=1)
    real_get = st.get

    def get(key, default=None):
        if key == "s:old0":  # a turn touched it between the snapshot and the recheck
            st.path.write_text(json.dumps({**st._all(), "s:old0": {"touched": T0}}))
        return real_get(key, default)

    st.get = get
    turn(ctx, sid="s:trigger")
    assert "s:old0" in st._all()


# storage: one file per session (0.3.0) ---------------------------------------------


class DirState(FileState):
    """Shaped like the host's PluginState: a data_dir, whole-file get and set, atomic
    replace, and a None tombstone instead of a delete."""

    def __init__(self, root):
        root.mkdir(parents=True, exist_ok=True)
        super().__init__(root / "state.json")
        self.data_dir = root
        self.sets = 0

    def set(self, key, value):
        self.sets += 1
        data = self._all()
        data[key] = value
        tmp = self.path.with_name("state.json.tmp")
        tmp.write_text(json.dumps(data))
        os.replace(tmp, self.path)


def _dir_ctx(monkeypatch, tmp_path, legacy=None, now=T0):
    clock = Clock(now)
    monkeypatch.setattr(tick, "_now", clock)
    ctx = FakeCtx({"timezone": "America/New_York"})
    ctx.state = DirState(tmp_path / "plugin-data")
    if legacy:
        ctx.state.path.write_text(json.dumps(legacy))
    tick.register(ctx)
    return ctx, clock


def _ledger(ctx):
    return ctx.hooks["pre_llm_call"].__self__.ledger


def _rec(touched, **kw):
    return {"touched": touched, "last": kw.pop("last", touched), "obs": [], "items": [], **kw}


def test_hooks_do_not_parse_or_rewrite_the_shared_store(monkeypatch, tmp_path):
    # 0.2.x kept every session in ctx.state, so each hook parsed and rewrote the whole
    # file under one profile lock: 0.49 s per hook on Lyra's 6.1 MB store.
    legacy = {f"s:old{i}": _rec(T0 - HOUR) for i in range(50)}
    ctx, clock = _dir_ctx(monkeypatch, tmp_path, legacy)
    st = ctx.state
    st.gets = st.sets = 0
    for _ in range(10):
        turn(ctx, sid="s1")
        tool(ctx, "terminal", {"command": "ls"}, sid="s1")
        clock.t += MIN
    assert st.sets == 0
    assert st.gets <= 2  # the two client-zone keys, read once, then cached on the file stat


def test_migration_copies_live_sessions_and_the_id_counter(monkeypatch, tmp_path):
    legacy = {
        "s:live": _rec(T0 - HOUR, last=T0 - 2 * HOUR,
                       items=[{"id": 7, "kind": "open", "text": "carry me", "done": False, "t": T0 - HOUR}]),
        "s:old": _rec(T0 - 31 * 86400),
        "ids:next": 8,
        "cfg:other": {"x": 1},
    }
    ctx, _ = _dir_ctx(monkeypatch, tmp_path, legacy)
    files = _ledger(ctx).files
    assert files.get("s:live")["items"][0]["text"] == "carry me"
    assert files.get("s:old") is None  # expired records are not carried
    assert "last message 2h00m ago" in turn(ctx, sid="live")
    assert call_tool(ctx, sid="live", action="add", kind="open", text="next")["id"] == 8


def test_migration_runs_once_and_never_overwrites_a_newer_record(monkeypatch, tmp_path):
    legacy = {"s:live": _rec(T0 - HOUR, items=[{"id": 1, "kind": "open", "text": "old", "done": False, "t": T0}])}
    ctx, _ = _dir_ctx(monkeypatch, tmp_path, legacy)
    call_tool(ctx, sid="live", action="add", kind="open", text="after")
    files = _ledger(ctx).files
    os.unlink(files.dir / tick.MIGRATED)  # force a second migration pass
    tick.register(ctx)
    assert [i["text"] for i in files.get("s:live")["items"]] == ["old", "after"]
    assert (files.dir / tick.MIGRATED).exists()


def _hold(led, sid):
    entered, release = threading.Event(), threading.Event()

    def run():
        with led.update(sid):
            entered.set()
            release.wait(10)

    th = threading.Thread(target=run)
    th.start()
    assert entered.wait(5)
    return release, th


def test_a_busy_session_never_blocks_another_session(monkeypatch, tmp_path):
    ctx, _ = _dir_ctx(monkeypatch, tmp_path)
    led = _ledger(ctx)
    files = getattr(led, "files", None)
    a, b = "busy", "other"
    if files is not None:  # pick a session on another lock stripe
        b = next(s for s in (f"other{i}" for i in range(500))
                 if files.lock_file(tick._key(s)) != files.lock_file(tick._key(a)))
    release, th = _hold(led, a)
    start = time.monotonic()
    text = turn(ctx, sid=b)
    took = time.monotonic() - start
    release.set()
    th.join()
    assert "[tick] now" in text
    assert took < 1.0


def test_a_hook_gives_up_on_a_busy_lock_and_fails_open(monkeypatch, tmp_path):
    monkeypatch.setattr(tick, "LOCK_WAIT_S", 0.2, raising=False)
    ctx, _ = _dir_ctx(monkeypatch, tmp_path)
    led = _ledger(ctx)
    release, th = _hold(led, "busy")  # held by a thread in this process
    start = time.monotonic()
    text = turn(ctx, sid="busy")
    took = time.monotonic() - start
    release.set()
    th.join()
    assert "[tick] now" in text  # clock only; the turn is never blocked
    assert took < 2.0

    import fcntl  # held by another process: a separate open file description
    with open(led.files.lock_file(tick._key("busy")), "a+") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        start = time.monotonic()
        tool(ctx, "terminal", {"command": "ls"}, sid="busy")
        assert time.monotonic() - start < 2.0


def test_gc_drops_expired_session_files_without_touching_the_store(monkeypatch, tmp_path):
    legacy = {f"s:fresh{i}": _rec(T0 - HOUR) for i in range(20)}
    legacy.update({f"s:old{i}": _rec(T0 - 29 * 86400) for i in range(3)})
    ctx, clock = _dir_ctx(monkeypatch, tmp_path, legacy)
    files = _ledger(ctx).files
    turn(ctx, sid="trigger")  # first gc pass: nothing is past the TTL yet
    assert files.get("s:old0") is not None
    ctx.state.sets = 0
    clock.t += 2 * 86400  # old* are now 31 days idle, fresh* 2 days
    turn(ctx, sid="trigger")
    assert [k for k in ("s:old0", "s:old1", "s:old2") if files.get(k) is not None] == []
    assert all(files.get(f"s:fresh{i}") is not None for i in range(20))
    assert ctx.state.sets == 0


def test_files_gc_keeps_a_session_touched_after_the_scan(monkeypatch, tmp_path):
    ctx, clock = _dir_ctx(monkeypatch, tmp_path, {"s:old0": _rec(T0 - 29 * 86400)})
    turn(ctx, sid="trigger")
    files = _ledger(ctx).files
    clock.t += 2 * 86400
    target, real_read, seen = files.path("s:old0"), tick.SessionFiles.read, []

    def read(path):
        doc = real_read(path)
        if str(path) == str(target) and not seen:  # a turn touches it after the scan read
            seen.append(1)
            files.set("s:old0", _rec(clock.t))
        return doc

    monkeypatch.setattr(tick.SessionFiles, "read", staticmethod(read))
    turn(ctx, sid="trigger")
    assert seen and files.get("s:old0") is not None


def test_concurrent_sessions_keep_every_item_with_unique_ids(monkeypatch, tmp_path):
    ctx, _ = _dir_ctx(monkeypatch, tmp_path, {"ids:next": 100})
    ids, lock = [], threading.Lock()

    def work(n):
        for j in range(5):
            out = call_tool(ctx, sid=f"p{n}", action="add", kind="open", text=f"{n}.{j}")
            with lock:
                ids.append(out["id"])

    ths = [threading.Thread(target=work, args=(n,)) for n in range(8)]
    [t.start() for t in ths]
    [t.join() for t in ths]
    assert len(ids) == len(set(ids)) == 40 and min(ids) >= 100
    files = _ledger(ctx).files
    assert all(len(files.get(f"s:p{n}")["items"]) == 5 for n in range(8))


def test_zone_cache_rereads_after_the_store_file_changes(monkeypatch, tmp_path):
    ctx, _ = _dir_ctx(monkeypatch, tmp_path)
    assert "set by you" not in turn(ctx, sid="z")
    ctx.state.set("client:override", {"tz": "Europe/Paris", "set": T0, "until": None})
    assert "set by you" in turn(ctx, sid="z")


# round 2: faults never replace a ledger, reuse an id, or stall gc ------------------


def test_an_unreadable_session_file_fails_open_and_is_left_alone(monkeypatch, tmp_path):
    ctx, _ = _dir_ctx(monkeypatch, tmp_path)
    call_tool(ctx, sid="s1", action="add", kind="open", text="keep me")
    path = _ledger(ctx).files.path("s:s1")
    path.write_bytes(b'{"key": "s:s1", "record": {"items": [')  # torn or damaged
    before = path.read_bytes()
    assert "[tick] now" in turn(ctx, sid="s1")  # clock only
    tool(ctx, "terminal", {"command": "ls"}, sid="s1")
    assert call_tool(ctx, sid="s1", action="list")["ok"] is False
    assert path.read_bytes() == before


def test_a_malformed_id_counter_refuses_instead_of_guessing(monkeypatch, tmp_path):
    ctx, _ = _dir_ctx(monkeypatch, tmp_path, {"ids:next": 1})
    assert call_tool(ctx, sid="a", action="add", kind="open", text="one")["id"] == 1
    ids = _ledger(ctx).files.root / "ids.json"
    ids.write_text("{not json")
    out = call_tool(ctx, sid="b", action="add", kind="open", text="two")
    assert out["ok"] is False and "ids.json" in out["error"]
    assert ids.read_text() == "{not json"  # left for an operator, never overwritten


def test_an_unreadable_id_counter_refuses(monkeypatch, tmp_path):
    ctx, _ = _dir_ctx(monkeypatch, tmp_path)
    ids = _ledger(ctx).files.root / "ids.json"
    ids.unlink()
    ids.mkdir()  # present, but no read can succeed
    out = call_tool(ctx, sid="a", action="add", kind="open", text="x")
    assert out["ok"] is False


def test_a_failed_migration_never_loses_a_legacy_ledger(monkeypatch, tmp_path):
    legacy = {
        "s:a": _rec(T0 - HOUR, items=[{"id": 1, "kind": "open", "text": "a-item", "done": False, "t": T0}]),
        "s:b": _rec(T0 - HOUR, items=[{"id": 2, "kind": "open", "text": "b-item", "done": False, "t": T0}]),
        "ids:next": 3,
    }
    real_set = tick.SessionFiles.set
    calls = []

    def flaky(self, key, record):
        calls.append(key)
        if len(calls) == 1:
            raise OSError("disk full")
        return real_set(self, key, record)

    monkeypatch.setattr(tick.SessionFiles, "set", flaky)
    ctx, _ = _dir_ctx(monkeypatch, tmp_path, legacy)
    files = _ledger(ctx).files
    assert not (files.dir / tick.MIGRATED).exists()  # incomplete: retried next start
    failed = calls[0]
    sid = failed[2:]
    call_tool(ctx, sid=sid, action="add", kind="open", text="new")  # writes before the retry
    texts = [i["text"] for i in files.get(failed)["items"]]
    assert texts == [f"{sid}-item", "new"]
    tick.register(ctx)  # the retry completes and keeps both records whole
    assert (files.dir / tick.MIGRATED).exists()
    assert [i["text"] for i in files.get(failed)["items"]] == [f"{sid}-item", "new"]
    assert {"s:a", "s:b"} <= {k for k in ("s:a", "s:b") if files.get(k)}


def test_a_non_finite_legacy_value_does_not_stop_migration(monkeypatch, tmp_path):
    # 0.2.x stored after_minutes "inf" as Infinity; the host store allows it.
    legacy = {
        "s:inf": _rec(T0 - HOUR, items=[{"id": 1, "kind": "check", "text": "x", "done": False,
                                         "t": T0, "due": float("inf")}]),
        "s:after": _rec(T0 - HOUR),
    }
    ctx, _ = _dir_ctx(monkeypatch, tmp_path, legacy)
    files = _ledger(ctx).files
    assert files.get("s:after") is not None and files.get("s:inf") is not None
    assert (files.dir / tick.MIGRATED).exists()


def test_fallback_gc_never_loses_a_write_made_during_its_recheck(env, tmp_path):
    ctx, clock = env({"session_ttl_days": 30})
    st = ctx.state = FileState(tmp_path / "state.json")  # a store path, no data_dir
    call_tool(ctx, sid="busy", action="add", kind="open", text="mine")
    clock.t += 31 * 86400  # idle past the TTL: a gc candidate
    real_get, main, workers = st.get, threading.current_thread(), []

    def get(key, default=None):
        stale = real_get(key, default)
        if key == "s:busy" and threading.current_thread() is main and not workers:
            # gc has re-read the candidate; an add for that session runs now
            w = threading.Thread(target=call_tool, args=(ctx,),
                                 kwargs=dict(sid="busy", action="add", kind="open", text="new"))
            workers.append(w)
            w.start()
            w.join(0.5)
        return stale

    st.get = get
    turn(ctx, sid="trigger")
    workers[0].join(10)
    rec = st._all().get("s:busy")
    assert rec is not None and "new" in [i["text"] for i in rec["items"]]


def test_files_gc_skips_busy_sessions_without_adding_up_waits(monkeypatch, tmp_path):
    legacy = {f"s:old{i}": _rec(T0 - 29 * 86400) for i in range(7)}
    ctx, clock = _dir_ctx(monkeypatch, tmp_path, legacy)
    turn(ctx, sid="trigger")
    clock.t += 2 * 86400
    files = _ledger(ctx).files
    import fcntl
    handles = []
    for lf in {files.lock_file(f"s:old{i}") for i in range(7)}:  # another process holds them
        fh = open(lf, "a+")
        fcntl.flock(fh, fcntl.LOCK_EX)
        handles.append(fh)
    held = {files.lock_file(f"s:old{i}") for i in range(7)}
    sid = next(s for s in (f"t{i}" for i in range(500)) if files.lock_file(tick._key(s)) not in held)
    try:
        start = time.monotonic()
        turn(ctx, sid=sid)
        took = time.monotonic() - start
    finally:
        [fh.close() for fh in handles]
    assert took < 2.0
    assert all(files.get(f"s:old{i}") is not None for i in range(7))


def test_a_disabled_plugin_does_not_migrate(monkeypatch, tmp_path):
    clock = Clock(T0)
    monkeypatch.setattr(tick, "_now", clock)
    ctx = FakeCtx({"timezone": "America/New_York", "enabled": False})
    ctx.state = DirState(tmp_path / "plugin-data")
    ctx.state.path.write_text(json.dumps({"s:a": _rec(T0 - HOUR)}))
    tick.register(ctx)
    assert not (tmp_path / "plugin-data" / "sessions").exists()


# round 4: the counter is the only memory of removed ids; inheritance names its parents


def test_migration_seeds_the_counter_from_the_legacy_counter(monkeypatch, tmp_path):
    legacy = {"s:old": _rec(T0 - HOUR, items=[{"id": 3, "kind": "open", "text": "o", "done": False, "t": T0}]),
              "ids:next": 9}  # ids 4..8 were used and pruned under 0.2.x
    ctx, _ = _dir_ctx(monkeypatch, tmp_path, legacy)
    assert json.loads((_ledger(ctx).files.root / "ids.json").read_text()) == {"next": 9}
    assert call_tool(ctx, sid="new", action="add", kind="open", text="n")["id"] == 9


def test_a_legacy_store_without_a_counter_seeds_above_its_records(monkeypatch, tmp_path):
    legacy = {"s:old": _rec(T0 - HOUR, items=[{"id": 5, "kind": "open", "text": "o", "done": False, "t": T0}])}
    ctx, _ = _dir_ctx(monkeypatch, tmp_path, legacy)
    assert call_tool(ctx, sid="new", action="add", kind="open", text="n")["id"] == 6


def test_a_failed_migration_still_seeds_the_counter(monkeypatch, tmp_path):
    legacy = {
        "s:parent": _rec(T0 - HOUR, items=[{"id": 5, "kind": "open", "text": "p", "done": False, "t": T0}]),
        "ids:next": 6,
    }
    real_set = tick.SessionFiles.set

    def refuse_parent(self, key, record):
        if key == "s:parent":
            raise OSError("disk full")
        return real_set(self, key, record)

    monkeypatch.setattr(tick.SessionFiles, "set", refuse_parent)
    ctx, _ = _dir_ctx(monkeypatch, tmp_path, legacy)
    assert not _ledger(ctx).migrated(_ledger(ctx).files)
    assert call_tool(ctx, sid="other", action="add", kind="open", text="o")["id"] == 6


def test_a_malformed_legacy_counter_refuses_adds_until_set(monkeypatch, tmp_path):
    legacy = {"s:parent": _rec(T0 - HOUR, items=[{"id": 1, "kind": "open", "text": "p", "done": False, "t": T0}]),
              "ids:next": "bad"}
    ctx, _ = _dir_ctx(monkeypatch, tmp_path, legacy)
    ids = _ledger(ctx).files.root / "ids.json"
    assert not ids.exists()
    assert call_tool(ctx, sid="other", action="add", kind="open", text="o")["ok"] is False
    ids.write_text(json.dumps({"next": 40}))  # the operator's fix
    assert call_tool(ctx, sid="other", action="add", kind="open", text="o")["id"] == 40


@pytest.mark.parametrize("damage", ["delete", "malform"])
@pytest.mark.parametrize("retention", ["gc", "prune"])
def test_a_counter_lost_after_retention_never_reuses_an_id(monkeypatch, tmp_path, damage, retention):
    ctx, clock = _dir_ctx(monkeypatch, tmp_path)
    files = _ledger(ctx).files
    if retention == "gc":  # the record holding the top id is gone
        call_tool(ctx, sid="old", action="add", kind="open", text="o")
        files.delete("s:old")
    else:  # max_items pruned the closed top item
        top = call_tool(ctx, sid="a", action="add", kind="open", text="t")["id"]
        call_tool(ctx, sid="a", action="done", id=top)
        rec = files.get("s:a")
        rec["items"] = []
        files.set("s:a", rec)
    ids = files.root / "ids.json"
    os.unlink(ids) if damage == "delete" else ids.write_text("[]")
    assert call_tool(ctx, sid="b", action="add", kind="open", text="n")["ok"] is False


def test_the_fallback_counter_refuses_when_malformed(env, tmp_path):
    ctx, _ = env()
    st = ctx.state = FileState(tmp_path / "state.json")  # a store path, no data_dir
    assert call_tool(ctx, sid="parent", action="add", kind="open", text="p")["id"] == 1
    st.set("ids:next", "bad")
    assert call_tool(ctx, sid="child", action="add", kind="open", text="c")["ok"] is False


def test_the_fallback_counter_refuses_when_removed_after_first_use(env):
    ctx, _ = env()  # FakeState: neither data_dir nor path
    assert call_tool(ctx, sid="parent", action="add", kind="open", text="p")["id"] == 1
    ctx.state.set("ids:next", None)
    assert call_tool(ctx, sid="child", action="add", kind="open", text="c")["ok"] is False


def test_the_fallback_counter_starts_at_one_on_a_new_store(env):
    ctx, _ = env()
    assert call_tool(ctx, sid="a", action="add", kind="open", text="x")["id"] == 1
    assert call_tool(ctx, sid="a", action="add", kind="open", text="y")["id"] == 2


# round 5: the counter is seeded once; a store that holds ids is never "fresh"


def _half_migrated(monkeypatch, tmp_path):
    """A 0.2.x store whose copy of s:stuck keeps failing, so migration never completes."""
    legacy = {"s:stuck": _rec(T0 - HOUR, items=[{"id": 4, "kind": "open", "text": "s", "done": False, "t": T0}]),
              "ids:next": 5}
    real_set = tick.SessionFiles.set

    def refuse_stuck(self, key, record):
        if key == "s:stuck":
            raise OSError("disk full")
        return real_set(self, key, record)

    monkeypatch.setattr(tick.SessionFiles, "set", refuse_stuck)
    ctx, clock = _dir_ctx(monkeypatch, tmp_path, legacy)
    assert not _ledger(ctx).migrated(_ledger(ctx).files)
    return ctx, legacy


def test_a_lost_counter_is_not_reseeded_while_migration_is_incomplete(monkeypatch, tmp_path):
    ctx, _ = _half_migrated(monkeypatch, tmp_path)
    assert call_tool(ctx, sid="a", action="add", kind="open", text="a")["id"] == 5
    assert call_tool(ctx, sid="a", action="add", kind="open", text="b")["id"] == 6
    ids = _ledger(ctx).files.root / "ids.json"
    ids.unlink()
    assert call_tool(ctx, sid="b", action="add", kind="open", text="c")["ok"] is False
    assert not ids.exists()  # not reseeded at 5 from the stale ids:next


def test_a_restart_does_not_reseed_a_lost_counter(monkeypatch, tmp_path):
    ctx, legacy = _half_migrated(monkeypatch, tmp_path)
    assert call_tool(ctx, sid="a", action="add", kind="open", text="a")["id"] == 5
    ids = _ledger(ctx).files.root / "ids.json"
    ids.unlink()
    tick.register(ctx)  # a new process runs migrate() again
    _ledger(ctx).migrate()
    assert not ids.exists()
    assert call_tool(ctx, sid="b", action="add", kind="open", text="c")["ok"] is False


def test_an_operator_set_counter_is_marked_on_first_use(monkeypatch, tmp_path):
    legacy = {"s:p": _rec(T0 - HOUR, items=[{"id": 1, "kind": "open", "text": "p", "done": False, "t": T0}]),
              "ids:next": "bad"}
    ctx, _ = _dir_ctx(monkeypatch, tmp_path, legacy)
    root = _ledger(ctx).files.root
    assert not (root / "ids.seeded").exists()
    (root / "ids.json").write_text(json.dumps({"next": 40}))
    assert call_tool(ctx, sid="o", action="add", kind="open", text="o")["id"] == 40
    assert (root / "ids.seeded").exists()
    (root / "ids.json").unlink()
    assert call_tool(ctx, sid="o", action="add", kind="open", text="o")["ok"] is False


def test_an_unreadable_seed_mark_refuses(monkeypatch, tmp_path):
    ctx, _ = _half_migrated(monkeypatch, tmp_path)
    root = _ledger(ctx).files.root
    (root / "ids.json").unlink()
    (root / "ids.seeded").unlink(missing_ok=True)  # only round 5 writes it
    real_stat = os.stat

    def deny(path, *a, **kw):
        if str(path).endswith("ids.seeded"):
            raise PermissionError("denied")
        return real_stat(path, *a, **kw)

    monkeypatch.setattr(tick.os, "stat", deny)
    assert call_tool(ctx, sid="b", action="add", kind="open", text="c")["ok"] is False
    assert not (root / "ids.json").exists()


def _fallback_file(env, tmp_path, data):
    ctx, clock = env()
    st = ctx.state = FileState(tmp_path / "state.json")  # a store path, no data_dir
    st.path.write_text(json.dumps(data))
    return ctx


def test_an_upgraded_fallback_store_without_a_counter_refuses(env, tmp_path):
    item = {"id": 4, "kind": "open", "text": "o", "done": False, "t": T0}
    ctx = _fallback_file(env, tmp_path, {"s:old": {"touched": T0, "items": [item]}})
    assert call_tool(ctx, sid="new", action="add", kind="open", text="n")["ok"] is False


def test_a_fresh_fallback_store_with_records_but_no_ids_starts_at_one(env, tmp_path):
    ctx = _fallback_file(env, tmp_path, {})
    turn(ctx, sid="a")  # writes s:a with no items
    assert call_tool(ctx, sid="a", action="add", kind="open", text="x")["id"] == 1


def test_a_pathless_fallback_refuses_when_its_own_record_holds_ids(env):
    ctx, _ = env()
    assert call_tool(ctx, sid="a", action="add", kind="open", text="x")["id"] == 1
    ctx.state.set("ids:next", None)
    ctx.state.set("ids:seeded", None)  # as a store written before the marker existed
    assert call_tool(ctx, sid="a", action="add", kind="open", text="y")["ok"] is False


def _torn(ctx, sid):
    path = _ledger(ctx).files.path("s:" + sid)
    good = path.read_bytes()
    path.write_bytes(b"{torn")
    return lambda: path.write_bytes(good)


def test_an_unreadable_parent_defers_inheritance_until_it_reads(monkeypatch, tmp_path):
    ctx, clock = _dir_ctx(monkeypatch, tmp_path)
    call_tool(ctx, sid="parent", action="add", kind="open", text="parent obligation")
    heal = _torn(ctx, "parent")
    turn(ctx, sid="child", parent="parent")
    assert _ledger(ctx).files.get("s:child")["inherit_from"] == ["parent"]
    heal()
    clock.t += MIN
    turn(ctx, sid="child", parent="parent")
    rec = _ledger(ctx).files.get("s:child")
    assert "inherit_from" not in rec and [i["text"] for i in rec["items"]] == ["parent obligation"]


def test_a_deferred_inheritance_retries_without_the_parent_id(monkeypatch, tmp_path):
    ctx, clock = _dir_ctx(monkeypatch, tmp_path)
    call_tool(ctx, sid="parent", action="add", kind="open", text="parent obligation")
    heal = _torn(ctx, "parent")
    turn(ctx, sid="child", parent="parent")
    heal()
    clock.t += MIN
    turn(ctx, sid="child")  # the host does not repeat parent_session_id
    rec = _ledger(ctx).files.get("s:child")
    assert [i["text"] for i in rec["items"]] == ["parent obligation"]


def test_rotating_while_pending_keeps_the_grandparents_obligations(monkeypatch, tmp_path):
    ctx, clock = _dir_ctx(monkeypatch, tmp_path)
    call_tool(ctx, sid="a", action="add", kind="open", text="grandparent obligation")
    heal = _torn(ctx, "a")
    turn(ctx, sid="b", parent="a")
    call_tool(ctx, sid="b", action="add", kind="open", text="child obligation")
    clock.t += MIN
    turn(ctx, sid="c", parent="b")  # b compacts into c while a is still unreadable
    assert _ledger(ctx).files.get("s:c")["inherit_from"] == ["a"]
    heal()
    clock.t += MIN
    turn(ctx, sid="c")
    rec = _ledger(ctx).files.get("s:c")
    assert sorted(i["text"] for i in rec["items"]) == ["child obligation", "grandparent obligation"]
    assert "inherit_from" not in rec


def test_an_inheritance_cycle_terminates(monkeypatch, tmp_path):
    ctx, clock = _dir_ctx(monkeypatch, tmp_path)
    files = _ledger(ctx).files
    files.set("s:x", {**tick._empty(), "last": T0, "inherit_from": ["y"]})
    files.set("s:y", {**tick._empty(), "last": T0, "inherit_from": ["x"]})
    turn(ctx, sid="z", parent="x")
    assert "inherit_from" not in files.get("s:z")

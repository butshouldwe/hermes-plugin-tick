"""v0.2 behaviour: the clock follows the user's client zone, not the host's."""
from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
from datetime import datetime, timezone

import pytest

from test_tick import FakeCtx, T0, HOUR, Clock, call_tool, tick, turn

ROOT = pathlib.Path(__file__).resolve().parents[1]
DAY = 24 * HOUR


@pytest.fixture
def env(monkeypatch):
    def make(settings=None, now=T0):
        base = {"timezone": "America/New_York"}
        base.update(settings or {})
        ctx = FakeCtx(base)
        ctx.commands = {}
        ctx.register_command = lambda name, handler, **kw: ctx.commands.__setitem__(name, handler)
        clock = Clock(now)
        monkeypatch.setattr(tick, "_now", clock)
        tick.register(ctx)
        return ctx, clock

    return make


def device(ctx, zone, seen):
    ctx.state.set("client:device", {"tz": zone, "seen": seen, "source": "desktop"})


# --- resolution order -----------------------------------------------------------


def test_home_zone_when_no_client_report(env):
    ctx, _ = env()
    text = turn(ctx)
    assert "19:00 EDT (UTC-04:00)" in text
    assert "home" not in text and "your device" not in text


def test_fresh_device_report_wins_and_home_is_shown(env):
    ctx, _ = env()
    device(ctx, "America/Los_Angeles", T0 - HOUR)
    text = turn(ctx)
    assert "16:00 PDT (UTC-07:00, your device)" in text
    assert "home 19:00 EDT" in text
    assert "2026-10-09T23:00Z" in text


def test_device_report_in_home_zone_shows_single_zone(env):
    ctx, _ = env()
    device(ctx, "America/New_York", T0 - HOUR)
    text = turn(ctx)
    assert "19:00 EDT (UTC-04:00)" in text and "home" not in text


def test_stale_device_report_is_ignored(env):
    ctx, _ = env({"device_max_age_hours": 72})
    device(ctx, "America/Los_Angeles", T0 - 73 * HOUR)
    assert "PDT" not in turn(ctx)


def test_override_beats_device_until_it_expires(env):
    ctx, clock = env()
    device(ctx, "America/Los_Angeles", T0 - HOUR)
    r = call_tool(ctx, action="tz", zone="Europe/London", days=2)
    assert r["ok"] and r["zone"] == "Europe/London"
    assert "00:00 BST (UTC+01:00, set by you)" in turn(ctx)
    clock.t += 2 * DAY + 60
    device(ctx, "America/Los_Angeles", clock.t - 60)
    assert "PDT" in turn(ctx, sid="s2")


def test_override_home_clears_override(env):
    ctx, _ = env()
    call_tool(ctx, action="tz", zone="Asia/Tokyo")
    assert call_tool(ctx, action="tz", zone="home")["ok"]
    assert "EDT" in turn(ctx) and "JST" not in turn(ctx, sid="s2")


def test_client_zone_is_shared_across_sessions(env):
    ctx, _ = env()
    call_tool(ctx, sid="a", action="tz", zone="Asia/Tokyo")
    assert "JST" in turn(ctx, sid="b")


def test_client_timezone_off_ignores_reports_and_overrides(env):
    ctx, _ = env({"client_timezone": False})
    device(ctx, "America/Los_Angeles", T0 - HOUR)
    ctx.state.set("client:override", {"tz": "Asia/Tokyo", "set": T0, "until": None})
    text = turn(ctx)
    assert "EDT" in text and "PDT" not in text and "JST" not in text
    assert "tz" not in ctx.commands


def test_corrupt_client_records_fall_back_to_home(env):
    ctx, _ = env()
    ctx.state.set("client:device", {"tz": "Not/AZone", "seen": T0})
    ctx.state.set("client:override", "garbage")
    assert "19:00 EDT" in turn(ctx)


def test_gap_label_uses_client_zone(env):
    ctx, clock = env()
    device(ctx, "America/Los_Angeles", T0)
    turn(ctx)
    clock.t += 2 * HOUR
    assert "(16:00)" in turn(ctx)


# --- tool and /tz command --------------------------------------------------------


def test_tz_tool_rejects_unknown_zone(env):
    ctx, _ = env()
    r = call_tool(ctx, action="tz", zone="Mars/Olympus")
    assert r["ok"] is False and "IANA" in r["error"]


def test_tz_tool_without_zone_reports_resolution(env):
    ctx, _ = env()
    device(ctx, "America/Chicago", T0 - HOUR)
    r = call_tool(ctx, action="tz")
    assert r["ok"] and r["zone"] == "America/Chicago" and r["source"] == "device"


def test_tz_command_sets_and_shows(env):
    ctx, _ = env()
    out = ctx.commands["tz"]("Europe/Paris 3")
    assert "Europe/Paris" in out and "3 day" in out
    assert "CEST" in turn(ctx)
    assert "Europe/Paris" in ctx.commands["tz"]("")
    assert "home" in ctx.commands["tz"]("home").lower()
    assert "EDT" in turn(ctx, sid="s2")


def test_tz_command_reports_bad_input(env):
    ctx, _ = env()
    assert "IANA" in ctx.commands["tz"]("Nowhere/Land")
    assert "days" in ctx.commands["tz"]("Europe/Paris soon")


def test_schema_includes_tz_action(env):
    ctx, _ = env()
    props = ctx.tools["tick"][1]["parameters"]["properties"]
    assert "tz" in props["action"]["enum"]
    assert "zone" in props and "days" in props


# --- desktop backend (dashboard/plugin_api.py) -----------------------------------


def _api():
    pytest.importorskip("fastapi")
    spec = importlib.util.spec_from_file_location("tick_plugin_api", ROOT / "dashboard" / "plugin_api.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["tick_plugin_api"] = mod
    spec.loader.exec_module(mod)
    return mod


class MemState:
    def __init__(self):
        self.data = {}

    def get(self, k, d=None):
        return self.data.get(k, d)

    def set(self, k, v):
        self.data[k] = v


def test_backend_records_valid_device_zone():
    api = _api()
    st = MemState()
    out = api.record_device_zone(st, {"timezone": "America/Los_Angeles", "source": "desktop"}, T0)
    assert out == {"ok": True, "zone": "America/Los_Angeles"}
    assert st.data["client:device"] == {"tz": "America/Los_Angeles", "seen": T0, "source": "desktop"}


@pytest.mark.parametrize("body", [{}, {"timezone": ""}, {"timezone": "Not/AZone"},
                                  {"timezone": "x" * 80}, {"timezone": 5}, "nope"])
def test_backend_rejects_bad_bodies_without_writing(body):
    api = _api()
    st = MemState()
    out = api.record_device_zone(st, body, T0)
    assert out["ok"] is False and st.data == {}


def test_backend_routes_post_and_get():
    api = _api()
    fastapi = pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    st = MemState()
    api._state = lambda: st
    app = fastapi.FastAPI()
    app.include_router(api.router)
    c = TestClient(app)
    assert c.post("/client", json={"timezone": "Asia/Tokyo"}).json()["ok"] is True
    assert c.get("/client").json()["device"]["tz"] == "Asia/Tokyo"
    assert c.post("/client", json={"timezone": "bad"}).status_code == 400


def test_dashboard_manifest_declares_hidden_backend():
    m = json.loads((ROOT / "dashboard" / "manifest.json").read_text())
    assert m["name"] == "tick" and m["api"] == "plugin_api.py"
    assert m["tab"]["hidden"] is True
    assert (ROOT / "dashboard" / m["entry"]).is_file()


def test_backend_and_agent_share_the_state_key():
    api = _api()
    assert api.DEVICE_KEY == tick.DEVICE_KEY


# --- Review regressions (v0.2) ------------------------------------------------------


def test_malformed_override_does_not_hide_valid_device(env):
    ctx, _ = env()
    ctx.state.set("client:override", {"tz": "Asia/Tokyo", "set": T0, "until": "bad"})
    device(ctx, "America/Los_Angeles", T0 - HOUR)
    assert "PDT" in turn(ctx)
    assert call_tool(ctx, action="tz")["source"] == "device"


def test_override_matching_home_reports_override_source(env):
    ctx, _ = env()
    device(ctx, "America/Los_Angeles", T0 - HOUR)
    r = call_tool(ctx, action="tz", zone="America/New_York", days=5)
    assert r["source"] == "override"
    text = turn(ctx)
    assert "19:00 EDT (UTC-04:00)" in text and "home" not in text and "PDT" not in text


@pytest.mark.parametrize("days", ["Infinity", "inf", "nan", "1e308"])
def test_non_finite_days_rejected_without_writing(env, days):
    ctx, _ = env()
    assert call_tool(ctx, action="tz", zone="Asia/Tokyo", days=days)["ok"] is False
    assert "finite" in ctx.commands["tz"](f"Asia/Tokyo {days}")
    assert ctx.state.data.get("client:override") is None
    json.dumps(ctx.state.data, allow_nan=False)


def test_tz_tool_works_through_session_lock(env):
    ctx, _ = env()
    assert call_tool(ctx, action="tz", zone="Asia/Tokyo")["ok"]
    assert "JST" in turn(ctx)


def test_master_switch_disables_tz_command(env):
    """Review regression: enabled=false must not register /tz or write overrides."""
    ctx, _ = env({"enabled": False})
    assert "tz" not in ctx.commands
    out = tick.Tick(ctx).command("Asia/Tokyo")
    assert "off" in out
    assert ctx.state.data.get("client:override") is None

"""tick desktop backend: receives the client device's IANA time zone.

Mounted by Hermes at /api/plugins/tick/ when tick is in plugins.enabled. The
desktop half (desktop/plugin.js) posts the zone of the machine the app runs on.
The record lands in the same profile-scoped plugin state that the agent-side
hooks read (key DEVICE_KEY), so the next turn shows the user's local time.
"""
from __future__ import annotations

import time
from typing import Any, Dict

from fastapi import APIRouter, HTTPException

DEVICE_KEY = "client:device"
router = APIRouter()


def _valid_zone(name: Any) -> bool:
    if not isinstance(name, str) or not name or len(name) > 64:
        return False
    try:
        from zoneinfo import ZoneInfo

        ZoneInfo(name)
        return True
    except Exception:
        return False


def record_device_zone(state: Any, body: Any, now: float) -> Dict[str, Any]:
    """Validate and store one device report. Never writes on bad input."""
    if not isinstance(body, dict) or not _valid_zone(body.get("timezone")):
        return {"ok": False, "error": "timezone must be an IANA zone such as America/New_York"}
    source = str(body.get("source") or "desktop")[:32]
    state.set(DEVICE_KEY, {"tz": body["timezone"], "seen": now, "source": source})
    return {"ok": True, "zone": body["timezone"]}


def _state() -> Any:
    from hermes_cli.plugins_state import PluginState

    return PluginState("tick")


@router.post("/client")
async def post_client(body: Dict[str, Any]) -> Dict[str, Any]:
    out = record_device_zone(_state(), body, time.time())
    if not out["ok"]:
        raise HTTPException(status_code=400, detail=out["error"])
    return out


@router.get("/client")
async def get_client() -> Dict[str, Any]:
    st = _state()
    return {"ok": True, "device": st.get(DEVICE_KEY), "override": st.get("client:override")}

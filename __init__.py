"""tick: keep Hermes agents grounded in time.

Every turn, tick appends a short line to the current user message with the
local time, the UTC offset, the gap since the previous message and, when
configured, the model's training cutoff. After a stale gap or a compaction it
adds a reconcile block: what the agent observed before the gap, open
commitments and due checks, so the agent re-checks mutable facts before it
acts on them.

All context goes into the user message through ``pre_llm_call``. tick never
touches the system prompt or past history, so the prompt cache stays valid.
Every hook fails open.
"""
from __future__ import annotations

import contextlib
import fnmatch
import hashlib
import json
import logging
import math
import re
import threading
import time
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

TOOL_NAME = "tick"
KINDS = ("decision", "open", "check")
ACTIONS = ("add", "done", "list", "tz")
DEVICE_KEY = "client:device"      # written by the desktop companion (dashboard/plugin_api.py)
OVERRIDE_KEY = "client:override"  # written by /tz or the tick tool

DEFAULTS: Dict[str, Any] = {
    "enabled": True,
    "label": "tick",
    "timezone": "",
    "clock": True,
    "stale_after_minutes": 60,
    "recheck_hint": (
        "Earlier observations of mutable state (PRs, CI, deploys, external "
        "systems) may be stale. Re-check them before acting on them."
    ),
    "cutoffs": {},
    "ledger": True,
    "observe": ["terminal", "web_*", "browser_*", "mcp_*"],
    "ignore": [],
    "max_observations": 40,
    "max_items": 60,
    "reconcile_lines": 8,
    "session_ttl_days": 30,
    "client_timezone": True,
    "device_max_age_hours": 72,
}

_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_ARG_KEYS = ("command", "url", "urls", "query", "code", "path", "name", "question")


def _now() -> float:
    return time.time()


# --- settings -------------------------------------------------------------------


class Settings:
    """Typed view of ``plugins.entries.tick.settings`` with DEFAULTS as fallback."""

    def __init__(self, ctx: Any):
        self._ctx = ctx

    def __getattr__(self, key: str) -> Any:
        default = DEFAULTS[key]
        try:
            value = self._ctx.get_config(key, default)
        except Exception:
            return default
        if value is None:
            return default
        if isinstance(default, bool):
            return bool(value)
        if isinstance(default, int):
            try:
                return int(value)
            except (TypeError, ValueError):
                return default
        if isinstance(default, list):
            return [str(v) for v in value] if isinstance(value, list) else default
        if isinstance(default, dict):
            return dict(value) if isinstance(value, dict) else default
        return str(value)


# --- time formatting ------------------------------------------------------------


def _resolve_tz(name: str) -> tzinfo:
    """Plugin setting, else Hermes's ``timezone`` setting, else the host zone."""
    if name:
        try:
            from zoneinfo import ZoneInfo

            return ZoneInfo(name)
        except Exception:
            logger.debug("tick: unknown timezone %r; using UTC", name)
            return timezone.utc
    try:
        from hermes_time import get_timezone

        tz = get_timezone()
        if tz is not None:
            return tz
    except Exception:
        pass
    return datetime.now().astimezone().tzinfo or timezone.utc


def _zone(name: Any) -> Optional[tzinfo]:
    """An IANA zone, or None when the name is not one."""
    if not isinstance(name, str) or not name or len(name) > 64:
        return None
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception:
        return None


def _finite(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _same_zone(a: tzinfo, b: tzinfo, ts: float) -> bool:
    ka, kb = getattr(a, "key", None), getattr(b, "key", None)
    if ka and kb:
        return ka == kb
    da, db = datetime.fromtimestamp(ts, a), datetime.fromtimestamp(ts, b)
    return da.utcoffset() == db.utcoffset() and da.tzname() == db.tzname()


def _offset(dt: datetime) -> str:
    total = int((dt.utcoffset() or timedelta(0)).total_seconds() // 60)
    sign = "+" if total >= 0 else "-"
    total = abs(total)
    return f"UTC{sign}{total // 60:02d}:{total % 60:02d}"


def _stamp(ts: float, tz: tzinfo, source: str = "", home: Optional[tzinfo] = None) -> str:
    """Local time with zone and offset, then UTC. A non-home zone also shows home time."""
    dt = datetime.fromtimestamp(ts, tz)
    name = dt.tzname() or ""
    utc = datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
    if dt.utcoffset() == timedelta(0) and name in ("UTC", "GMT", "+00", "Z", "") and not source:
        return f"{dt:%a %Y-%m-%d %H:%M} UTC ({utc})"
    tag = f", {source}" if source else ""
    out = f"{dt:%a %Y-%m-%d %H:%M} {name} ({_offset(dt)}{tag})"
    if home is not None:
        h = datetime.fromtimestamp(ts, home)
        out += f" | home {h:%H:%M} {h.tzname()}"
    return f"{out} = {utc}"


def _ago(seconds: float) -> str:
    s = max(0, int(seconds))
    if s < 60:
        return f"{s}s"
    m = s // 60
    if m < 60:
        return f"{m}m"
    h, m = divmod(m, 60)
    if h < 24:
        return f"{h}h{m:02d}m"
    d, h = divmod(h, 24)
    return f"{d}d{h:02d}h"


def _when(then: float, now: float, tz: tzinfo) -> str:
    a = datetime.fromtimestamp(then, tz)
    days = (datetime.fromtimestamp(now, tz).date() - a.date()).days
    if days == 0:
        return f"{a:%H:%M}"
    if days == 1:
        return f"yesterday {a:%H:%M}"
    if days < 7:
        return f"{a:%a %H:%M}"
    return f"{a:%Y-%m-%d %H:%M}"


# --- state ----------------------------------------------------------------------


def _key(session_id: str) -> str:
    """State key for a session. Raw ids use ``s:``; ids that are not key-safe are hashed
    under the disjoint ``h:`` prefix, so no raw id can alias a hashed one."""
    sid = str(session_id or "default")
    candidate = f"s:{sid}"
    if _KEY_RE.fullmatch(candidate) and ".." not in candidate:
        return candidate
    return "h:" + hashlib.sha256(sid.encode("utf-8")).hexdigest()[:40]


def _empty() -> Dict[str, Any]:
    return {"last": None, "obs": [], "items": [], "compacted": False}


_THREAD_LOCKS: Dict[str, threading.Lock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()
_ID_LOCK = threading.Lock()  # always taken inside a session lock, never the reverse
ID_KEY = "ids:next"


class Ledger:
    """Per-session record in ``ctx.state`` (profile-scoped, survives restarts).

    ``ctx.state`` makes each get and set atomic, but not a load-modify-save
    sequence. ``update`` serializes that sequence per session: a thread lock
    for parallel hooks and tool calls in one process, plus an ``fcntl`` lock
    file in the plugin data dir for other processes on the same profile.
    """

    def __init__(self, ctx: Any, cfg: Settings):
        self.ctx = ctx
        self.cfg = cfg

    @contextlib.contextmanager
    def _locked(self, key: str):
        with _THREAD_LOCKS_GUARD:
            lock = _THREAD_LOCKS.setdefault(key, threading.Lock())
        with lock:
            fh = None
            try:
                import fcntl

                data_dir = getattr(self.ctx.state, "data_dir", None)
                if data_dir is not None:
                    data_dir.mkdir(parents=True, exist_ok=True)
                    fh = open(data_dir / "tick.lock", "a+")
                    fcntl.flock(fh, fcntl.LOCK_EX)
            except Exception as exc:  # no fcntl (Windows) or no data dir: thread lock only
                logger.debug("tick: process lock unavailable: %s", exc)
                if fh is not None:
                    fh.close()
                    fh = None
            try:
                yield
            finally:
                if fh is not None:
                    fh.close()  # closing releases the flock

    @contextlib.contextmanager
    def update(self, session_id: str):
        """Load, yield for mutation, save; all under the session lock."""
        with self._locked(_key(session_id)):
            data = self.load(session_id)
            yield data
            self.save(session_id, data)

    def load(self, session_id: str) -> Dict[str, Any]:
        data = self.ctx.state.get(_key(session_id))
        base = _empty()
        if isinstance(data, dict):
            base.update(data)
        return base

    def save(self, session_id: str, data: Dict[str, Any]) -> None:
        data["obs"] = data.get("obs", [])[-max(1, self.cfg.max_observations):]
        items = data.get("items", [])
        cap = max(1, self.cfg.max_items)
        if len(items) > cap:
            live = [i for i in items if not i.get("done")]
            done = [i for i in items if i.get("done")]
            room = max(0, cap - len(live))
            items = sorted(live + (done[-room:] if room else []), key=lambda i: i["id"])[-cap:]
        data["items"] = items
        data["touched"] = _now()
        self.ctx.state.set(_key(session_id), data)


def _history_last_ts(history: Any) -> Optional[float]:
    """Newest ``timestamp`` before the current turn, for sessions tick has not seen.

    Hermes passes ``conversation_history=list(messages)`` to ``pre_llm_call``
    (agent/turn_context.py, _collect_pre_llm_call_context), so the list already
    ends with the current user message. Its own timestamp is "now", so it is
    skipped; otherwise a resumed session reports a 0s gap.
    """
    if not isinstance(history, list):
        return None
    prior = history
    if prior and isinstance(prior[-1], dict) and prior[-1].get("role") == "user":
        prior = prior[:-1]
    for msg in reversed(prior):
        ts = msg.get("timestamp") if isinstance(msg, dict) else None
        if isinstance(ts, (int, float)) and not isinstance(ts, bool) and ts > 0:
            return float(ts)
        if isinstance(ts, str):
            try:
                return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
            except ValueError:
                continue
    return None


# --- observations ---------------------------------------------------------------


def _summarise_args(args: Any) -> str:
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            return " ".join(args.split())
    if not isinstance(args, dict):
        return ""
    for k in _ARG_KEYS:
        v = args.get(k)
        if isinstance(v, list):
            v = " ".join(str(x) for x in v[:3])
        if isinstance(v, str) and v.strip():
            if k == "code":  # browser/python steps: the leading comment names the step
                return v.strip().splitlines()[0].lstrip("# ").strip()
            return " ".join(v.split())
    return ""


def _matches(name: str, patterns: List[str]) -> bool:
    return any(fnmatch.fnmatchcase(name, p) for p in patterns)


def _inherit(child: Dict[str, Any], parent: Dict[str, Any]) -> None:
    """Merge the parent ledger into a child that may already hold entries.

    Item ids come from one profile-wide counter, so parent and child ids never
    collide and no id the agent has already seen is ever renumbered.
    Observations merge latest-wins per (tool, what).
    """
    seen = {i["id"] for i in child.get("items", [])}
    inherited = [i for i in parent.get("items", []) if i["id"] not in seen]
    child["items"] = sorted(inherited + list(child.get("items", [])), key=lambda i: i["id"])
    merged: Dict[Any, Dict[str, Any]] = {}
    for o in list(parent.get("obs", [])) + list(child.get("obs", [])):
        k = (o.get("tool"), o.get("what"))
        if k not in merged or o["t"] >= merged[k]["t"]:
            merged[k] = o
    child["obs"] = sorted(merged.values(), key=lambda o: o["t"])
    child["compacted"] = bool(child.get("compacted") or parent.get("compacted"))
    if child.get("last") is None:
        child["last"] = parent.get("last")


# --- the plugin -----------------------------------------------------------------


class Tick:
    def __init__(self, ctx: Any):
        self.cfg = Settings(ctx)
        self.ledger = Ledger(ctx, self.cfg)
        self._gc_at = 0.0

    # pre_llm_call ----------------------------------------------------------------

    def pre_llm_call(self, **kw: Any) -> Optional[Dict[str, str]]:
        try:
            return self._pre_llm_call(**kw)
        except Exception as exc:
            logger.debug("tick pre_llm_call failed open: %s", exc)
            return None

    def _pre_llm_call(self, session_id: str = "", model: str = "", parent_session_id: str = "",
                      conversation_history: Any = None, **_: Any) -> Optional[Dict[str, str]]:
        cfg = self.cfg
        if not cfg.enabled:
            return None
        try:
            with self.ledger.update(session_id) as data:
                return self._turn(data, model, parent_session_id, conversation_history)
        except Exception as exc:
            logger.debug("tick: state unavailable, clock only: %s", exc)
            return self._turn(_empty(), model, "", conversation_history)

    def _turn(self, data: Dict[str, Any], model: str, parent_session_id: str,
              conversation_history: Any) -> Optional[Dict[str, str]]:
        cfg = self.cfg
        now = _now()
        tz, source, home = self.client_zone(now)
        if data["last"] is None and parent_session_id and not data.get("inherited"):
            parent = self._safe_load(parent_session_id)
            if parent["last"] is not None or parent["items"] or parent["obs"]:
                _inherit(data, parent)  # compaction rotated the session id: carry the ledger forward
            data["inherited"] = True
        last = data["last"] if data["last"] is not None else _history_last_ts(conversation_history)
        gap = (now - last) if last is not None else None
        stale = gap is not None and gap >= cfg.stale_after_minutes * 60
        label = f"[{cfg.label}]"

        lines: List[str] = []
        if cfg.clock:
            head = f"{label} now {_stamp(now, tz, source, home)}"
            if gap is None:
                head += " | new session"
            else:
                head += f" | last message {_ago(gap)} ago ({_when(last, now, tz)})"
            cutoff = self._cutoff(model)
            if cutoff:
                head += (f" | {str(model).split('/')[-1]} training cutoff {cutoff}; "
                         "facts after it need a source")
            lines.append(head)
        if cfg.ledger:
            lines.extend(self._reconcile(data, now, stale, bool(data.get("compacted")), gap, label))
        elif stale:
            lines.append(f"{label} reconcile: stale gap ({_ago(gap)}). {cfg.recheck_hint}")

        data["last"] = now
        data["compacted"] = False
        self._gc()
        text = "\n".join(lines)
        return {"context": text} if text else None

    def _safe_load(self, session_id: str) -> Dict[str, Any]:
        try:
            return self.ledger.load(session_id)
        except Exception as exc:
            logger.debug("tick: state unreadable: %s", exc)
            return _empty()

    # client zone ---------------------------------------------------------------------

    def resolve_zone(self, now: float):
        """(zone, kind) with kind in override, device, home. Override > fresh device > home.

        Each candidate is read and validated on its own, so a malformed
        higher-priority record never hides a valid lower-priority one.
        """
        home = _resolve_tz(self.cfg.timezone)
        if not self.cfg.client_timezone:
            return home, "home"
        state = self.ledger.ctx.state
        for key, kind in ((OVERRIDE_KEY, "override"), (DEVICE_KEY, "device")):
            try:
                rec = state.get(key)
                if not isinstance(rec, dict):
                    continue
                if kind == "override":
                    until = rec.get("until")
                    if until is not None and not (_finite(until) and until > now):
                        continue
                else:
                    seen = rec.get("seen")
                    max_age = max(0, self.cfg.device_max_age_hours) * 3600
                    if not (_finite(seen) and now - seen <= max_age):
                        continue
                tz = _zone(rec.get("tz"))
                if tz is not None:
                    return tz, kind
            except Exception as exc:
                logger.debug("tick: %s record unreadable: %s", kind, exc)
        return home, "home"

    def client_zone(self, now: float):
        """Display view: (zone, source label, home zone to show or None)."""
        tz, kind = self.resolve_zone(now)
        home = _resolve_tz(self.cfg.timezone)
        if kind == "home" or _same_zone(tz, home, now):
            return tz, "", None
        return tz, {"override": "set by you", "device": "your device"}[kind], home

    def _zone_report(self, now: float) -> Dict[str, Any]:
        tz, kind = self.resolve_zone(now)
        name = getattr(tz, "key", None) or datetime.fromtimestamp(now, tz).tzname()
        return {"ok": True, "zone": name, "source": kind,
                "local": datetime.fromtimestamp(now, tz).strftime("%a %Y-%m-%d %H:%M %Z")}

    def set_zone(self, zone: str, days: Any = None) -> Dict[str, Any]:
        """Set or clear the user's zone override. zone='home' clears it."""
        state = self.ledger.ctx.state
        now = _now()
        if not zone:
            return self._zone_report(now)
        if zone.lower() in ("home", "clear", "reset"):
            state.set(OVERRIDE_KEY, None)
            return self._zone_report(now)
        if _zone(zone) is None:
            return {"ok": False, "error": f"{zone!r} is not an IANA zone (example: America/Los_Angeles)"}
        until = None
        if days not in (None, ""):
            try:
                d = float(days)
                until = now + max(0.0, d) * 86400
            except (TypeError, ValueError, OverflowError):
                return {"ok": False, "error": "days must be a finite number"}
            if not (_finite(d) and _finite(until)):
                return {"ok": False, "error": "days must be a finite number"}
        state.set(OVERRIDE_KEY, {"tz": zone, "set": now, "until": until})
        out = self._zone_report(now)
        out["until"] = until
        return out

    def _alloc_id(self, data: Dict[str, Any]) -> int:
        """Next profile-wide item id. Ids are unique across sessions, so inheritance
        after compaction never has to renumber an id the agent already saw.
        Called inside the session lock; the process lock file (when present)
        also serializes other processes."""
        state = self.ledger.ctx.state
        with _ID_LOCK:
            nxt = state.get(ID_KEY)
            nxt = nxt if isinstance(nxt, int) and not isinstance(nxt, bool) and nxt > 0 else 1
            legacy = max([i.get("id", 0) for i in data.get("items", [])] + [0]) + 1
            nxt = max(nxt, legacy)
            state.set(ID_KEY, nxt + 1)
            return nxt

    def command(self, raw: str = "") -> str:
        """/tz [Area/City [days] | home]"""
        if not (self.cfg.enabled and self.cfg.client_timezone):
            return "tick time zones are off (enabled or client_timezone is false)"
        try:
            parts = str(raw or "").split()
            if len(parts) > 1:
                try:
                    ok = _finite(float(parts[1]))
                except ValueError:
                    ok = False
                if not ok:
                    return "Usage: /tz [Area/City [days] | home] - days must be a finite number"
            r = self.set_zone(parts[0] if parts else "", parts[1] if len(parts) > 1 else None)
            if not r.get("ok"):
                return r["error"]
            line = f"Time zone: {r['zone']} ({r['source']}), now {r['local']}"
            if len(parts) > 1 and r.get("until"):
                line += f", for {parts[1]} day(s)"
            if r["source"] == "home" and parts and parts[0].lower() in ("home", "clear", "reset"):
                line += ". Override cleared; using home zone"
            return line
        except Exception as exc:
            return f"tick: time zone unavailable ({exc})"

    def _cutoff(self, model: str) -> str:
        cutoffs = self.cfg.cutoffs
        best = ""
        for needle in cutoffs:
            if needle and needle.lower() in str(model or "").lower() and len(needle) > len(best):
                best = needle
        return str(cutoffs[best]) if best else ""

    def _reconcile(self, data: Dict[str, Any], now: float, stale: bool, compacted: bool,
                   gap: Optional[float], label: str) -> List[str]:
        cfg = self.cfg
        n = max(1, cfg.reconcile_lines)
        due = [i for i in data["items"] if not i.get("done") and i["kind"] == "check"
               and i.get("due") is not None and i["due"] <= now]
        out: List[str] = []
        if stale or compacted:
            why = ([f"stale gap ({_ago(gap)})"] if stale else []) + \
                  (["context was compacted"] if compacted else [])
            out.append(f"{label} reconcile: {' and '.join(why)}. {cfg.recheck_hint}")
            obs = sorted(data["obs"], key=lambda o: o["t"], reverse=True)[:n]
            if obs:
                out.append("Observed before this point (newest first):")
                for o in obs:
                    bad = o.get("status") not in (None, "", "ok", "success")
                    out.append(f"- {_ago(now - o['t'])} ago: {o['tool']} {o['what']}"
                               + (f" -> {o['status']}" if bad else ""))
            due_ids = {i["id"] for i in due}
            live = [i for i in data["items"] if not i.get("done") and i["id"] not in due_ids]
            if live:
                out.append("Ledger (open):")
                for i in live[-n:]:
                    out.append(f"- #{i['id']} {i['kind']}: {i['text']} ({_ago(now - i['t'])} ago)")
        if due:
            out.append(f"{label} Due checks:")
            for i in due[:n]:
                out.append(f"- #{i['id']} {i['text']} (due {_ago(now - i['due'])} ago)")
        return out

    # post_tool_call --------------------------------------------------------------

    def post_tool_call(self, tool_name: str = "", args: Any = None, session_id: str = "",
                       status: Any = None, **_: Any) -> None:
        try:
            cfg = self.cfg
            if not (cfg.enabled and cfg.ledger) or not tool_name or tool_name == TOOL_NAME:
                return None
            if not _matches(tool_name, cfg.observe) or _matches(tool_name, cfg.ignore):
                return None
            what = _summarise_args(args)[:160]
            with self.ledger.update(session_id) as data:
                data["obs"] = [o for o in data["obs"]
                               if not (o["tool"] == tool_name and o["what"] == what)]
                data["obs"].append({"t": _now(), "tool": tool_name, "what": what,
                                    "status": str(status or "ok")})
        except Exception as exc:
            logger.debug("tick post_tool_call failed open: %s", exc)
        return None

    # pre_auxiliary_call ------------------------------------------------------------

    def pre_auxiliary_call(self, aux_task: str = "", session_id: str = "", **_: Any) -> None:
        try:
            if not self.cfg.enabled or not session_id:
                return None
            if "compress" not in str(aux_task or "").lower():
                return None
            with self.ledger.update(session_id) as data:
                data["compacted"] = True
        except Exception as exc:
            logger.debug("tick pre_auxiliary_call failed open: %s", exc)
        return None

    # tick tool ---------------------------------------------------------------------

    def tool(self, args: Dict[str, Any], **kw: Any) -> str:
        sid = str(kw.get("session_id") or kw.get("task_id") or "")
        if not (self.cfg.enabled and self.cfg.ledger):
            return json.dumps({"ok": False, "error": "tick is disabled (enabled or ledger is false)"})
        try:
            with self.ledger.update(sid) as data:
                return json.dumps(self._tool(args or {}, data))
        except Exception as exc:
            return json.dumps({"ok": False, "error": f"tick ledger unavailable: {exc}"})

    def _tool(self, args: Dict[str, Any], data: Dict[str, Any]) -> Dict[str, Any]:
        """Mutates ``data`` in place; the caller saves it under the session lock."""
        action = args.get("action")
        if action not in ACTIONS:
            return {"ok": False, "error": f"action must be one of {list(ACTIONS)}"}
        if action == "tz":
            if not self.cfg.client_timezone:
                return {"ok": False, "error": "client time zones are off (client_timezone: false)"}
            return self.set_zone(str(args.get("zone") or "").strip(), args.get("days"))
        if action == "list":
            return {"ok": True, "items": [i for i in data["items"] if not i.get("done")]}
        if action == "done":
            try:
                target = int(args.get("id"))
            except (TypeError, ValueError):
                return {"ok": False, "error": "done needs the numeric id from add or list"}
            for item in data["items"]:
                if item["id"] == target and not item.get("done"):
                    item["done"] = _now()
                    return {"ok": True, "id": target}
            return {"ok": False, "error": f"no open item #{target}"}
        kind = args.get("kind")
        text = " ".join(str(args.get("text") or "").split())[:300]
        if kind not in KINDS:
            return {"ok": False, "error": f"kind must be one of {list(KINDS)}"}
        if not text:
            return {"ok": False, "error": "text is required"}
        now = _now()
        item: Dict[str, Any] = {"id": 0, "kind": kind, "text": text, "t": now}
        after = args.get("after_minutes")
        if kind == "check" and after not in (None, ""):
            try:
                item["due"] = now + max(0.0, float(after)) * 60
            except (TypeError, ValueError):
                return {"ok": False, "error": "after_minutes must be a number"}
        item["id"] = self._alloc_id(data)
        data["items"].append(item)
        return {"ok": True, "id": item["id"]}

    # housekeeping ------------------------------------------------------------------

    def _gc(self) -> None:
        """Every 6 h, drop session records idle longer than session_ttl_days."""
        try:
            if _now() - self._gc_at < 6 * 3600:
                return
            self._gc_at = _now()
            state = self.ledger.ctx.state
            path = getattr(state, "path", None)
            if path is None:
                return
            with open(path, encoding="utf-8") as fh:
                keys = list(json.load(fh))
            cutoff = _now() - self.cfg.session_ttl_days * 86400
            for k in keys:
                rec = state.get(k) if k.startswith(("s:", "h:")) else None
                if isinstance(rec, dict) and rec.get("touched", _now()) < cutoff:
                    state.set(k, None)
        except Exception as exc:
            logger.debug("tick gc skipped: %s", exc)


TOOL_SCHEMA: Dict[str, Any] = {
    "name": TOOL_NAME,
    "description": (
        "Working ledger for this session. After a bigger move (a decision, a merge, a "
        "deploy, a hand-off), record it so it survives long gaps and compaction. `add` a "
        "decision, an open item, or a check (set after_minutes to be reminded when it is "
        "due); `done` closes an item by id; `list` shows open items. tick shows open items "
        "and due checks automatically after a long gap or a compaction. `tz` sets the "
        "user's current time zone when they say where they are (zone='home' clears it; "
        "days makes it expire); with no zone it reports the zone in use."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": list(ACTIONS)},
            "kind": {"type": "string", "enum": list(KINDS),
                     "description": "For add: decision, open (unresolved item) or check."},
            "text": {"type": "string", "description": "For add: one line, max 300 characters."},
            "after_minutes": {"type": "number",
                              "description": "For add with kind=check: minutes until due."},
            "id": {"type": "integer", "description": "For done: the item id."},
            "zone": {"type": "string",
                     "description": "For tz: IANA zone such as America/Los_Angeles, or home."},
            "days": {"type": "number", "description": "For tz: optional expiry in days."},
        },
        "required": ["action"],
    },
}


def register(ctx: Any) -> None:
    plugin = Tick(ctx)
    ctx.register_hook("pre_llm_call", plugin.pre_llm_call)
    ctx.register_hook("post_tool_call", plugin.post_tool_call)
    ctx.register_hook("pre_auxiliary_call", plugin.pre_auxiliary_call)
    if Settings(ctx).enabled and Settings(ctx).ledger:
        ctx.register_tool(
            name=TOOL_NAME, toolset="tick", schema=TOOL_SCHEMA, handler=plugin.tool,
            description=TOOL_SCHEMA["description"], emoji="✓",
        )
    if Settings(ctx).enabled and Settings(ctx).client_timezone and hasattr(ctx, "register_command"):
        ctx.register_command(
            "tz", plugin.command, description="Show or set your current time zone for tick",
            args_hint="[Area/City [days] | home]",
        )

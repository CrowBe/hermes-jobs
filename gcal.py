"""Google Calendar v3 over Hermes plugin oauth only (credentialed_provider_request("google", ...)).

Touches exactly one calendar (the bound one). Scope is calendar.events, so no calendar listing or
creation here: the user binds an existing calendar id ("primary" or a secondary calendar's id).
Never reads google_token.json, .env, or any other token store.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
from urllib.parse import quote, urlencode

API = "https://www.googleapis.com/calendar/v3"
TAG = "hermes_job_id"
MINUTES = 15


class CalendarError(RuntimeError):
    pass


class Conflict(CalendarError):
    """409: an event with this client-chosen id already exists (possibly cancelled)."""


class Gone(CalendarError):
    """410 on a read: an incremental-sync token expired; the caller resyncs from scratch."""


def _req(method: str, url: str, body: Optional[dict] = None) -> Any:
    from hermes_cli.plugin_provider_requests import credentialed_provider_request
    resp = credentialed_provider_request("google", method, url, json=body, timeout=20)
    if resp.status == 410:
        if method == "DELETE":
            return None  # already gone
        raise Gone(f"{method} {url.split('?')[0]} -> 410")
    if resp.status == 409:
        raise Conflict(f"{method} {url.split('?')[0]} -> 409")
    if resp.status >= 300:
        raise CalendarError(f"{method} {url.split('?')[0]} -> {resp.status}: {_error_message(resp)}")
    return resp.json() if resp.body else None


class Calendar:
    def __init__(self, calendar_id: str) -> None:
        self.base = f"{API}/calendars/{quote(calendar_id, safe='')}/events"

    def list_tagged(self) -> List[Dict[str, Any]]:
        """Masters, singles and exceptions (singleEvents=false, showDeleted=true).

        Untagged items are kept ONLY as possible "this and following" splits of our own series
        (Google names them <master id>_R<date> and may not copy our tags); the planner ties them to
        a job strictly by that id prefix and otherwise ignores them. Cancelled items are kept so
        deleted masters and deleted single instances are visible.
        """
        out, token = [], None
        while True:
            q: Dict[str, Any] = {"maxResults": 250, "singleEvents": "false", "showDeleted": "true"}
            if token:
                q["pageToken"] = token
            data = _req("GET", f"{self.base}?{urlencode(q)}") or {}
            out += [e for e in map(from_google, data.get("items", []))
                    if e["hermes_job_id"] or e["recurring_event_id"] or "_R" in (e["id"] or "")]
            token = data.get("nextPageToken")
            if not token:
                return out

    def exceptions(self, master_id: str, time_min: str, time_max: str) -> List[Dict[str, Any]]:
        """Moved or cancelled instances of one master in a window (authoritative per-master view)."""
        out, token = [], None
        while True:
            q: Dict[str, Any] = {"maxResults": 250, "showDeleted": "true", "timeMin": time_min, "timeMax": time_max}
            if token:
                q["pageToken"] = token
            data = _req("GET", f"{self.base}/{quote(master_id, safe='')}/instances?{urlencode(q)}") or {}
            for e in map(from_google, data.get("items", [])):
                e["recurring_event_id"] = e["recurring_event_id"] or master_id
                if e["status"] == "cancelled" or (e["original_start"] and e["start"]
                                                  and _same(e["start"], e["original_start"]) is False):
                    out.append(e)
            token = data.get("nextPageToken")
            if not token:
                return out

    def changes(self, token: Optional[str]) -> tuple:
        """Google incremental sync: (raw changed items, next sync token). ``token=None`` pages the
        calendar once for a baseline token and reports no changes. Raises Gone on 410."""
        out, page = [], None
        while True:
            q: Dict[str, Any] = {"maxResults": 250, "singleEvents": "false"}
            q.update({"syncToken": token} if token else {"showDeleted": "true"})
            if page:
                q["pageToken"] = page
            data = _req("GET", f"{self.base}?{urlencode(q)}") or {}
            if token:
                out += [g for g in data.get("items", []) if isinstance(g, dict)]
            page = data.get("nextPageToken")
            if not page:
                if not data.get("nextSyncToken"):
                    raise CalendarError("events.list returned no nextSyncToken")
                return out, data["nextSyncToken"]

    def get_raw(self, path: str = "", **query: Any) -> Any:
        """Read-only GET of raw Google JSON under this calendar's events (fixture capture)."""
        q = f"?{urlencode(query)}" if query else ""
        return _req("GET", f"{self.base}{path}{q}")

    def insert(self, ev: Dict[str, Any]) -> Dict[str, Any]:
        """Insert with the derived id when given. 409 = already exists (an earlier attempt landed,
        or the id was used by an event since deleted): adopt it by patching it to the desired body
        (status confirmed revives a cancelled one). Refuse if it belongs to another job."""
        try:
            return from_google(_req("POST", self.base, to_google(ev)))
        except Conflict:
            if not ev.get("id"):
                raise
            url = f"{self.base}/{quote(ev['id'], safe='')}"
            existing = _req("GET", url) or {}
            tag = ((existing.get("extendedProperties") or {}).get("private") or {}).get(TAG)
            if tag and tag != ev.get("hermes_job_id"):
                raise CalendarError(f"event id {ev['id']} already belongs to job {tag}; not adopting")
            body = {k: v for k, v in to_google(ev).items() if k != "id"}
            body["status"] = "confirmed"
            return from_google(_req("PATCH", url, body))

    def patch(self, event_id: str, ev: Dict[str, Any]) -> Dict[str, Any]:
        return from_google(_req("PATCH", f"{self.base}/{quote(event_id, safe='')}", to_google(ev)))

    def delete(self, event_id: str) -> None:
        _req("DELETE", f"{self.base}/{quote(event_id, safe='')}")


def _error_message(resp) -> str:
    """Only Google's error message (never headers or the raw body), capped and redacted."""
    try:
        msg = str(((resp.json() or {}).get("error") or {}).get("message") or "")
    except Exception:  # noqa: BLE001
        msg = ""
    return redact(msg)[:200] or "no error message"


_SECRET_PATTERNS = None


def redact(text: str) -> str:
    """Belt and braces for anything printed: mask bearer tokens and Google token shapes."""
    global _SECRET_PATTERNS
    import re
    if _SECRET_PATTERNS is None:
        _SECRET_PATTERNS = [re.compile(p) for p in (
            r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+", r"ya29\.[A-Za-z0-9._-]+", r"1//[A-Za-z0-9._-]{10,}",
            r"GOCSPX-[A-Za-z0-9_-]+",
            r"(?i)(access_token|refresh_token|id_token|client_secret|authorization)([\"']?\s*[:=]\s*[\"']?)[^\s\"',}]+")]
    for pat in _SECRET_PATTERNS:
        text = pat.sub(lambda m: (m.group(1) + m.group(2) if m.lastindex and m.lastindex >= 2 else "") + "[redacted]",
                       text)
    return text


def instant(value: Any) -> Optional[str]:
    """Calendar timestamps are untrusted: only a timezone-aware ISO-8601 instant survives,
    re-serialised canonically. Anything else (all-day dates, junk, oversized) reads as None."""
    if not isinstance(value, str) or len(value) > 40:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.isoformat() if dt.tzinfo else None


def _zone(value: Any) -> Optional[str]:
    if not isinstance(value, str) or not value or len(value) > 64:
        return None
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo(value)
        return value
    except Exception:  # noqa: BLE001
        return "invalid"  # never equals a job zone: series-zone changes are refused


def _text(value: Any, cap: int) -> Optional[str]:
    return value[:cap] if isinstance(value, str) else None


def _same(a: str, b: str) -> bool:
    return datetime.fromisoformat(a.replace("Z", "+00:00")) == datetime.fromisoformat(b.replace("Z", "+00:00"))


def to_google(ev: Dict[str, Any]) -> Dict[str, Any]:
    g: Dict[str, Any] = {k: ev[k] for k in ("summary", "description") if k in ev}
    if ev.get("id"):
        g["id"] = ev["id"]
    if ev.get("start"):
        start = datetime.fromisoformat(ev["start"])
        g["start"] = {"dateTime": start.isoformat()}
        g["end"] = {"dateTime": (start + timedelta(minutes=MINUTES)).isoformat()}
        if ev.get("tz"):  # recurring events need an IANA zone so the RRULE expands on its wall clock
            g["start"]["timeZone"] = g["end"]["timeZone"] = ev["tz"]
    if ev.get("color_id"):
        g["colorId"] = ev["color_id"]
    if "recurrence" in ev:
        g["recurrence"] = list(ev["recurrence"] or [])  # [] turns a master back into a single event
    if ev.get("hermes_job_id"):
        # Always write the FULL private set so PATCH can never strip the job tag.
        g["extendedProperties"] = {"private": {**(ev.get("props") or {}), TAG: ev["hermes_job_id"]}}
    return g


def from_google(g: Dict[str, Any]) -> Dict[str, Any]:
    private = (g.get("extendedProperties") or {}).get("private") or {}
    common = {"status": g.get("status") or "confirmed", "recurring_event_id": g.get("recurringEventId"),
              "original_start": instant((g.get("originalStartTime") or {}).get("dateTime")),
              "recurrence": [r for r in (g.get("recurrence") or []) if isinstance(r, str) and len(r) <= 512][:16]
              or None, "tz": _zone((g.get("start") or {}).get("timeZone"))}
    if g.get("status") == "cancelled":
        # Cancelled masters/singles read as gone; cancelled instances keep their identity.
        return {"id": g.get("id"), "hermes_job_id": None, "props": {}, "summary": None,
                "description": None, "start": None, **common}
    return {
        **common,
        "id": g.get("id"),
        "props": {k: v for k, v in private.items() if k != TAG},
        "summary": _text(g.get("summary"), 1024),
        "description": _text(g.get("description"), 8192),  # compared only, never interpreted
        "color_id": _text(g.get("colorId"), 8),
        "start": instant((g.get("start") or {}).get("dateTime")),
        "hermes_job_id": ((g.get("extendedProperties") or {}).get("private") or {}).get(TAG),
    }


FIXTURE_KEYS = {"kind", "status", "summary", "description", "start", "end", "recurrence", "recurringEventId",
                "originalStartTime", "extendedProperties", "eventType", "id", "sequence"}


def sanitise(g: Dict[str, Any], replace: Dict[str, str]) -> Dict[str, Any]:
    """Fixture form of a raw Google event: allow-listed keys only (no organizer/creator emails,
    htmlLink, iCalUID, etag, calendar id), with real ids swapped for placeholders."""
    text = json.dumps({k: v for k, v in g.items() if k in FIXTURE_KEYS}, sort_keys=True)
    for real, fake in sorted(replace.items(), key=lambda kv: -len(kv[0])):
        text = text.replace(real, fake)
    return json.loads(text)

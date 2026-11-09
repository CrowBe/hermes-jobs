"""Google Calendar v3 over Hermes plugin oauth only (credentialed_provider_request("google", ...)).

Touches exactly one calendar (the bound one). Scope is calendar.events, so no calendar listing or
creation here: the user binds an existing calendar id ("primary" or a secondary calendar's id).
Never reads google_token.json, .env, or any other token store.
"""
from __future__ import annotations

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


def _req(method: str, url: str, body: Optional[dict] = None) -> Any:
    from hermes_cli.plugin_provider_requests import credentialed_provider_request
    resp = credentialed_provider_request("google", method, url, json=body, timeout=20)
    if resp.status == 410 and method == "DELETE":
        return None  # already gone
    if resp.status == 409:
        raise Conflict(f"{method} {url.split('?')[0]} -> 409")
    if resp.status >= 300:
        raise CalendarError(f"{method} {url.split('?')[0]} -> {resp.status}: {resp.text[:200]}")
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
    if "recurrence" in ev:
        g["recurrence"] = list(ev["recurrence"] or [])  # [] turns a master back into a single event
    if ev.get("hermes_job_id"):
        # Always write the FULL private set so PATCH can never strip the job tag.
        g["extendedProperties"] = {"private": {**(ev.get("props") or {}), TAG: ev["hermes_job_id"]}}
    return g


def from_google(g: Dict[str, Any]) -> Dict[str, Any]:
    private = (g.get("extendedProperties") or {}).get("private") or {}
    common = {"status": g.get("status") or "confirmed", "recurring_event_id": g.get("recurringEventId"),
              "original_start": (g.get("originalStartTime") or {}).get("dateTime"),
              "recurrence": g.get("recurrence"), "tz": (g.get("start") or {}).get("timeZone")}
    if g.get("status") == "cancelled":
        # Cancelled masters/singles read as gone; cancelled instances keep their identity.
        return {"id": g.get("id"), "hermes_job_id": None, "props": {}, "summary": None,
                "description": None, "start": None, **common}
    return {
        **common,
        "id": g.get("id"),
        "props": {k: v for k, v in private.items() if k != TAG},
        "summary": g.get("summary"),
        "description": g.get("description"),
        "start": (g.get("start") or {}).get("dateTime"),
        "hermes_job_id": ((g.get("extendedProperties") or {}).get("private") or {}).get(TAG),
    }

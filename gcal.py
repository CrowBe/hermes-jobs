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


def _req(method: str, url: str, body: Optional[dict] = None) -> Any:
    from hermes_cli.plugin_provider_requests import credentialed_provider_request
    resp = credentialed_provider_request("google", method, url, json=body, timeout=20)
    if resp.status == 410 and method == "DELETE":
        return None  # already gone
    if resp.status >= 300:
        raise CalendarError(f"{method} {url.split('?')[0]} -> {resp.status}: {resp.text[:200]}")
    return resp.json() if resp.body else None


class Calendar:
    def __init__(self, calendar_id: str) -> None:
        self.base = f"{API}/calendars/{quote(calendar_id, safe='')}/events"

    def list_tagged(self) -> List[Dict[str, Any]]:
        """Events carrying our private tag (filtered client-side; untagged events are dropped)."""
        out, token = [], None
        while True:
            q: Dict[str, Any] = {"maxResults": 250}
            if token:
                q["pageToken"] = token
            data = _req("GET", f"{self.base}?{urlencode(q)}") or {}
            out += [e for e in map(from_google, data.get("items", [])) if e["hermes_job_id"]]
            token = data.get("nextPageToken")
            if not token:
                return out

    def insert(self, ev: Dict[str, Any]) -> Dict[str, Any]:
        return from_google(_req("POST", self.base, to_google(ev)))

    def patch(self, event_id: str, ev: Dict[str, Any]) -> Dict[str, Any]:
        return from_google(_req("PATCH", f"{self.base}/{quote(event_id, safe='')}", to_google(ev)))

    def delete(self, event_id: str) -> None:
        _req("DELETE", f"{self.base}/{quote(event_id, safe='')}")


def to_google(ev: Dict[str, Any]) -> Dict[str, Any]:
    g: Dict[str, Any] = {k: ev[k] for k in ("summary", "description") if k in ev}
    if ev.get("start"):
        start = datetime.fromisoformat(ev["start"])
        g["start"] = {"dateTime": start.isoformat()}
        g["end"] = {"dateTime": (start + timedelta(minutes=MINUTES)).isoformat()}
    if ev.get("hermes_job_id"):
        g["extendedProperties"] = {"private": {TAG: ev["hermes_job_id"]}}
    return g


def from_google(g: Dict[str, Any]) -> Dict[str, Any]:
    if g.get("status") == "cancelled":
        return {"id": g.get("id"), "hermes_job_id": None}
    return {
        "id": g.get("id"),
        "summary": g.get("summary"),
        "description": g.get("description"),
        "start": (g.get("start") or {}).get("dateTime"),
        "hermes_job_id": ((g.get("extendedProperties") or {}).get("private") or {}).get(TAG),
    }

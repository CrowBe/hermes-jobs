"""hermes-jobs MCP server: one MCP Events type, ``calendar.event_changed``, webhook delivery only.

Server side of the experimental ``events/*`` extension (modelcontextprotocol/experimental-ext-triggers-events,
docs/design-sketch-proposal.md) as Hermes' client (tools/mcp_tool_events.py) speaks it: ``events/list``,
``events/subscribe`` (which is also the refresh), ``events/unsubscribe``; Standard Webhooks signatures with
the client's ``whsec_`` secret; a signed verification challenge before the first delivery to a URL;
``eventId`` for dedup; the Google sync token as the replay ``cursor``.

Hermes runs it as a stdio server (``hermes hermes-jobs mcp-serve``). Only while a subscription is live,
it polls the bound calendar with Google incremental sync and POSTs one signed occurrence per batch of
changes. The payload is ids, etags, status and change times only, never titles or descriptions, so
nothing written in Calendar can reach a prompt. It delivers only under this Hermes gateway's own
webhook origin (``platforms.webhook.extra.public_url``/webhooks/) and never follows redirects.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import re
import secrets
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import gcal

log = logging.getLogger("hermes_jobs.events")

EVENT = "calendar.event_changed"
NOT_FOUND, UNSUPPORTED, INVALID, CALLBACK = -32023, -32026, -32602, -32027
TTL_MAX_MS, TTL_MIN_MS = 24 * 3600 * 1000, 60 * 1000
POLL_S, POLL_MIN_S = 60, 30
RETRY_S = (0, 2, 10, 30)  # 4 attempts in ~42s, then abandoned: the next change triggers a full sync anyway
MAX_CHANGES = 50
PRINCIPAL = "local-stdio"  # the only caller is the Hermes process that spawned this server over stdio

DESCRIPTOR = {
    "name": EVENT,
    "description": ("A Hermes job event on the bound Google Calendar was created, edited, moved or deleted. "
                    "The payload is ids, etags and change times only. Use it to trigger "
                    "`hermes hermes-jobs sync --auto`."),
    "delivery": ["webhook"],
    "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    "payloadSchema": {"type": "object", "properties": {
        "changes": {"type": "array", "items": {"type": "object", "properties": {
            "eventId": {"type": "string"}, "etag": {"type": "string"},
            "status": {"type": "string", "enum": ["confirmed", "tentative", "cancelled"]},
            "changedAt": {"type": "string", "format": "date-time"}}}},
        "more": {"type": "integer"}, "resync": {"type": "boolean"}}},
}

_ID = re.compile(r"[A-Za-z0-9_]{1,256}")
_ETAG = re.compile(r'"[A-Za-z0-9_-]{1,64}"')
_ROUTE = re.compile(r"[A-Za-z0-9_-]{1,128}")
_STATUS = {"confirmed", "tentative", "cancelled"}


class EventsError(Exception):
    """A JSON-RPC error from the sketch's table (code, message, data)."""

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code, self.message, self.data = code, message, data


def change(g: Any) -> Optional[Dict[str, str]]:
    """One changed Google item reduced to id/etag/status/time, or None when it is not ours or malformed.
    Ours = tagged, an instance or "this and following" split of a series, or a deletion (Google strips
    deleted items to their id). No free text is ever copied."""
    if not isinstance(g, dict) or not isinstance(g.get("id"), str) or not _ID.fullmatch(g["id"]):
        return None
    tagged = ((g.get("extendedProperties") or {}).get("private") or {}).get(gcal.TAG)
    status = g.get("status") if g.get("status") in _STATUS else "confirmed"
    if not (tagged or g.get("recurringEventId") or "_R" in g["id"] or status == "cancelled"):
        return None
    out = {"eventId": g["id"], "status": status}
    if isinstance(g.get("etag"), str) and _ETAG.fullmatch(g["etag"]):
        out["etag"] = g["etag"]
    if gcal.instant(g.get("updated")):
        out["changedAt"] = gcal.instant(g["updated"])
    return out


def secret_key(secret: Any) -> Optional[bytes]:
    """Standard Webhooks symmetric secret: ``whsec_`` + base64 of 24-64 bytes (else None)."""
    if not isinstance(secret, str) or not secret.startswith("whsec_"):
        return None
    try:
        key = base64.b64decode(secret[len("whsec_"):], validate=True)
    except ValueError:
        return None
    return key if 24 <= len(key) <= 64 else None


def sign(key: bytes, msg_id: str, ts: str, body: bytes) -> str:
    mac = hmac.new(key, f"{msg_id}.{ts}.".encode() + body, hashlib.sha256).digest()
    return "v1," + base64.b64encode(mac).decode()


def _iso(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).isoformat(timespec="seconds")


def http_post(url: str, body: bytes, headers: Dict[str, str], timeout: float = 5.0) -> Tuple[int, bytes]:
    """POST without following redirects (a 3xx is a failed delivery). Network errors raise."""
    import urllib.error
    import urllib.request

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):  # noqa: ANN002, ANN003
            return None
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.build_opener(_NoRedirect).open(req, timeout=timeout) as resp:
            return resp.status, resp.read(4096)
    except urllib.error.HTTPError as exc:
        exc.close()
        return exc.code, b""


def callback_base() -> Optional[str]:
    """The https origin Hermes' events client builds callback URLs from (same config key)."""
    from hermes_cli.config import load_config_readonly
    hook = (load_config_readonly().get("platforms") or {}).get("webhook") or {}
    url = (hook.get("extra") or {}).get("public_url") or hook.get("public_url") or ""
    url = url.strip().rstrip("/") if isinstance(url, str) else ""
    return url if url.startswith("https://") else None


class Hub:
    """Subscriptions + the poller. Blocking I/O; the MCP layer calls it from worker threads."""

    def __init__(self, changes: Optional[Callable[[Optional[str]], Tuple[List[Any], str]]],
                 base: Callable[[], Optional[str]] = callback_base, post=http_post,
                 now: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep) -> None:
        self._changes, self._base, self._post, self._now, self._sleep = changes, base, post, now, sleep
        self.subs: Dict[str, Dict[str, Any]] = {}
        self.verified: set = set()
        self.token: Optional[str] = None
        self._lock, self._poll = threading.Lock(), threading.Lock()

    @staticmethod
    def sub_id(url: Any, name: Any, arguments: Any) -> str:
        key = json.dumps([PRINCIPAL, url, name, arguments or {}], sort_keys=True, separators=(",", ":"))
        return "sub_" + hashlib.sha256(key.encode()).hexdigest()[:16]

    def list_events(self) -> Dict[str, Any]:
        return {"events": [DESCRIPTOR]}

    def subscribe(self, name: Any, arguments: Any, delivery: Any, cursor: Optional[str] = None,
                  ttl_ms: Optional[int] = TTL_MAX_MS) -> Dict[str, Any]:
        delivery = delivery if isinstance(delivery, dict) else {}
        url = delivery.get("url")
        self._check(name, arguments, url)
        if delivery.get("mode", "webhook") != "webhook":
            raise EventsError(UNSUPPORTED, "only webhook delivery is offered",
                              {"feature": "deliveryMode", "value": delivery.get("mode")})
        key = secret_key(delivery.get("secret"))
        if key is None:
            raise EventsError(INVALID, "delivery.secret must be whsec_ + base64 of 24-64 random bytes")
        if self._changes is None:
            raise EventsError(UNSUPPORTED, "no calendar bound (hermes hermes-jobs bind <calendar>)",
                              {"feature": "calendar"})
        sid = self.sub_id(url, name, arguments)
        if url not in self.verified:
            self._verify(url, key, sid)
            self.verified.add(url)
        with self._poll:
            if self.token is None:  # baseline now, so changes right after subscribing are not missed
                _, self.token = self._changes(None)
        ttl = TTL_MAX_MS if ttl_ms is None else min(max(int(ttl_ms), TTL_MIN_MS), TTL_MAX_MS)
        now = self._now()
        with self._lock:
            old = self.subs.get(sid)
            fresh = old is None or old["expires"] <= now
            replay = (cursor if cursor and cursor != self.token else None) if fresh else old.get("replay_from")
            self.subs[sid] = {"id": sid, "url": url, "key": key, "expires": now + ttl / 1000, "replay_from": replay}
        return {"id": sid, "refreshBefore": _iso(now + ttl / 1000), "cursor": self.token, "truncated": False}

    def unsubscribe(self, name: Any, arguments: Any, delivery: Any) -> Dict[str, Any]:
        url = delivery.get("url") if isinstance(delivery, dict) else None
        with self._lock:
            if self.subs.pop(self.sub_id(url, name, arguments), None) is None:
                raise EventsError(NOT_FOUND, "no such subscription", {"kind": "subscription"})
        return {}

    def _check(self, name: Any, arguments: Any, url: Any) -> None:
        if name != EVENT:
            raise EventsError(NOT_FOUND, f"unknown event type {str(name)[:64]!r}", {"kind": "event"})
        if arguments not in (None, {}):
            raise EventsError(INVALID, f"{EVENT} takes no arguments")
        if not isinstance(url, str) or not url.startswith("https://"):
            raise EventsError(INVALID, "delivery.url must be https")
        base = self._base()
        prefix = f"{base}/webhooks/" if base else None
        if not prefix or not url.startswith(prefix) or not _ROUTE.fullmatch(url[len(prefix):]):
            raise EventsError(INVALID, "delivery.url must be a route on this Hermes gateway "
                                       "(platforms.webhook.extra.public_url + /webhooks/<route>)")

    def _send(self, url: str, key: bytes, msg_id: str, payload: Dict[str, Any], sid: str) -> Tuple[int, bytes]:
        body = json.dumps(payload, separators=(",", ":")).encode()
        ts = str(int(self._now()))
        return self._post(url, body, {"Content-Type": "application/json", "webhook-id": msg_id,
                                      "webhook-timestamp": ts, "webhook-signature": sign(key, msg_id, ts, body),
                                      "X-MCP-Subscription-Id": sid})

    def _verify(self, url: str, key: bytes, sid: str) -> None:
        challenge = secrets.token_urlsafe(24)
        try:
            status, body = self._send(url, key, f"msg_verification_{secrets.token_hex(8)}",
                                      {"type": "verification", "challenge": challenge}, sid)
        except Exception as exc:  # noqa: BLE001 -- category only, never the endpoint's response
            reason = "timeout" if isinstance(exc, TimeoutError) else "connection_refused"
            raise EventsError(CALLBACK, "callback endpoint unreachable", {"reason": reason}) from None
        try:
            echoed = json.loads(body or b"{}").get("challenge")
        except (ValueError, AttributeError):
            echoed = None
        if not (200 <= status < 300 and isinstance(echoed, str) and hmac.compare_digest(echoed, challenge)):
            reason = "http_5xx" if status >= 500 else "http_4xx" if status >= 400 else "challenge_failed"
            raise EventsError(CALLBACK, "callback endpoint failed verification", {"reason": reason})

    def _deliver(self, sub: Dict[str, Any], occurrence: Dict[str, Any]) -> bool:
        """Retry with back-off; every attempt is re-stamped and re-signed. 410/413 are final."""
        for delay in RETRY_S:
            if delay:
                self._sleep(delay)
            try:
                status, _ = self._send(sub["url"], sub["key"], occurrence["eventId"], occurrence, sub["id"])
            except Exception:  # noqa: BLE001
                status = 0
            if 200 <= status < 300:
                return True
            if status in (410, 413):
                return False
        log.warning("hermes-jobs events: delivery %s abandoned", occurrence["eventId"])
        return False

    def _occurrence(self, cursor: str, changes: List[Dict[str, str]], resync: bool) -> Dict[str, Any]:
        """eventId hashes the changes themselves, so the same upstream changes seen by two server
        processes (the gateway's and a chat session's) dedupe at the receiver."""
        basis = json.dumps(["resync", cursor] if resync else sorted(changes, key=lambda c: c["eventId"]),
                           sort_keys=True)
        data: Dict[str, Any] = {"changes": changes[:MAX_CHANGES]}
        if len(changes) > MAX_CHANGES:
            data["more"] = len(changes) - MAX_CHANGES
        if resync:
            data["resync"] = True
        return {"eventId": "evt_" + hashlib.sha256(basis.encode()).hexdigest()[:32], "name": EVENT,
                "timestamp": _iso(self._now()), "data": data, "cursor": cursor}

    def _read(self, token: Optional[str]) -> Tuple[List[Dict[str, str]], str, bool]:
        """(our changes, next token, resync?) from one incremental read; an expired or unreadable
        token (410) falls back to a fresh baseline, reported as a resync."""
        try:
            items, nxt = self._changes(token)
            return [c for c in map(change, items) if c], nxt, False
        except gcal.Gone:
            return [], self._changes(None)[1], True

    def tick(self) -> int:
        """One poll: expire, replay for resubscribed cursors, incremental read, deliver. Returns how
        many deliveries were accepted. With no live subscription it makes no Google call at all."""
        with self._poll:
            now = self._now()
            with self._lock:
                for sid in [s for s, v in self.subs.items() if v["expires"] <= now]:
                    del self.subs[sid]
                subs = list(self.subs.values())
            if not subs or self._changes is None:
                return 0
            sent = 0
            for sub in subs:
                frm = sub.pop("replay_from", None)
                if frm:
                    try:
                        found, _, resync = self._read(frm)
                    except gcal.CalendarError:  # a cursor Google no longer reads: treat as a gap
                        found, resync = [], True
                    if found or resync:
                        sent += self._deliver(sub, self._occurrence(self.token, found, resync))
            found, self.token, resync = self._read(self.token)
            if found or resync:
                occurrence = self._occurrence(self.token, found, resync)
                sent += sum(self._deliver(sub, occurrence) for sub in subs)
            return sent


def build_server(hub: Hub):
    """The MCP server: three events/* methods, no tools, no resources, no prompts."""
    import anyio
    from mcp import types
    from mcp.server.lowlevel import Server
    from mcp.shared.exceptions import MCPError

    class ListParams(types.RequestParams):
        cursor: Optional[str] = None

    class SubscribeParams(types.RequestParams):
        name: str
        arguments: Optional[Dict[str, Any]] = None
        delivery: Dict[str, Any] = {}
        cursor: Optional[str] = None
        ttl_ms: Optional[int] = TTL_MAX_MS
        max_age_ms: Optional[int] = None  # accepted; replay is bounded by Google's own token lifetime

    class UnsubscribeParams(types.RequestParams):
        name: str
        arguments: Optional[Dict[str, Any]] = None
        delivery: Dict[str, Any] = {}

    async def call(fn, *args):
        try:
            return await anyio.to_thread.run_sync(fn, *args)
        except EventsError as exc:
            raise MCPError(exc.code, exc.message, exc.data) from None

    async def on_list(ctx, params):  # noqa: ANN001
        return hub.list_events()

    async def on_subscribe(ctx, p):  # noqa: ANN001
        return await call(hub.subscribe, p.name, p.arguments, p.delivery, p.cursor, p.ttl_ms)

    async def on_unsubscribe(ctx, p):  # noqa: ANN001
        return await call(hub.unsubscribe, p.name, p.arguments, p.delivery)

    server = Server("hermes-jobs", version="0.4.0",
                    instructions=f"Subscribe to {EVENT} (webhook delivery) to trigger hermes-jobs sync --auto.")
    server.add_request_handler("events/list", ListParams, on_list)
    server.add_request_handler("events/subscribe", SubscribeParams, on_subscribe)
    server.add_request_handler("events/unsubscribe", UnsubscribeParams, on_unsubscribe)
    return server


def init_options(server):
    return server.create_initialization_options(experimental_capabilities={"events": {"listChanged": False}})


def serve(calendar_id: Optional[str], interval: int = POLL_S) -> None:
    """Run over stdio until the client closes it (Hermes owns the process lifetime)."""
    import anyio
    from mcp.server.stdio import stdio_server
    cal = gcal.Calendar(calendar_id) if calendar_id else None
    hub = Hub(cal.changes if cal else None)
    server = build_server(hub)

    async def poll() -> None:
        while True:
            await anyio.sleep(max(interval, POLL_MIN_S))
            try:
                await anyio.to_thread.run_sync(hub.tick, abandon_on_cancel=True)
            except Exception as exc:  # noqa: BLE001 -- not signed in, network: retry next interval
                log.warning("hermes-jobs events: poll failed: %s", gcal.redact(f"{type(exc).__name__}: {exc}")[:300])

    async def main() -> None:
        async with stdio_server() as (read, write):
            async with anyio.create_task_group() as tg:
                tg.start_soon(poll)
                await server.run(read, write, init_options(server))
                tg.cancel_scope.cancel()
    anyio.run(main)

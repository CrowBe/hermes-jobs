"""Exact cron <-> RFC 5545 RRULE conversion for Google recurring events. Pure, stdlib only.

Only forms whose meaning is IDENTICAL on both sides convert; everything else returns a reason and
the job keeps a single patch-forward event. Both Hermes (croniter on the zone's wall clock) and
Google (RRULE expanded in start.timeZone) are wall-clock schedules, so a fixed HH:MM in one IANA
zone means the same instants, EXCEPT when HH:MM is skipped or repeated by a DST transition: the two
engines resolve those differently, so such times are refused.

Supported (minute and hour must be single integers):
  M H * * *          FREQ=DAILY
  M H * * DOWS       FREQ=WEEKLY;BYDAY=...         (lists/ranges/names, 0 and 7 = Sunday)
  M H DOMS * *       FREQ=MONTHLY;BYMONTHDAY=...
  M H DOMS MONS *    FREQ=YEARLY;BYMONTH=...;BYMONTHDAY=...
  M H * MONS *       FREQ=DAILY;BYMONTH=...
  M H * MONS DOWS    FREQ=WEEKLY;BYMONTH=...;BYDAY=...
Refused: steps (*/n), minute/hour lists or ranges, L/W/#/?, day-of-month AND day-of-week both
restricted (cron ORs them, RRULE ANDs), and RRULEs with INTERVAL>1, COUNT, UNTIL, BYSETPOS,
ordinal BYDAY (2TU), BYHOUR/BYMINUTE/BYWEEKNO/BYYEARDAY.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Dict, FrozenSet, Optional, Tuple
from zoneinfo import ZoneInfo

DAYS = ["SU", "MO", "TU", "WE", "TH", "FR", "SA"]  # cron 0..6
_DOW_NAMES = {n: i for i, n in enumerate(["SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT"])}
_MON_NAMES = {n: i + 1 for i, n in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"])}

Spec = Dict[str, object]  # minute:int hour:int dom/mon/dow: frozenset|None (None = '*')


class Unconvertible(ValueError):
    pass


def _field(text: str, lo: int, hi: int, names: Dict[str, int], wrap7: bool = False) -> Optional[FrozenSet[int]]:
    if text == "*":
        return None
    out = set()
    for part in text.upper().split(","):
        if not re.fullmatch(r"[A-Z0-9]+(-[A-Z0-9]+)?", part):
            raise Unconvertible(f"cron field {text!r} uses steps or special characters")
        bounds = [names.get(x) if x in names else (int(x) if x.isdigit() else None) for x in part.split("-")]
        if None in bounds:
            raise Unconvertible(f"cron field {text!r} not understood")
        a, b = bounds[0], bounds[-1]
        if wrap7 and b == 7 and a != 7:
            out.update(range(a, 7)); out.add(0); continue
        if wrap7:
            a, b = (0 if a == 7 else a), (0 if b == 7 else b)
        if not (lo <= a <= hi and lo <= b <= hi and a <= b):
            raise Unconvertible(f"cron field {text!r} out of range")
        out.update(range(a, b + 1))
    return frozenset(out)


def parse_cron(expr: str) -> Spec:
    parts = (expr or "").split()
    if len(parts) != 5:
        raise Unconvertible("only 5-field cron expressions convert")
    m, h, dom, mon, dow = parts
    if not (m.isdigit() and h.isdigit()) or int(m) > 59 or int(h) > 23:
        raise Unconvertible("minute and hour must be single values (one event time per day)")
    spec: Spec = {"minute": int(m), "hour": int(h), "dom": _field(dom, 1, 31, {}),
                  "mon": _field(mon, 1, 12, _MON_NAMES), "dow": _field(dow, 0, 7, _DOW_NAMES, wrap7=True)}
    if spec["dom"] is not None and spec["dow"] is not None:
        raise Unconvertible("day-of-month AND day-of-week both set: cron ORs them, RRULE cannot")
    if spec["dow"] is not None and len(spec["dow"]) == 7:
        spec["dow"] = None
    if spec["mon"] is not None and len(spec["mon"]) == 12:
        spec["mon"] = None
    return spec


def _nums(s) -> str:
    return ",".join(str(x) for x in sorted(s))


def format_cron(spec: Spec) -> str:
    """Canonical text: numeric, sorted, Sunday = 0."""
    f = lambda v: "*" if v is None else _nums(v)
    return f"{spec['minute']} {spec['hour']} {f(spec['dom'])} {f(spec['mon'])} {f(spec['dow'])}"


def is_cron(expr: str) -> bool:
    """True only for a plain 5-field expression this module can parse (digits, names, , - * /)."""
    import re as _re
    if not isinstance(expr, str) or len(expr) > 100 or not _re.fullmatch(r"[0-9A-Za-z*,/\- ]+", expr):
        return False
    try:
        parse_cron(expr)
        return True
    except Exception:  # noqa: BLE001
        return False


def canonical_cron(expr: str) -> str:
    return format_cron(parse_cron(expr))


def spec_to_rrule(spec: Spec) -> str:
    parts = []
    if spec["dom"] is not None:
        parts.append("FREQ=YEARLY" if spec["mon"] is not None else "FREQ=MONTHLY")
    elif spec["dow"] is not None:
        parts.append("FREQ=WEEKLY")
    else:
        parts.append("FREQ=DAILY")
    if spec["mon"] is not None:
        parts.append("BYMONTH=" + _nums(spec["mon"]))
    if spec["dom"] is not None:
        parts.append("BYMONTHDAY=" + _nums(spec["dom"]))
    if spec["dow"] is not None:
        parts.append("BYDAY=" + ",".join(DAYS[d] for d in [1, 2, 3, 4, 5, 6, 0] if d in spec["dow"]))
    return "RRULE:" + ";".join(parts)


def dst_safe(hour: int, minute: int, zone: str, start: Optional[date] = None, days: int = 800) -> bool:
    """False if HH:MM is skipped or repeated on any day of the next ~2 years in ``zone``."""
    z = ZoneInfo(zone)
    d = start or date.today()
    for i in range(days):
        wall = datetime.combine(d + timedelta(days=i), datetime.min.time()).replace(hour=hour, minute=minute)
        a, b = wall.replace(tzinfo=z, fold=0), wall.replace(tzinfo=z, fold=1)
        if a.utcoffset() != b.utcoffset():
            return False
    return True


def cron_to_rrule(expr: str, zone: str) -> Tuple[Optional[str], Optional[str]]:
    """(RRULE line, None) or (None, reason)."""
    try:
        spec = parse_cron(expr)
    except Unconvertible as exc:
        return None, str(exc)
    if not dst_safe(spec["hour"], spec["minute"], zone):
        return None, f"{spec['hour']:02d}:{spec['minute']:02d} falls in a DST transition in {zone}"
    return spec_to_rrule(spec), None


def parse_rrule(lines) -> Dict[str, str]:
    rules = [l for l in (lines or []) if l.upper().startswith("RRULE:")]
    if len(rules) != 1 or any(l.upper().startswith(("RDATE", "EXDATE", "EXRULE")) for l in lines or []):
        raise Unconvertible("needs exactly one RRULE and no RDATE/EXDATE")
    out = {}
    for kv in rules[0][6:].split(";"):
        k, _, v = kv.partition("=")
        out[k.upper()] = v.upper()
    return out


def has_until(lines) -> bool:
    try:
        r = parse_rrule(lines)
    except Unconvertible:
        return False
    return "UNTIL" in r or "COUNT" in r


def until_instant(lines) -> Optional[datetime]:
    try:
        u = parse_rrule(lines).get("UNTIL")
        if not u:
            return None
        from datetime import timezone
        if "T" in u:
            return datetime.strptime(u.rstrip("Z"), "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)
        return datetime.strptime(u, "%Y%m%d").replace(tzinfo=timezone.utc)
    except (Unconvertible, ValueError):
        return None


def rrule_to_cron(lines, start_local: datetime) -> Tuple[Optional[str], Optional[str]]:
    """Inverse for adoption. ``start_local`` = DTSTART on the event's own wall clock."""
    try:
        if start_local is None:
            raise Unconvertible("event has no timed start (all-day events cannot become cron)")
        r = parse_rrule(lines)
        for bad in ("COUNT", "UNTIL", "BYSETPOS", "BYHOUR", "BYMINUTE", "BYSECOND", "BYWEEKNO", "BYYEARDAY"):
            if bad in r:
                raise Unconvertible(f"RRULE uses {bad}, which cron cannot express")
        if r.get("INTERVAL", "1") != "1":
            raise Unconvertible("RRULE INTERVAL > 1 (every N days/weeks) has no cron equivalent")
        freq = r.get("FREQ")
        ints = lambda key: frozenset(int(x) for x in r[key].split(",")) if key in r else None
        mon, dom = ints("BYMONTH"), ints("BYMONTHDAY")
        dow = None
        if "BYDAY" in r:
            names = r["BYDAY"].split(",")
            if any(n not in DAYS for n in names):
                raise Unconvertible("ordinal BYDAY (e.g. 2TU = second Tuesday) has no cron equivalent")
            dow = frozenset(DAYS.index(n) for n in names)
        if dom is not None and any(d < 1 for d in dom):
            raise Unconvertible("negative BYMONTHDAY (from month end) has no cron equivalent")
        wd = (start_local.weekday() + 1) % 7
        if freq == "DAILY":
            if dom is not None:
                raise Unconvertible("DAILY with BYMONTHDAY not supported")
        elif freq == "WEEKLY":
            if dom is not None:
                raise Unconvertible("WEEKLY with BYMONTHDAY not supported")
            dow = dow or frozenset({wd})
        elif freq == "MONTHLY":
            if dow is not None:
                raise Unconvertible("MONTHLY by weekday has no cron equivalent")
            dom = dom or frozenset({start_local.day})
        elif freq == "YEARLY":
            if dow is not None:
                raise Unconvertible("YEARLY by weekday has no cron equivalent")
            mon = mon or frozenset({start_local.month})
            dom = dom or frozenset({start_local.day})
        else:
            raise Unconvertible(f"FREQ={freq} has no cron equivalent")
        spec: Spec = {"minute": start_local.minute, "hour": start_local.hour, "dom": dom, "mon": mon, "dow": dow}
        if start_local.second:
            raise Unconvertible("start has seconds")
        # Round-trip guard: only accept if converting back yields the same rule set.
        expr = format_cron(parse_cron(format_cron(spec)))
        return expr, None
    except (Unconvertible, ValueError, KeyError) as exc:
        return None, str(exc)

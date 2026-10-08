from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

import caldav
from icalendar import Calendar
from calendar_write import process_commands


CALDAV_URL = "https://caldav.icloud.com/"
SNAPSHOT_SCHEMA = 1


@dataclass(frozen=True)
class SnapshotEvent:
    id: str
    calendar: str
    title: str
    start: str
    end: str
    all_day: bool
    location: str
    notes: str
    status: str
    uid: str = ""
    recurring: bool = False


def env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def _text(component: Any, name: str, default: str = "") -> str:
    value = component.get(name)
    return str(value) if value is not None else default


def _serialize_time(value: date | datetime, tz_name: str) -> str:
    if isinstance(value, datetime) and value.tzinfo is None:
        value = value.replace(tzinfo=ZoneInfo(tz_name))
    return value.isoformat()


def parse_ics(raw: bytes, calendar_name: str, tz_name: str) -> Iterable[SnapshotEvent]:
    parsed = Calendar.from_ical(raw)
    for item in parsed.walk("VEVENT"):
        uid = _text(item, "UID")
        if not uid or item.get("DTSTART") is None:
            continue

        status = _text(item, "STATUS", "confirmed").lower()
        if status == "cancelled":
            continue

        start = item.decoded("DTSTART")
        end = item.decoded("DTEND") if item.get("DTEND") is not None else None
        all_day = isinstance(start, date) and not isinstance(start, datetime)
        if end is None:
            end = start + (timedelta(days=1) if all_day else timedelta(hours=1))

        recurrence_id = item.decoded("RECURRENCE-ID") if item.get("RECURRENCE-ID") is not None else None
        source_key = f"{calendar_name}\x1f{uid}\x1f{recurrence_id or ''}\x1f{start.isoformat()}"
        event_id = hashlib.sha256(source_key.encode("utf-8")).hexdigest()[:24]

        yield SnapshotEvent(
            id=event_id,
            calendar=calendar_name,
            title=_text(item, "SUMMARY", "(Ohne Titel)"),
            start=_serialize_time(start, tz_name),
            end=_serialize_time(end, tz_name),
            all_day=all_day,
            location=_text(item, "LOCATION"),
            notes=_text(item, "DESCRIPTION"),
            uid=uid,
            recurring=item.get("RRULE") is not None or recurrence_id is not None,
            status=status if status in {"confirmed", "tentative"} else "confirmed",
        )


def fetch_icloud_events(start: datetime, end: datetime, tz_name: str) -> tuple[list[str], list[SnapshotEvent]]:
    username = env("ICLOUD_USERNAME")
    password = env("ICLOUD_APP_PASSWORD")
    selected = {x.strip() for x in os.environ.get("ICLOUD_CALENDARS", "").split(",") if x.strip()}

    with caldav.DAVClient(url=CALDAV_URL, username=username, password=password) as client:
        calendars = client.principal().calendars()
        available = {calendar.name: calendar for calendar in calendars}
        missing = sorted(selected - set(available))
        if selected and missing:
            print(json.dumps({"calendar_filter_status": "fallback_all", "missing": missing}))
            selected = set()

        chosen = [calendar for name, calendar in available.items() if not selected or name in selected]
        result: dict[str, SnapshotEvent] = {}
        for calendar in chosen:
            try:
                rows = calendar.search(start=start, end=end, event=True, expand=True)
            except Exception as exc:
                raise RuntimeError(f"Could not read iCloud calendar {calendar.name!r}: {exc}") from exc
            for row in rows:
                for event in parse_ics(row.data, calendar.name, tz_name):
                    result[event.id] = event

        events = sorted(result.values(), key=lambda event: (event.start, event.end, event.calendar, event.title))
        return sorted(calendar.name for calendar in chosen), events


def build_snapshot(
    calendars: list[str], events: list[SnapshotEvent], start: datetime, end: datetime, tz_name: str
) -> dict[str, Any]:
    return {
        "schema_version": SNAPSHOT_SCHEMA,
        "timezone": tz_name,
        "window": {"start": start.date().isoformat(), "end": end.date().isoformat()},
        "calendars": calendars,
        "events": [asdict(event) for event in events],
    }


def main() -> None:
    output = Path(os.environ.get("SNAPSHOT_PATH", "calendar_snapshot.json"))
    previous = json.loads(output.read_text(encoding="utf-8")) if output.exists() else {}
    command_results = previous.get("command_results", {})
    try:
        command_results = process_commands(env("ICLOUD_USERNAME"), env("ICLOUD_APP_PASSWORD"), command_results)
    except Exception as exc:
        print(json.dumps({"calendar_write_status": "unconfirmed", "error_type": type(exc).__name__}))
    tz_name = os.environ.get("BRIDGE_TIMEZONE", "Europe/Berlin")
    tz = ZoneInfo(tz_name)
    now = datetime.now(tz)
    start = datetime.combine(
        now.date() - timedelta(days=int(os.environ.get("SYNC_DAYS_PAST", "7"))), time.min, tz
    )
    end = datetime.combine(
        now.date() + timedelta(days=int(os.environ.get("SYNC_DAYS_FUTURE", "90"))), time.max, tz
    )
    calendars, events = fetch_icloud_events(start, end, tz_name)
    snapshot = build_snapshot(calendars, events, start, end, tz_name)
    snapshot["command_results"] = command_results
    output.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": "ok", "calendars": len(calendars), "events": len(events), "output": str(output)}))


if __name__ == "__main__":
    main()

"""Process explicitly requested calendar commands: create, update and delete.

Writable calendars: the ones listed in WRITABLE_CALENDARS (defaults to ICLOUD_CALENDARS).
Safety rules:
- Every command has a stable unique id; a confirmed id is never executed again.
- update/delete address one event by its CalDAV UID (see `uid` in calendar_snapshot.json).
- delete additionally requires `expected_title` to match the current event title.
- Recurring events and events with attendees (invitations) are never updated or deleted.
- Every write is verified by read-back before it is reported as confirmed.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import caldav
from caldav.lib.error import NotFoundError
from icalendar import Calendar, Event, Alarm


import os
_CALS = [x.strip() for x in os.environ.get("WRITABLE_CALENDARS", os.environ.get("ICLOUD_CALENDARS", "")).split(",") if x.strip()]
PERSONAL_CALENDAR = os.environ.get("PERSONAL_CALENDAR", _CALS[0] if _CALS else "")
CALENDAR_ALIASES = {}
WRITABLE_CALENDARS = set(_CALS)
PERSONAL_CALENDAR_ALIASES = {PERSONAL_CALENDAR}

COMMON_FIELDS = {"id", "action", "calendar"}
CREATE_FIELDS = COMMON_FIELDS | {"title", "start", "end", "notes", "location", "reminder_minutes"}
UPDATE_FIELDS = COMMON_FIELDS | {"uid", "title", "start", "end", "notes", "location", "reminder_minutes"}
DELETE_FIELDS = COMMON_FIELDS | {"uid", "expected_title"}


def calendar_name(command) -> str:
    name = command.get("calendar")
    name = CALENDAR_ALIASES.get(name, name)
    if name not in WRITABLE_CALENDARS:
        raise ValueError("Calendar is not writable: " + str(name))
    return name


def parse_when(value):
    if not isinstance(value, str):
        raise ValueError("Start/end must be text")
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return date.fromisoformat(value)
    parsed = datetime.fromisoformat(value)
    if parsed.utcoffset() is None:
        raise ValueError("Start/end must include an offset")
    return parsed


def _check_times(start, end):
    if type(start) is not type(end):
        raise ValueError("Start and end must both be dates or both be date-times")
    if end <= start:
        raise ValueError("End must follow start")


def _check_text(command):
    if "title" in command:
        title = command["title"]
        if not isinstance(title, str) or not title.strip() or len(title) > 500:
            raise ValueError("A non-empty title is required")
    for field in ("notes", "location", "expected_title"):
        if field in command and not isinstance(command[field], str):
            raise ValueError(f"{field} must be text")
    if "reminder_minutes" in command:
        minutes = command["reminder_minutes"]
        if type(minutes) is not int or not 0 <= minutes <= 10080:
            raise ValueError("Reminder must be an integer from 0 to 10080 minutes")


def fingerprint_of(command) -> str:
    return hashlib.sha256(json.dumps(command, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def validate(command):
    if not isinstance(command, dict):
        raise ValueError("Command must be an object")
    action = command.get("action")
    allowed = {"create": CREATE_FIELDS, "update": UPDATE_FIELDS, "delete": DELETE_FIELDS}.get(action)
    if allowed is None:
        raise ValueError("Action must be create, update or delete")
    if set(command) - allowed:
        raise ValueError("Unsupported fields: " + ", ".join(sorted(set(command) - allowed)))
    key = command.get("id", "")
    if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,100}", key):
        raise ValueError("A unique, stable command id is required")
    calendar_name(command)
    _check_text(command)
    if action == "create":
        if "title" not in command or "start" not in command or "end" not in command:
            raise ValueError("create needs title, start and end")
        _check_times(parse_when(command["start"]), parse_when(command["end"]))
    else:
        uid = command.get("uid")
        if not isinstance(uid, str) or not uid.strip():
            raise ValueError(f"{action} needs the event uid")
    if action == "update":
        if not set(command) & {"title", "start", "end", "notes", "location", "reminder_minutes"}:
            raise ValueError("update needs at least one changed field")
        if ("start" in command) != ("end" in command):
            raise ValueError("update must give start and end together")
        if "start" in command:
            _check_times(parse_when(command["start"]), parse_when(command["end"]))
    if action == "delete" and not command.get("expected_title", "").strip():
        raise ValueError("delete needs expected_title")
    return fingerprint_of(command)


def _set_alarm(event, title, minutes):
    for alarm in list(event.walk("VALARM")):
        event.subcomponents.remove(alarm)
    alarm = Alarm()
    alarm.add("action", "DISPLAY")
    alarm.add("description", title)
    alarm.add("trigger", -timedelta(minutes=minutes))
    event.add_component(alarm)


def _to_ical_time(value):
    return value.astimezone(timezone.utc) if isinstance(value, datetime) else value


def prepare(command):
    """Build the iCalendar text for a create command."""
    fingerprint = validate(command)
    if command["action"] != "create":
        raise ValueError("prepare only builds create commands")
    uid = "hark-bridge-" + hashlib.sha256(command["id"].encode()).hexdigest() + "@calendar.local"
    start, end = parse_when(command["start"]), parse_when(command["end"])
    event = Event()
    for name, value in [("uid", uid), ("summary", command["title"]), ("dtstart", _to_ical_time(start)),
                        ("dtend", _to_ical_time(end)), ("dtstamp", datetime.now(timezone.utc)),
                        ("description", command.get("notes", "")), ("location", command.get("location", "")),
                        ("class", "PRIVATE"), ("X-HARK-BRIDGE-HASH", fingerprint)]:
        event.add(name, value)
    _set_alarm(event, command["title"], command.get("reminder_minutes", 15))
    calendar = Calendar()
    calendar.add("prodid", "-//Hark Calendar Bridge//DE")
    calendar.add("version", "2.0")
    calendar.add_component(event)
    return uid, fingerprint, calendar.to_ical().decode()


def _single_event(resource):
    parsed = Calendar.from_ical(resource.data)
    events = parsed.walk("VEVENT")
    if len(events) != 1:
        raise ValueError("Recurring or multi-part events are not changed by the bridge")
    return parsed, events[0]


def _same_time(a, b):
    if isinstance(a, datetime) and isinstance(b, datetime):
        return a == b
    return a == b and type(a) is type(b)


def verify_fields(event, command):
    if "title" in command and str(event.get("SUMMARY")) != command["title"]:
        raise ValueError("Read-back title differs")
    if "start" in command and not _same_time(event.decoded("DTSTART"), parse_when(command["start"])):
        raise ValueError("Read-back start differs")
    if "end" in command and not _same_time(event.decoded("DTEND"), parse_when(command["end"])):
        raise ValueError("Read-back end differs")
    for field, prop in (("notes", "DESCRIPTION"), ("location", "LOCATION")):
        if field in command and str(event.get(prop, "")) != command[field]:
            raise ValueError(f"Read-back {field} differs")
    if "reminder_minutes" in command or command["action"] == "create":
        minutes = command.get("reminder_minutes", 15)
        if not any(a.get("ACTION") == "DISPLAY" and a.decoded("TRIGGER") == -timedelta(minutes=minutes)
                   for a in event.walk("VALARM")):
            raise ValueError("Reminder was not confirmed by read-back")


def verify(resource, uid, fingerprint, command):
    _, event = _single_event(resource)
    if str(event.get("UID")) != uid or str(event.get("X-HARK-BRIDGE-HASH")) != fingerprint:
        raise ValueError("Read-back differs from the requested event")
    verify_fields(event, command)


def _result(command, fingerprint, **extra):
    return {"id": command["id"], "action": command["action"], "status": "confirmed", "fingerprint": fingerprint,
            "confirmed_at": datetime.now(timezone.utc).isoformat(), **extra}


def create_one(calendar, command):
    uid, fingerprint, ics = prepare(command)
    try:
        resource = calendar.event_by_uid(uid)
    except NotFoundError:
        url = str(calendar.url).rstrip("/") + "/" + uid + ".ics"
        response = calendar.client.put(url, ics, headers={"Content-Type": "text/calendar; charset=utf-8", "If-None-Match": "*"})
        if response.status not in (201, 204, 412):
            raise RuntimeError(f"Calendar creation failed (HTTP {response.status})")
        resource = calendar.event_by_uid(uid)
    verify(resource, uid, fingerprint, command)
    return _result(command, fingerprint, uid=uid)


def _guard_changeable(event):
    if event.get("RRULE") is not None or event.get("RECURRENCE-ID") is not None:
        raise ValueError("Recurring events are not changed by the bridge")
    if event.get("ATTENDEE") is not None:
        raise ValueError("Events with attendees (invitations) are not changed by the bridge")


def update_one(calendar, command):
    fingerprint = validate(command)
    resource = calendar.event_by_uid(command["uid"])
    parsed, event = _single_event(resource)
    _guard_changeable(event)
    if str(event.get("X-HARK-BRIDGE-UPDATE")) == fingerprint:
        verify_fields(event, command)
        return _result(command, fingerprint, uid=command["uid"])
    for field, prop in (("title", "SUMMARY"), ("notes", "DESCRIPTION"), ("location", "LOCATION")):
        if field in command:
            if prop in event:
                del event[prop]
            event.add(prop, command[field])
    if "start" in command:
        for prop in ("DTSTART", "DTEND", "DURATION"):
            if prop in event:
                del event[prop]
        event.add("DTSTART", _to_ical_time(parse_when(command["start"])))
        event.add("DTEND", _to_ical_time(parse_when(command["end"])))
    if "reminder_minutes" in command:
        _set_alarm(event, str(event.get("SUMMARY")), command["reminder_minutes"])
    for prop in ("DTSTAMP", "LAST-MODIFIED", "X-HARK-BRIDGE-UPDATE"):
        if prop in event:
            del event[prop]
    now = datetime.now(timezone.utc)
    event.add("DTSTAMP", now)
    event.add("LAST-MODIFIED", now)
    event.add("X-HARK-BRIDGE-UPDATE", fingerprint)
    sequence = int(event.get("SEQUENCE", 0)) + 1
    if "SEQUENCE" in event:
        del event["SEQUENCE"]
    event.add("SEQUENCE", sequence)
    resource.data = parsed.to_ical().decode()
    resource.save()
    _, saved = _single_event(calendar.event_by_uid(command["uid"]))
    verify_fields(saved, command)
    return _result(command, fingerprint, uid=command["uid"])


def delete_one(calendar, command):
    fingerprint = validate(command)
    try:
        resource = calendar.event_by_uid(command["uid"])
    except NotFoundError:
        return _result(command, fingerprint, uid=command["uid"], note="already absent")
    _, event = _single_event(resource)
    _guard_changeable(event)
    if str(event.get("SUMMARY", "")).strip() != command["expected_title"].strip():
        raise ValueError("Current title does not match expected_title")
    resource.delete()
    try:
        calendar.event_by_uid(command["uid"])
    except NotFoundError:
        return _result(command, fingerprint, uid=command["uid"])
    raise RuntimeError("Event still present after delete")


HANDLERS = {"create": create_one, "update": update_one, "delete": delete_one}


def process_commands(username, password, previous):
    path = Path("calendar_commands.json")
    if not path.exists():
        return previous
    queue = json.loads(path.read_text(encoding="utf-8"))
    commands = queue.get("commands")
    if queue.get("schema_version") != 1 or not isinstance(commands, list):
        raise ValueError("Invalid calendar command queue")
    ids = [c.get("id") for c in commands if isinstance(c, dict)]
    if len(ids) != len(commands) or len(ids) != len(set(ids)):
        raise ValueError("Command ids must be unique")
    results = dict(previous)
    pending = []
    for command in commands:
        key = command["id"]
        old = results.get(key, {})
        if old.get("status") == "confirmed":
            continue  # never re-run a confirmed command
        try:
            validate(command)
            pending.append(command)
        except (ValueError, TypeError, KeyError) as exc:
            results[key] = {"id": key, "status": "rejected", "reason": str(exc)}
    if not pending:
        return results
    with caldav.DAVClient(url="https://caldav.icloud.com/", username=username, password=password) as client:
        available = {}
        for cal in client.principal().calendars():
            available.setdefault(cal.name, []).append(cal)
        for command in pending:
            try:
                name = calendar_name(command)
                matches = available.get(name, [])
                if len(matches) != 1:
                    raise ValueError(f"Exactly one calendar named {name} must exist")
                results[command["id"]] = HANDLERS[command["action"]](matches[0], command)
            except (ValueError, NotFoundError) as exc:
                results[command["id"]] = {"id": command["id"], "status": "rejected", "reason": str(exc)[:200]}
            except Exception as exc:
                # Do not log server response bodies, URLs or credentials.
                results[command["id"]] = {"id": command["id"], "status": "unconfirmed", "error_type": type(exc).__name__}
    return results

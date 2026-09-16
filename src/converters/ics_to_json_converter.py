#!/usr/bin/env python3
"""Convert a Google Calendar ICS file into grouped JSON agendas.

Groups events by color, date and doctor while preserving relevant fields:
- date
- organizer_email (fixed/configurable)
- guests
- description
- event color (if present)
"""

from __future__ import annotations

import argparse
import json
import os
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore


DEFAULT_ICS_PATH = Path(
    "src/converters/calendar_file/DR MARLON ENJOY.ics"
)
DEFAULT_OUTPUT_PATH = Path("src/converters/agenda_grouped.json")
DEFAULT_ORGANIZER_EMAIL = "agenda@enjoydental.local"


@dataclass
class CalendarMeta:
    name: str | None = None
    timezone: str = "UTC"
    description: str | None = None


def unfold_ics_lines(raw_text: str) -> list[str]:
    """Unfold RFC5545 folded lines (continuation starts with space/tab)."""
    unfolded: list[str] = []
    for raw_line in raw_text.splitlines():
        if not unfolded:
            unfolded.append(raw_line)
            continue
        if raw_line.startswith((" ", "\t")):
            unfolded[-1] += raw_line[1:]
        else:
            unfolded.append(raw_line)
    return unfolded


def unescape_ics_text(value: str) -> str:
    """Decode escaped characters used by ICS text fields."""
    return (
        value.replace("\\n", "\n")
        .replace("\\N", "\n")
        .replace("\\,", ",")
        .replace("\\;", ";")
        .replace("\\\\", "\\")
    ).strip()


def split_property(line: str) -> tuple[str, str]:
    """Split an ICS line into property name and value.

    Supports keys with parameters: KEY;PARAM=VALUE:actual-value
    """
    if ":" not in line:
        return line, ""
    left, value = line.split(":", 1)
    prop_name = left.split(";", 1)[0]
    return prop_name, value


def parse_ics_datetime(value: str) -> datetime | None:
    """Parse common ICS datetime formats into timezone-aware datetimes."""
    raw = value.strip()

    if not raw:
        return None

    # UTC date-time (e.g. 20221121T160000Z)
    if re.fullmatch(r"\d{8}T\d{6}Z", raw):
        return datetime.strptime(raw, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)

    # Local date-time without Z (e.g. 20221121T160000)
    if re.fullmatch(r"\d{8}T\d{6}", raw):
        return datetime.strptime(raw, "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)

    # Date-only (all-day)
    if re.fullmatch(r"\d{8}", raw):
        return datetime.strptime(raw, "%Y%m%d").replace(tzinfo=timezone.utc)

    return None


def normalize_doctor(summary: str, fallback_calendar_name: str | None) -> str:
    """Extract doctor from summary with tolerant matching."""
    text = summary.strip()
    if not text:
        return fallback_calendar_name or "unknown"

    # Examples supported:
    # "Dr Marlon", "dr marlon", "Dra Beatriz", "Dr Marlon Córdoba"
    match = re.search(r"\b(Dr(?:a)?\.?\s+[A-Za-zÁÉÍÓÚáéíóúÑñ]+(?:\s+[A-Za-zÁÉÍÓÚáéíóúÑñ]+)?)", text, re.IGNORECASE)
    if match:
        raw_name = match.group(1)
        # Normalize capitalization but preserve accents.
        normalized = " ".join(part.capitalize() for part in raw_name.replace(".", "").split())
        return normalized

    return fallback_calendar_name or "unknown"


def extract_attendee_email(raw_value: str) -> str | None:
    value = raw_value.strip()
    if not value:
        return None
    # Common ICS attendee value: MAILTO:user@example.com
    if value.upper().startswith("MAILTO:"):
        return value[7:].strip() or None
    if "@" in value:
        return value
    return None


def parse_calendar(lines: list[str]) -> tuple[CalendarMeta, list[dict[str, Any]]]:
    meta = CalendarMeta()
    events: list[dict[str, Any]] = []

    current_event: dict[str, Any] | None = None

    for line in lines:
        stripped = line.strip("\r")

        if stripped == "BEGIN:VEVENT":
            current_event = {"ATTENDEE": []}
            continue

        if stripped == "END:VEVENT":
            if current_event is not None:
                events.append(current_event)
            current_event = None
            continue

        prop, value = split_property(stripped)

        if current_event is not None:
            if prop == "ATTENDEE":
                attendee_email = extract_attendee_email(value)
                if attendee_email:
                    current_event["ATTENDEE"].append(attendee_email)
            elif prop in current_event and prop in {"DESCRIPTION"}:
                # Keep multiline text joined if repeated.
                current_event[prop] = f"{current_event[prop]}\n{value}".strip()
            else:
                current_event[prop] = value
            continue

        # VCALENDAR metadata
        if prop == "X-WR-CALNAME":
            meta.name = unescape_ics_text(value)
        elif prop == "X-WR-TIMEZONE":
            meta.timezone = value.strip() or "UTC"
        elif prop == "X-WR-CALDESC":
            meta.description = unescape_ics_text(value)

    return meta, events


def build_event_record(
    event: dict[str, Any],
    *,
    calendar_meta: CalendarMeta,
    organizer_email: str,
) -> dict[str, Any]:
    dt_start = parse_ics_datetime(event.get("DTSTART", ""))
    dt_end = parse_ics_datetime(event.get("DTEND", ""))

    tz_name = calendar_meta.timezone or "UTC"
    if ZoneInfo is not None:
        try:
            target_tz = ZoneInfo(tz_name)
        except Exception:
            target_tz = timezone.utc
    else:
        target_tz = timezone.utc

    start_local = dt_start.astimezone(target_tz) if dt_start else None
    end_local = dt_end.astimezone(target_tz) if dt_end else None

    raw_summary = unescape_ics_text(event.get("SUMMARY", ""))
    raw_description = unescape_ics_text(event.get("DESCRIPTION", ""))
    description = raw_description if raw_description else raw_summary

    attendees = event.get("ATTENDEE", [])
    if not isinstance(attendees, list):
        attendees = []

    doctor = normalize_doctor(raw_summary, calendar_meta.name)

    record = {
        "uid": event.get("UID"),
        "date": start_local.date().isoformat() if start_local else None,
        "start_at": start_local.isoformat() if start_local else None,
        "end_at": end_local.isoformat() if end_local else None,
        "organizer_email": organizer_email,
        "guests": attendees,
        "description": description,
        "summary": raw_summary,
        "doctor": doctor,
        "color": event.get("X-GOOGLE-EVENT-LABEL-COLOR") or "unknown",
        "label": unescape_ics_text(event.get("X-GOOGLE-EVENT-LABEL-NAME", "")) or None,
        "status": event.get("STATUS"),
        "location": unescape_ics_text(event.get("LOCATION", "")) or None,
    }

    return record


def build_grouped_output(
    records: list[dict[str, Any]],
    *,
    meta: CalendarMeta,
    organizer_email: str,
) -> dict[str, Any]:
    by_color: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_date: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_doctor: dict[str, list[dict[str, Any]]] = defaultdict(list)

    for record in records:
        color_key = record.get("color") or "unknown"
        date_key = record.get("date") or "unknown"
        doctor_key = record.get("doctor") or "unknown"

        by_color[color_key].append(record)
        by_date[date_key].append(record)
        by_doctor[doctor_key].append(record)

    return {
        "meta": {
            "calendar_name": meta.name,
            "calendar_description": meta.description,
            "calendar_timezone": meta.timezone,
            "organizer_email": organizer_email,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "total_events": len(records),
        },
        "events": records,
        "grouped": {
            "by_color": dict(by_color),
            "by_date": dict(by_date),
            "by_doctor": dict(by_doctor),
        },
    }


def convert_ics_to_json(
    ics_path: Path,
    output_path: Path,
    organizer_email: str,
) -> dict[str, Any]:
    raw_text = ics_path.read_text(encoding="utf-8")
    lines = unfold_ics_lines(raw_text)
    meta, events = parse_calendar(lines)

    records = [
        build_event_record(
            event,
            calendar_meta=meta,
            organizer_email=organizer_email,
        )
        for event in events
    ]

    payload = build_grouped_output(records, meta=meta, organizer_email=organizer_email)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return payload


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Convert ICS agenda to grouped JSON (color/date/doctor)."
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=DEFAULT_ICS_PATH,
        help="Path to .ics input file.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT_PATH,
        help="Path to output .json file.",
    )
    parser.add_argument(
        "--organizer-email",
        default=os.getenv("ORGANIZER_EMAIL", DEFAULT_ORGANIZER_EMAIL),
        help="Fixed organizer email to include in every event.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    payload = convert_ics_to_json(
        ics_path=args.input,
        output_path=args.output,
        organizer_email=args.organizer_email,
    )
    print(
        f"Converted {payload['meta']['total_events']} events to {args.output} "
        f"(grouped by color/date/doctor)."
    )


if __name__ == "__main__":
    main()

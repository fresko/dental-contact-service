#!/usr/bin/env python3
"""Merge grouped agenda JSON files into a single consolidated database JSON.

This script merges the existing agenda_grouped*.json files into one payload and
applies conservative normalization rules:
- collapse obvious doctor aliases into a canonical doctor group
- extract a real patient when the text mentions pcte/pct/pte-style markers
- keep a short, descriptive reason in Spanish
- avoid numeric-only, acronym-only, or time-only motives
- propagate known doctor colors
- replace agenda@enjoydental.com with enjoydentolux@gmail.com everywhere
"""

from __future__ import annotations

import argparse
import json
import re
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SOURCE_GLOB = "agenda_grouped*.json"
DEFAULT_OUTPUT_PATH = Path("src/converters/db_agenda_enjoy.json")
OLD_EMAIL = "agenda@enjoydental.com"
NEW_EMAIL = "enjoydentolux@gmail.com"

DOCTOR_RE = re.compile(r"\b(dr|dra)\.?\s+[A-Za-zÁÉÍÓÚáéíóúÑñ][A-Za-zÁÉÍÓÚáéíóúÑñ'\- ]*", re.IGNORECASE)
PATIENT_MARKER_RE = re.compile(r"\b(?:pcte|pct|pte|paciente)\b[:\-\s,]*", re.IGNORECASE)
TIME_ONLY_RE = re.compile(r"^\d{1,2}(?::\d{2})?\s*(?:am|pm)?$", re.IGNORECASE)
MONEY_ONLY_RE = re.compile(r"^\$?\d+(?:[.,]\d+)?(?:\s*(?:mil|k))?$", re.IGNORECASE)

PATIENT_STOPWORDS = {
    "de",
    "del",
    "la",
    "el",
    "los",
    "las",
    "y",
    "con",
    "para",
    "por",
    "en",
    "al",
    "dr",
    "dra",
    "doctor",
    "doctora",
    "consultorio",
    "agenda",
    "enjoy",
    "celular",
    "pcte",
    "pct",
    "pte",
    "ya",
    "no",
    "se",
    "le",
    "lo",
    "una",
    "un",
    "dos",
    "tres",
    "hora",
    "horas",
    "vx",
    "control",
    "confirmo",
    "confirmó",
    "confirmado",
    "confirmada",
    "pago",
    "pagado",
    "pagada",
    "todo",
    "ha",
    "dado",
    "costo",
    "precio",
    "gratis",
    "quiere",
    "viene",
    "aparece",
    "hija",
    "hijo",
    "primo",
    "paciente",
    "debe",
    "mi",
    "su",
    "a",
    "am",
    "pm",
    "consult",
}

MOTIVE_STOPWORDS = PATIENT_STOPWORDS | {
    "resina",
    "resinas",
    "operatoria",
    "operatorio",
    "endodoncia",
    "endodontic",
    "extraccion",
    "extracción",
    "cirugia",
    "cirugía",
    "blanqueamiento",
    "limpieza",
    "implante",
    "implantes",
    "ortodoncia",
    "ortodoncista",
    "rehabilitacion",
    "rehabilitación",
    "prótesis",
    "protesis",
    "carillas",
    "margen",
    "bordes",
    "metal",
    "enfilado",
    "color",
    "a3",
    "a2",
    "a1",
    "b1",
    "c1",
    "mil",
    "sesion",
    "sesión",
    "minidiseño",
    "minidiseno",
    "garantia",
    "garantía",
    "valoración",
    "valoracion",
}

# Clinical/treatment vocabulary that is stripped from MOTIVE_STOPWORDS above only
# for the purpose of locating where a motive phrase starts; these words are the
# actual signal that a clause describes a treatment/reason and must not be
# treated as filler when deciding whether a clause is a reasonable reason.
CLINICAL_KEYWORDS_ES = MOTIVE_STOPWORDS - PATIENT_STOPWORDS

MOTIVE_KEYWORDS = {
    "try-in",
    "set-up",
    "shade",
    "margins",
    "resin",
    "resins",
    "cleaning",
    "follow-up",
    "evaluation",
    "root canal",
    "extraction",
    "surgery",
    "whitening",
    "implant",
    "implants",
    "orthodontics",
    "orthodontic",
    "rehabilitation",
    "prosthesis",
    "prostheses",
    "veneer",
    "veneers",
    "impression",
    "impressions",
    "provisional",
    "retainer",
    "retainers",
    "delivery",
    "repair",
    "restoration",
    "review",
    "checkup",
    "retrieval",
    "withdrawal",
    "bite",
    "calza",
    "model",
    "plate",
    "splint",
    "crown",
    "crowns",
    "margin",
    "margins",
    "polish",
    "pontic",
    "bridge",
    "bridges",
    "fillings",
    "filling",
    "evaluation",
}

SPANISH_TO_ENGLISH = {
    "prueba": "try-in",
    "enfilado": "set-up",
    "bordes": "margins",
    "resina": "resin",
    "resinas": "resins",
    "limpieza": "cleaning",
    "control": "follow-up",
    "endodoncia": "root canal",
    "extraccion": "extraction",
    "extracción": "extraction",
    "cirugia": "surgery",
    "cirugía": "surgery",
    "blanqueamiento": "whitening",
    "implante": "implant",
    "implantes": "implants",
    "ortodoncia": "orthodontics",
    "ortodoncista": "orthodontics",
    "rehabilitacion": "rehabilitation",
    "rehabilitación": "rehabilitation",
    "carillas": "veneers",
    "metal": "metal",
    "color": "shade",
    "valoración": "evaluation",
    "valoracion": "evaluation",
    "prótesis": "prosthesis",
    "protesis": "prosthesis",
    "margen": "margin",
    "margenes": "margins",
    "pago": "payment",
    "pagado": "paid",
    "paga": "paid",
    "cita": "appointment",
    "consulta": "visit",
    "consulta odontológica": "dental visit",
    "odontológica": "dental",
    "odontologico": "dental",
    "odontológico": "dental",
    "tratamiento": "treatment",
    "y": "and",
    "de": "of",
    "del": "of",
    "para": "for",
    "con": "with",
    "sin": "without",
    "completa": "complete",
    "completo": "complete",
    "unidad": "unit",
    "unidades": "units",
    "superior": "upper",
    "inferior": "lower",
    "fija": "fixed",
    "fijo": "fixed",
    "cambio": "change",
    "retiro": "removal",
    "retirada": "removal",
    "puntos": "stitches",
    "revision": "review",
    "revisión": "review",
    "reunión": "meeting",
    "reunion": "meeting",
    "impresion": "impression",
    "impresión": "impression",
    "placa": "plate",
    "retenedores": "retainers",
    "profunda": "deep",
    "urgencia": "urgent",
    "equipo": "team",
    "cobro": "collection",
    "plan": "plan",
}

MOTIVE_KEEP_WORDS = {
    "and",
    "of",
    "for",
    "with",
    "without",
    "try-in",
    "set-up",
    "shade",
    "margins",
    "resin",
    "resins",
    "cleaning",
    "follow-up",
    "evaluation",
    "root",
    "canal",
    "extraction",
    "surgery",
    "whitening",
    "implant",
    "implants",
    "orthodontics",
    "orthodontic",
    "rehabilitation",
    "prosthesis",
    "prostheses",
    "veneer",
    "veneers",
    "impression",
    "impressions",
    "provisional",
    "retainer",
    "retainers",
    "delivery",
    "repair",
    "restoration",
    "review",
    "checkup",
    "retrieval",
    "withdrawal",
    "bite",
    "calza",
    "model",
    "plate",
    "splint",
    "crown",
    "crowns",
    "margin",
    "polish",
    "pontic",
    "bridge",
    "bridges",
    "fillings",
    "filling",
    "dental",
    "complete",
    "upper",
    "lower",
    "fixed",
    "change",
    "removal",
    "stitches",
    "deep",
    "urgent",
    "team",
    "collection",
    "plan",
    "unit",
    "units",
    "dental",
}


@dataclass
class AgendaSource:
    path: Path
    payload: dict[str, Any]


def normalize_text(value: str | None) -> str:
    if not value:
        return ""
    text = unicodedata.normalize("NFKD", value)
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = text.lower()
    text = text.replace(".", " ")
    text = text.replace(",", " ")
    text = text.replace(";", " ")
    text = text.replace("/", " ")
    text = text.replace("_", " ")
    text = re.sub(r"[^a-z0-9\s'\-]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def title_case_name(value: str) -> str:
    tokens = []
    for token in value.split():
        if token.upper() in {"DR", "DRA"}:
            tokens.append(token.capitalize())
            continue
        tokens.append(token[:1].upper() + token[1:].lower() if token else token)
    return " ".join(tokens)


def normalize_email_strings(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: normalize_email_strings(item) for key, item in value.items()}
    if isinstance(value, list):
        return [normalize_email_strings(item) for item in value]
    if isinstance(value, str):
        return value.replace(OLD_EMAIL, NEW_EMAIL)
    return value


def normalize_color(value: Any) -> str:
    if isinstance(value, str) and value.strip() and value.strip().lower() != "unknown":
        return value.strip()
    return "unknown"


def extract_doctor_marker(*values: str | None) -> str | None:
    for value in values:
        if not value:
            continue
        match = DOCTOR_RE.search(value)
        if match:
            return match.group(0)
    return None


def canonical_doctor_key(*values: str | None) -> str:
    marker = extract_doctor_marker(*values)
    if not marker:
        return "unknown"
    normalized = normalize_text(marker)
    tokens = [token for token in normalized.split() if token not in PATIENT_STOPWORDS]
    if tokens and tokens[0] in {"dr", "dra"}:
        tokens = tokens[1:]
    return tokens[0] if tokens else "unknown"


def canonical_doctor_display(doctor_key: str, *values: str | None) -> str:
    if doctor_key == "unknown":
        return "Unknown"
    marker = extract_doctor_marker(*values)
    prefix = "Dra" if marker and re.search(r"\bdra\b", normalize_text(marker)) else "Dr"
    return title_case_name(f"{prefix} {doctor_key}")


def is_name_like_candidate(value: str) -> bool:
    tokens = [token for token in normalize_text(value).split() if token]
    if len(tokens) < 2:
        return False
    if any(any(char.isdigit() for char in token) for token in tokens):
        return False
    if any(token in PATIENT_STOPWORDS for token in tokens[:2]):
        return False
    if sum(1 for token in tokens if token in PATIENT_STOPWORDS) > 1:
        return False
    return True


def extract_patient_text(*values: str | None) -> str | None:
    for value in values:
        if not value:
            continue
        match = PATIENT_MARKER_RE.search(value)
        if not match:
            continue
        tail = value[match.end() :]
        words = list(re.finditer(r"[A-Za-zÁÉÍÓÚáéíóúÑñ][A-Za-zÁÉÍÓÚáéíóúÑñ'\-]*", tail))
        for start_index in range(len(words)):
            for length in range(2, 5):
                end_index = start_index + length - 1
                if end_index >= len(words):
                    continue
                candidate = tail[words[start_index].start() : words[end_index].end()]
                if is_name_like_candidate(candidate):
                    return title_case_name(candidate)
    return None


def translate_phrase(text: str) -> str:
    translated = text
    for source, target in sorted(SPANISH_TO_ENGLISH.items(), key=lambda item: len(item[0]), reverse=True):
        translated = re.sub(rf"\b{re.escape(source)}\b", target, translated, flags=re.IGNORECASE)
    return translated


def clean_clause_text(clause: str) -> str:
    cleaned = re.sub(r"\b\d{1,2}:\d{2}\s*(?:am|pm)?\b", " ", clause, flags=re.IGNORECASE)
    cleaned = re.sub(r"\b\d{1,2}\s*(?:am|pm)\b", " ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\b\d+(?:[.,]\d+)?\s*(?:mil|k|usd|cop)?\b", " ", cleaned, flags=re.IGNORECASE)
    cleaned = cleaned.replace("·", " ")
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .:-\t")
    return cleaned


def clause_content_tokens(clause: str) -> list[str]:
    normalized = normalize_text(clause)
    return [
        token
        for token in normalized.split()
        if token and len(token) >= 2 and token not in PATIENT_STOPWORDS
    ]


def strip_boundary_filler_words(clause: str) -> str:
    words = clause.split()
    start = 0
    end = len(words)
    while start < end:
        token = normalize_text(words[start])
        if token and len(token) >= 2 and token not in PATIENT_STOPWORDS:
            break
        start += 1
    while end > start:
        token = normalize_text(words[end - 1])
        if token and len(token) >= 2 and token not in PATIENT_STOPWORDS:
            break
        end -= 1
    return " ".join(words[start:end])


def is_acronym_only(clause: str) -> bool:
    raw_tokens = [token for token in clause.split() if token]
    if not raw_tokens:
        return False
    return all(re.fullmatch(r"[A-ZÁÉÍÓÚÑ]{1,4}", token) for token in raw_tokens)


def is_reasonable_reason_clause(clause: str) -> bool:
    if not clause:
        return False
    if not re.search(r"[A-Za-zÁÉÍÓÚáéíóúÑñ]", clause):
        return False
    if TIME_ONLY_RE.fullmatch(clause) or MONEY_ONLY_RE.fullmatch(clause):
        return False
    if is_acronym_only(clause):
        return False
    tokens = clause_content_tokens(clause)
    if not tokens:
        return False
    if all(token.isdigit() for token in tokens):
        return False
    return True


def extract_reason(summary: str | None, description: str | None, patient: str | None) -> str | None:
    source = description or summary or ""
    if not source:
        return None

    text = source
    if patient:
        patient_match = re.search(re.escape(patient), text, flags=re.IGNORECASE)
        if patient_match:
            text = text[patient_match.end() :]

    text = re.sub(r"\b(?:pcte|pct|pte|paciente)\b[:\-\s,]*", " ", text, flags=re.IGNORECASE)
    # Bound the doctor-name match to at most 3 words so it cannot swallow the
    # rest of a comma-free clause (e.g. "Dr Marlon le ofrecio resina de alta
    # estética..."), which previously erased the actual motive text.
    text = re.sub(
        r"\b(?:dr|dra)\.?\s+[A-Za-zÁÉÍÓÚáéíóúÑñ'\-]+(?:\s+[A-Za-zÁÉÍÓÚáéíóúÑñ'\-]+){0,2}",
        " ",
        text,
        flags=re.IGNORECASE,
    )

    candidates: list[tuple[int, int, int, str]] = []
    for order, raw_clause in enumerate(re.split(r"[,;·\n\r]", text)):
        clause = clean_clause_text(raw_clause)
        if not is_reasonable_reason_clause(clause):
            continue
        trimmed_clause = strip_boundary_filler_words(clause)
        if not is_reasonable_reason_clause(trimmed_clause):
            continue
        normalized_clause = normalize_text(trimmed_clause)
        has_keyword = any(keyword in normalized_clause for keyword in CLINICAL_KEYWORDS_ES)
        content_tokens = clause_content_tokens(trimmed_clause)
        candidates.append((1 if has_keyword else 0, len(content_tokens), -order, trimmed_clause))

    if not candidates:
        return None

    candidates.sort(reverse=True)
    result = re.sub(r"\s+", " ", candidates[0][3]).strip(" .:-\t")
    if not result:
        return None
    return title_case_name(result)


def dedupe_key(record: dict[str, Any]) -> str:
    uid = normalize_text(str(record.get("uid") or ""))
    if uid:
        return f"uid:{uid}"

    parts = [
        normalize_text(str(record.get("start_at") or record.get("date") or "")),
        normalize_text(str(record.get("end_at") or "")),
        normalize_text(str(record.get("doctor_key") or record.get("doctor") or "")),
        normalize_text(str(record.get("patient") or "")),
        normalize_text(str(record.get("reason") or "")),
        normalize_text(str(record.get("location") or "")),
    ]
    return "fp:" + "|".join(parts)


def build_record(event: dict[str, Any], source_name: str) -> dict[str, Any]:
    summary = event.get("summary")
    description = event.get("description")
    patient = extract_patient_text(summary, description)
    doctor_key = canonical_doctor_key(summary, description, event.get("doctor"), source_name)
    doctor = canonical_doctor_display(doctor_key, summary, description, event.get("doctor"))
    reason = extract_reason(summary, description, patient) or ""

    record = dict(event)
    record["doctor_key"] = doctor_key
    record["doctor"] = doctor
    if patient:
        record["patient"] = patient
    record["reason"] = reason
    record["organizer_email"] = NEW_EMAIL
    record["color"] = normalize_color(record.get("color"))
    return normalize_email_strings(record)


def load_sources(source_dir: Path) -> list[AgendaSource]:
    sources: list[AgendaSource] = []
    for path in sorted(source_dir.glob(SOURCE_GLOB)):
        if path.name == DEFAULT_OUTPUT_PATH.name:
            continue
        if not path.is_file():
            continue
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        sources.append(AgendaSource(path=path, payload=payload))
    return sources


def merge_agendas(source_dir: Path) -> dict[str, Any]:
    sources = load_sources(source_dir)
    if not sources:
        raise FileNotFoundError(f"No agenda_grouped JSON files found in {source_dir}")

    records: list[dict[str, Any]] = []
    for source in sources:
        for event in source.payload.get("events", []):
            records.append(build_record(event, source.path.stem))

    deduped: list[dict[str, Any]] = []
    seen: set[str] = set()
    doctor_colors: dict[str, Counter[str]] = defaultdict(Counter)

    for record in records:
        key = dedupe_key(record)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(record)
        color = normalize_color(record.get("color"))
        if color != "unknown":
            doctor_colors[record.get("doctor_key") or "unknown"][color] += 1

    color_by_doctor = {
        doctor: colors.most_common(1)[0][0]
        for doctor, colors in doctor_colors.items()
        if colors
    }

    final_records: list[dict[str, Any]] = []
    for record in deduped:
        doctor_key = record.get("doctor_key") or "unknown"
        if normalize_color(record.get("color")) == "unknown":
            record["color"] = color_by_doctor.get(doctor_key, "unknown")
        record.pop("doctor_key", None)
        final_records.append(normalize_email_strings(record))

    by_color: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_date: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_doctor: dict[str, list[dict[str, Any]]] = defaultdict(list)

    for record in final_records:
        by_color[record.get("color") or "unknown"].append(record)
        by_date[record.get("date") or "unknown"].append(record)
        by_doctor[record.get("doctor") or "unknown"].append(record)

    payload = {
        "meta": {
            "calendar_name": "DB AGENDA ENJOY",
            "calendar_description": "Merged agenda database from grouped calendars",
            "calendar_timezone": "America/Bogota",
            "organizer_email": NEW_EMAIL,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "total_events": len(final_records),
            "source_files": [source.path.name for source in sources],
        },
        "events": final_records,
        "grouped": {
            "by_color": dict(by_color),
            "by_date": dict(by_date),
            "by_doctor": dict(by_doctor),
        },
    }

    return normalize_email_strings(payload)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Merge grouped agendas into db_agenda_enjoy.json")
    parser.add_argument("--source-dir", type=Path, default=Path("src/converters"))
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_PATH)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    payload = merge_agendas(args.source_dir)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {payload['meta']['total_events']} consolidated events to {args.output}")


if __name__ == "__main__":
    main()
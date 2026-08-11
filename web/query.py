"""HTTP query parsing and cursor serialization for the dashboard API."""

import base64
import json
from datetime import date, datetime


def first_value(query: dict, key: str, default=None):
    values = query.get(key)
    return values[0] if values else default


def integer_value(query: dict, key: str, default=None):
    value = first_value(query, key)
    if value in (None, ""):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ValueError(f"Invalid {key}")


def parse_scope(query: dict) -> str:
    scope = first_value(query, "scope", "latest")
    if scope not in ("latest", "all"):
        raise ValueError("Invalid scope")
    return scope


def parse_applied(query: dict):
    value = first_value(query, "applied", "all")
    if value not in ("all", "true", "false"):
        raise ValueError("Invalid applied filter")
    return None if value == "all" else value == "true"


def parse_job_filters(query: dict, *, paginated: bool = False) -> dict:
    """Convert one HTTP query format into the MySQLStore filter payload."""
    filters = {
        "scope": parse_scope(query),
        "run_id": integer_value(query, "run_id"),
        "company": query.get("company", []),
        "location": query.get("location", []),
        "date": query.get("date", []),
        "title": first_value(query, "title", ""),
        "applied": parse_applied(query),
    }
    if paginated:
        filters.update({
            "cursor": decode_cursor(first_value(query, "cursor", "")),
            "limit": integer_value(query, "limit", 50),
        })
    return filters


def encode_cursor(value):
    if not value:
        return None
    raw = json.dumps(value, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(value):
    if not value:
        return None
    try:
        padded = value + "=" * (-len(value) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded))
        return {"discovered_at": str(data["discovered_at"]), "id": int(data["id"])}
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        raise ValueError("Invalid cursor")


def serialise(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    raise TypeError(f"Unsupported JSON value: {type(value)!r}")

#!/usr/bin/env python3
"""Evidence-first history, deduplication, and reporting for San Isidro House Hunter.

This program does not scrape real-estate portals. It is the local, persistent part
of a public-web research workflow: a researcher or Codex automation records facts
from public original listing pages in JSONL, then this tool preserves the evidence,
deduplicates cross-posts, and produces disciplined daily alerts.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import sys
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parent
DEFAULT_DATABASE = ROOT / "data" / "house_hunter.sqlite3"
DEFAULT_CONFIG = ROOT / "config" / "targeting.json"
DEFAULT_LOCAL_SOURCES = ROOT / "config" / "local_sources.json"
ARGENTINA_TZ = ZoneInfo("America/Argentina/Buenos_Aires")

NUMERIC_FIELDS = {"price_usd", "covered_m2", "total_m2", "garden_m2", "expenses_ars", "parking", "units", "bedrooms", "bathrooms"}
BOOLEAN_FIELDS = {
    "private_garden", "private_pool", "pool_potential", "controlled_entrance",
    "owner_direct", "exceptional_layout", "credible_price_path", "high_traffic",
    "large_development", "needs_major_renovation", "security_concern", "flood_risk",
    "micro_location_rejected",
}
TRACKED_FIELDS = {
    "price_usd", "covered_m2", "total_m2", "garden_m2", "bedrooms", "bathrooms",
    "private_garden", "private_pool", "pool_potential", "parking", "units",
    "controlled_entrance", "expenses", "expenses_ars", "condition", "orientation", "property_type",
    "street_quality", "listing_state", "canonical_address",
}
STOP_WORDS = {
    "casa", "venta", "san", "isidro", "con", "para", "una", "del", "las", "los",
    "por", "que", "dos", "tres", "ambientes", "jardin", "pileta", "propiedad",
}


class HunterError(ValueError):
    """An input packet cannot be safely interpreted as evidence."""


def load_dotenv(path: Path | None = None) -> None:
    """Populate os.environ from a local .env file, without overwriting real env vars.

    Credentials live in .env (gitignored) rather than in exported shell variables,
    because a new terminal tab loses exports and a launchd job inherits almost no
    environment at all. An existing environment variable always wins, so a one-off
    override on the command line still works.
    """
    import os

    env_path = path or (ROOT / ".env")
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def local_now() -> datetime:
    return datetime.now(ARGENTINA_TZ)


def iso_now() -> str:
    return local_now().replace(microsecond=0).isoformat()


def text_key(value: Any) -> str:
    if value is None:
        return ""
    folded = unicodedata.normalize("NFKD", str(value)).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", " ", folded.lower()).strip()


def compact_key(value: Any) -> str:
    return re.sub(r"\s+", "", text_key(value))


def normal_url(value: Any) -> str:
    raw = str(value or "").strip()
    parsed = urlsplit(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise HunterError("url must be a public http(s) original listing URL")
    # Query parameters are commonly tracking IDs and make a poor identity key.
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path.rstrip("/"), "", ""))


def parse_number(value: Any, field: str) -> int | float | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        raise HunterError(f"{field} must be a number, not a boolean")
    if isinstance(value, (int, float)):
        return value
    cleaned = str(value).strip().replace(" ", "")
    # Argentina commonly formats 380 thousand as 380.000 or 380,000.
    if re.fullmatch(r"\d{1,3}([.,]\d{3})+", cleaned):
        cleaned = re.sub(r"[.,]", "", cleaned)
    elif "," in cleaned and "." not in cleaned:
        cleaned = cleaned.replace(",", ".")
    try:
        return float(cleaned) if "." in cleaned else int(cleaned)
    except ValueError as exc:
        raise HunterError(f"{field} must be numeric or null") from exc


def parse_bool(value: Any, field: str) -> bool | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        clean = text_key(value)
        if clean in {"true", "yes", "si", "1"}:
            return True
        if clean in {"false", "no", "0"}:
            return False
    raise HunterError(f"{field} must be true, false, or null")


def clean_text(value: Any) -> str | None:
    if value is None:
        return None
    value = re.sub(r"\s+", " ", str(value)).strip()
    return value or None


def clean_list(value: Any, field: str) -> list[str]:
    if value in (None, ""):
        return []
    if not isinstance(value, list):
        raise HunterError(f"{field} must be an array")
    return [item for raw in value if (item := clean_text(raw))]


def clean_evidence(value: Any) -> dict[str, str]:
    if value in (None, ""):
        return {}
    if not isinstance(value, dict):
        raise HunterError("evidence must be an object keyed by field name")
    return {str(key): item for key, raw in value.items() if (item := clean_text(raw))}


def clean_negotiation(value: Any) -> dict[str, Any] | None:
    """Keep pricing estimates only when their factual basis is recorded."""
    if value in (None, ""):
        return None
    if not isinstance(value, dict):
        raise HunterError("negotiation must be an object or null")
    result: dict[str, Any] = {"basis": clean_text(value.get("basis"))}
    for field in {"realistic_target_usd", "suggested_opening_offer_usd", "maximum_justified_price_usd"}:
        result[field] = parse_number(value.get(field), field)
    price_range = value.get("probable_range_usd")
    if price_range in (None, ""):
        result["probable_range_usd"] = None
    elif isinstance(price_range, list) and len(price_range) == 2:
        low = parse_number(price_range[0], "probable_range_usd[0]")
        high = parse_number(price_range[1], "probable_range_usd[1]")
        if low is None or high is None or low > high:
            raise HunterError("probable_range_usd must be [low, high]")
        result["probable_range_usd"] = [low, high]
    else:
        raise HunterError("probable_range_usd must be [low, high] or null")
    if any(known(result.get(field)) for field in result if field != "basis") and not result["basis"]:
        raise HunterError("negotiation estimates require a factual basis")
    return result


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def read_config(path: Path) -> dict[str, Any]:
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise HunterError(f"configuration not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise HunterError(f"configuration is not valid JSON: {path}") from exc
    required = {"price_ceiling_usd", "priority_a_terms", "priority_b_terms", "exclusion_terms"}
    missing = required - set(config)
    if missing:
        raise HunterError(f"configuration missing: {', '.join(sorted(missing))}")
    return config


def read_local_sources(path: Path) -> dict[str, Any]:
    try:
        sources = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise HunterError(f"local-source configuration not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise HunterError(f"local-source configuration is not valid JSON: {path}") from exc
    if not isinstance(sources.get("sources"), list) or not isinstance(sources.get("pre_market_terms"), list):
        raise HunterError("local-source configuration requires sources and pre_market_terms arrays")
    return sources


def normalise_record(raw: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise HunterError("each JSONL line must be an object")
    source = clean_text(raw.get("source"))
    if not source:
        raise HunterError("source is required")
    record: dict[str, Any] = {"source": source, "url": normal_url(raw.get("url"))}
    for field in NUMERIC_FIELDS:
        record[field] = parse_number(raw.get(field), field)
    for field in BOOLEAN_FIELDS:
        record[field] = parse_bool(raw.get(field), field)
    for field in {
        "canonical_address", "source_listing_id", "expenses", "condition", "orientation",
        "zone", "property_type", "street_quality", "publication_date", "description",
        "broker", "broker_phone", "listing_state", "fit_notes", "negotiation_notes",
        "market_stage", "observed_at",
    }:
        record[field] = clean_text(raw.get(field))
    record["photo_urls"] = [normal_url(item) for item in clean_list(raw.get("photo_urls"), "photo_urls")]
    record["distinctive_features"] = clean_list(raw.get("distinctive_features"), "distinctive_features")
    record["risks"] = clean_list(raw.get("risks"), "risks")
    record["evidence"] = clean_evidence(raw.get("evidence"))
    record["negotiation"] = clean_negotiation(raw.get("negotiation"))
    record["address_key"] = text_key(record["canonical_address"])
    record["broker_phone_key"] = re.sub(r"\D", "", record["broker_phone"] or "")
    record["description_key"] = text_key(record["description"])
    record["observed_at"] = record["observed_at"] or iso_now()
    return record


def connect(database: Path) -> sqlite3.Connection:
    database.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(database)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def initialise(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS listings (
            id INTEGER PRIMARY KEY,
            canonical_address TEXT,
            address_key TEXT NOT NULL DEFAULT '',
            first_seen TEXT NOT NULL,
            last_seen TEXT NOT NULL,
            review_state TEXT NOT NULL DEFAULT 'active',
            current_data_json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS aliases (
            kind TEXT NOT NULL,
            value TEXT NOT NULL,
            listing_id INTEGER NOT NULL REFERENCES listings(id),
            PRIMARY KEY (kind, value)
        );
        CREATE TABLE IF NOT EXISTS observations (
            id INTEGER PRIMARY KEY,
            listing_id INTEGER REFERENCES listings(id),
            source TEXT NOT NULL,
            url TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            classification TEXT NOT NULL CHECK (classification IN
                ('NEW', 'MATERIAL_CHANGE', 'OLD', 'DUPLICATE', 'UNCERTAIN')),
            match_score REAL,
            match_reasons_json TEXT NOT NULL,
            changes_json TEXT NOT NULL,
            fit_score INTEGER NOT NULL,
            priority TEXT,
            snapshot_hash TEXT NOT NULL,
            snapshot_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS observations_seen_idx ON observations(observed_at);
        CREATE INDEX IF NOT EXISTS observations_listing_idx ON observations(listing_id, observed_at);
        CREATE TABLE IF NOT EXISTS possible_matches (
            observation_id INTEGER NOT NULL REFERENCES observations(id),
            listing_id INTEGER NOT NULL REFERENCES listings(id),
            score REAL NOT NULL,
            reasons_json TEXT NOT NULL,
            PRIMARY KEY (observation_id, listing_id)
        );
        """
    )
    conn.commit()


def infer_priority(record: dict[str, Any], config: dict[str, Any]) -> tuple[str | None, str | None]:
    haystack = text_key(" ".join(filter(None, [record.get("canonical_address"), record.get("zone"), record.get("description")])))
    for term in config["priority_a_terms"]:
        if text_key(term) in haystack:
            return "A", term
    for term in config["priority_b_terms"]:
        if text_key(term) in haystack:
            return "B", term
    return None, None


def known(value: Any) -> bool:
    return value not in (None, "", [], {})


def answer_bool(value: bool | None, evidence: str | None = None) -> str:
    if value is None:
        return "Not verified."
    suffix = f" — {evidence}" if evidence else " — listing claim"
    return ("Yes" if value else "No") + suffix


def house_like(record: dict[str, Any]) -> str:
    property_key = text_key(record.get("property_type"))
    if not property_key:
        return "Not verified."
    if any(word in property_key for word in ("ph", "apartment", "departamento")):
        return "No — listing type does not read as a private house."
    if any(word in property_key for word in ("casa", "house")):
        return "Likely — inference from the stated property type; confirm on visit."
    return "Not verified."


def evaluate(record: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    """Score fit without promoting a claim beyond the supporting listing evidence."""
    priority, matched_term = infer_priority(record, config)
    score = 0
    positives: list[str] = []
    risks: list[str] = list(record.get("risks") or [])
    exclusions: list[str] = []
    words = text_key(" ".join(filter(None, [record.get("property_type"), record.get("description")])))

    if priority == "A":
        score += 25
        positives.append("Priority A micro-area match")
    elif priority == "B":
        score += 10
        positives.append("Priority B micro-area match")
    else:
        risks.append("Preferred micro-area: Not verified.")

    units = record.get("units")
    max_units = config.get("maximum_small_development_units", 8)
    if isinstance(units, (int, float)) and 2 <= units <= max_units:
        score += 15
        positives.append(f"small development ({int(units)} homes)")
    elif units is None:
        risks.append("Number of homes/units: Not verified.")
    elif units > max_units:
        exclusions.append(f"{int(units)} units; not the desired small-development scale")

    if record.get("controlled_entrance") is True:
        score += 9
        positives.append("controlled entrance")
    if record.get("private_garden") is True:
        score += 15
        positives.append("private garden")
    elif record.get("private_garden") is None:
        risks.append("Private garden: Not verified.")
    garden_m2 = record.get("garden_m2")
    min_garden = config.get("minimum_private_garden_m2")
    if isinstance(garden_m2, (int, float)) and min_garden and garden_m2 < min_garden:
        exclusions.append(f"private garden is only {garden_m2:,.0f} m² (minimum is {min_garden:,.0f} m²)")
    if record.get("private_pool") is True:
        score += 10
        positives.append("private pool")
    elif record.get("pool_potential") is True:
        score += 6
        positives.append("pool potential (listing claim)")

    covered = record.get("covered_m2")
    if isinstance(covered, (int, float)) and 150 <= covered <= 200:
        score += 10
        positives.append("150–200 m² covered")
    elif isinstance(covered, (int, float)) and covered >= 150:
        score += 5
        positives.append("150+ m² covered")
    total = record.get("total_m2")
    if isinstance(total, (int, float)) and total >= 250:
        score += 8
        positives.append("250+ m² total/lot")
    bedrooms = record.get("bedrooms")
    if isinstance(bedrooms, (int, float)) and bedrooms >= 3:
        score += 8
        positives.append("3+ bedrooms")
    elif bedrooms == 2 and record.get("exceptional_layout") is True:
        score += 6
        positives.append("2 bedrooms with stated exceptional layout")
    if isinstance(record.get("parking"), (int, float)) and record["parking"] >= 2:
        score += 4
        positives.append("2+ parking spaces")

    condition_key = text_key(record.get("condition"))
    if any(word in condition_key for word in ("new", "nuevo", "excellent", "excelente", "renovated", "reciclado")):
        score += 8
        positives.append("new, renovated, or excellent condition (listing claim)")
    if record.get("needs_major_renovation") is True:
        exclusions.append("major renovation indicated")
    if record.get("high_traffic") is True:
        exclusions.append("high-traffic street indicated")
    if record.get("large_development") is True:
        exclusions.append("large development indicated")
    if record.get("security_concern") is True:
        exclusions.append("security concern indicated")
    if record.get("micro_location_rejected") is True:
        exclusions.append("micro-location is in the buyer's rejected area")
    for term in config.get("rejected_micro_location_terms", []):
        if text_key(term) and text_key(term) in text_key(" ".join(filter(None, [record.get("canonical_address"), record.get("description")]))):
            exclusions.append(f"buyer-rejected micro-location: {term}")

    price = record.get("price_usd")
    ceiling = config["price_ceiling_usd"]
    if isinstance(price, (int, float)):
        if price <= 300000:
            score += 15
            positives.append("within ideal price band")
        elif price <= 350000:
            score += 12
            positives.append("within strong price band")
        elif price <= ceiling:
            score += 10
            positives.append("within hard price ceiling")
        elif record.get("credible_price_path") is True:
            score += 6
            positives.append("price path below ceiling has stated support")
        else:
            risks.append(f"Asking price exceeds USD {ceiling:,.0f} with no documented path below it.")
    else:
        risks.append("Asking price: Not verified.")
    if not known(record.get("expenses")):
        risks.append("Monthly expenses: Not verified.")
    expenses_ars = record.get("expenses_ars")
    if isinstance(expenses_ars, (int, float)):
        if expenses_ars >= config.get("expenses_reject_ars", float("inf")):
            exclusions.append(f"monthly expenses are ARS {expenses_ars:,.0f}, above the buyer's ceiling")
        elif expenses_ars >= config.get("expenses_review_ars", float("inf")):
            risks.append(f"monthly expenses are ARS {expenses_ars:,.0f}; verify whether they are justified")
    if "bajo san isidro" in text_key(record.get("canonical_address")) and record.get("flood_risk") is None:
        risks.append("Flood/sudestada risk in Bajo: Not verified.")

    for term, penalty in config["exclusion_terms"].items():
        if text_key(term) in words:
            exclusions.append(f"exclusion term in listing: {term}")
            score -= int(penalty)
    score = max(0, min(100, score))
    return {
        "score": score,
        "priority": priority,
        "priority_term": matched_term,
        "positives": positives,
        "risks": list(dict.fromkeys(risks)),
        "exclusions": list(dict.fromkeys(exclusions)),
    }


def listing_from_row(row: sqlite3.Row) -> dict[str, Any]:
    return json.loads(row["current_data_json"])


def fetch_listing(conn: sqlite3.Connection, listing_id: int) -> tuple[sqlite3.Row, dict[str, Any]]:
    row = conn.execute("SELECT * FROM listings WHERE id = ?", (listing_id,)).fetchone()
    if row is None:
        raise HunterError(f"history corruption: listing {listing_id} is missing")
    return row, listing_from_row(row)


def direct_match(conn: sqlite3.Connection, record: dict[str, Any]) -> tuple[int | None, list[str]]:
    keys: list[tuple[str, str, str]] = [("url", record["url"], "same canonical original URL")]
    source_id = compact_key(record.get("source_listing_id"))
    if source_id:
        keys.insert(0, ("source_id", f"{text_key(record['source'])}|{source_id}", "same source listing ID"))
    listing_ids: set[int] = set()
    reasons: list[str] = []
    for kind, value, reason in keys:
        row = conn.execute("SELECT listing_id FROM aliases WHERE kind = ? AND value = ?", (kind, value)).fetchone()
        if row:
            listing_ids.add(row["listing_id"])
            reasons.append(reason)
    if len(listing_ids) > 1:
        return None, ["conflicting stored identity aliases; manual review required"]
    return (next(iter(listing_ids)) if listing_ids else None), reasons


def tokens(value: Any) -> set[str]:
    return {token for token in text_key(value).split() if len(token) > 2 and token not in STOP_WORDS}


def close_number(left: Any, right: Any, tolerance: float = 0.15) -> bool:
    if not isinstance(left, (int, float)) or not isinstance(right, (int, float)) or left <= 0 or right <= 0:
        return False
    return abs(left - right) / max(left, right) <= tolerance


def fuzzy_score(record: dict[str, Any], prior: dict[str, Any]) -> tuple[float, list[str]]:
    score = 0.0
    reasons: list[str] = []
    if record["address_key"] and record["address_key"] == prior.get("address_key"):
        score += 0.55
        reasons.append("same normalized address")
    phone = record.get("broker_phone_key")
    if phone and phone == prior.get("broker_phone_key"):
        score += 0.20
        reasons.append("same broker phone")
    common_photos = set(record.get("photo_urls") or []) & set(prior.get("photo_urls") or [])
    if common_photos:
        score += 0.30
        reasons.append("shared public photo URL")
    if close_number(record.get("covered_m2"), prior.get("covered_m2")):
        score += 0.08
        reasons.append("similar covered area")
    if close_number(record.get("total_m2"), prior.get("total_m2")):
        score += 0.08
        reasons.append("similar total area")
    if record.get("bedrooms") is not None and record.get("bedrooms") == prior.get("bedrooms"):
        score += 0.06
        reasons.append("same bedroom count")
    if record.get("broker") and text_key(record["broker"]) == text_key(prior.get("broker")):
        score += 0.06
        reasons.append("same broker")
    old_description, new_description = tokens(prior.get("description")), tokens(record.get("description"))
    if old_description and new_description:
        overlap = len(old_description & new_description) / len(old_description | new_description)
        if overlap >= 0.35:
            score += min(0.18, overlap * 0.30)
            reasons.append("substantial description overlap")
    old_features, new_features = tokens(" ".join(prior.get("distinctive_features") or [])), tokens(" ".join(record.get("distinctive_features") or []))
    if old_features and new_features and old_features & new_features:
        score += 0.12
        reasons.append("shared distinctive features")
    return min(score, 1.0), reasons


def fuzzy_match(conn: sqlite3.Connection, record: dict[str, Any]) -> tuple[int | None, float, list[str]]:
    best_id: int | None = None
    best_score = 0.0
    best_reasons: list[str] = []
    for row in conn.execute("SELECT * FROM listings"):
        prior = listing_from_row(row)
        score, reasons = fuzzy_score(record, prior)
        if score > best_score:
            best_id, best_score, best_reasons = row["id"], score, reasons
    return best_id, best_score, best_reasons


def materially_changed(old: dict[str, Any], new: dict[str, Any]) -> dict[str, dict[str, Any]]:
    changes: dict[str, dict[str, Any]] = {}
    for field in TRACKED_FIELDS:
        before, after = old.get(field), new.get(field)
        if not known(after) or before == after:
            continue
        if field == "price_usd" and isinstance(before, (int, float)) and isinstance(after, (int, float)):
            if abs(after - before) < max(2500, before * 0.01):
                continue
        if field in {"covered_m2", "total_m2", "garden_m2"} and close_number(before, after, 0.08):
            continue
        changes[field] = {"from": before, "to": after}
    return changes


def merge_known(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    merged = dict(old)
    for key, value in new.items():
        if known(value):
            merged[key] = value
    return merged


def add_aliases(conn: sqlite3.Connection, listing_id: int, record: dict[str, Any]) -> None:
    aliases = [("url", record["url"])]
    source_id = compact_key(record.get("source_listing_id"))
    if source_id:
        aliases.append(("source_id", f"{text_key(record['source'])}|{source_id}"))
    for kind, value in aliases:
        conn.execute("INSERT OR IGNORE INTO aliases(kind, value, listing_id) VALUES (?, ?, ?)", (kind, value, listing_id))


def create_listing(conn: sqlite3.Connection, record: dict[str, Any], review_state: str) -> int:
    observed_at = record["observed_at"]
    cursor = conn.execute(
        """INSERT INTO listings(canonical_address, address_key, first_seen, last_seen, review_state, current_data_json)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (record.get("canonical_address"), record["address_key"], observed_at, observed_at, review_state, canonical_json(record)),
    )
    listing_id = int(cursor.lastrowid)
    add_aliases(conn, listing_id, record)
    return listing_id


def record_observation(
    conn: sqlite3.Connection,
    record: dict[str, Any],
    classification: str,
    listing_id: int | None,
    score: float | None,
    reasons: list[str],
    changes: dict[str, Any],
    evaluation: dict[str, Any],
) -> int:
    digest = hashlib.sha256(canonical_json(record).encode("utf-8")).hexdigest()
    cursor = conn.execute(
        """INSERT INTO observations(
            listing_id, source, url, observed_at, classification, match_score, match_reasons_json,
            changes_json, fit_score, priority, snapshot_hash, snapshot_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            listing_id, record["source"], record["url"], record["observed_at"], classification,
            score, canonical_json(reasons), canonical_json(changes), evaluation["score"],
            evaluation["priority"], digest, canonical_json(record),
        ),
    )
    return int(cursor.lastrowid)


def ingest_record(conn: sqlite3.Connection, record: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    evaluation = evaluate(record, config)
    direct_id, direct_reasons = direct_match(conn, record)
    fuzzy_id: int | None = None
    fuzzy = 0.0
    fuzzy_reasons: list[str] = []
    if direct_id is None:
        fuzzy_id, fuzzy, fuzzy_reasons = fuzzy_match(conn, record)

    classification: str
    listing_id: int | None
    match_score: float | None = None
    reasons: list[str] = []
    changes: dict[str, Any] = {}
    possible_match: tuple[int, float, list[str]] | None = None

    if direct_id is not None:
        listing_id = direct_id
        prior_row, prior = fetch_listing(conn, listing_id)
        changes = materially_changed(prior, record)
        same_source = text_key(prior.get("source")) == text_key(record.get("source"))
        classification = "MATERIAL_CHANGE" if same_source and changes else ("OLD" if same_source else "DUPLICATE")
        reasons = direct_reasons
        match_score = 1.0
        if classification in {"OLD", "MATERIAL_CHANGE"}:
            merged = merge_known(prior, record)
            conn.execute(
                """UPDATE listings SET canonical_address = ?, address_key = ?, last_seen = ?,
                   review_state = 'active', current_data_json = ? WHERE id = ?""",
                (merged.get("canonical_address"), merged.get("address_key", ""), record["observed_at"], canonical_json(merged), listing_id),
            )
            add_aliases(conn, listing_id, record)
    elif fuzzy_id is not None and fuzzy >= 0.85:
        listing_id = fuzzy_id
        prior_row, prior = fetch_listing(conn, listing_id)
        changes = materially_changed(prior, record)
        same_source = text_key(prior.get("source")) == text_key(record.get("source"))
        classification = "MATERIAL_CHANGE" if same_source and changes else ("OLD" if same_source else "DUPLICATE")
        reasons = fuzzy_reasons
        match_score = fuzzy
        if classification in {"OLD", "MATERIAL_CHANGE"}:
            merged = merge_known(prior, record)
            conn.execute(
                """UPDATE listings SET canonical_address = ?, address_key = ?, last_seen = ?,
                   review_state = 'active', current_data_json = ? WHERE id = ?""",
                (merged.get("canonical_address"), merged.get("address_key", ""), record["observed_at"], canonical_json(merged), listing_id),
            )
            add_aliases(conn, listing_id, record)
    elif fuzzy_id is not None and fuzzy >= 0.60:
        classification = "UNCERTAIN"
        listing_id = create_listing(conn, record, "needs_review")
        match_score, reasons = fuzzy, fuzzy_reasons
        possible_match = (fuzzy_id, fuzzy, fuzzy_reasons)
    else:
        classification = "NEW"
        listing_id = create_listing(conn, record, "active")

    observation_id = record_observation(conn, record, classification, listing_id, match_score, reasons, changes, evaluation)
    if possible_match:
        conn.execute(
            "INSERT INTO possible_matches(observation_id, listing_id, score, reasons_json) VALUES (?, ?, ?, ?)",
            (observation_id, possible_match[0], possible_match[1], canonical_json(possible_match[2])),
        )
    conn.commit()
    return {
        "observation_id": observation_id,
        "listing_id": listing_id,
        "classification": classification,
        "fit_score": evaluation["score"],
        "priority": evaluation["priority"],
        "changes": changes,
        "match_score": match_score,
    }


def load_packet(path: Path) -> Iterable[tuple[int, dict[str, Any]]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError as exc:
        raise HunterError(f"research packet not found: {path}") from exc
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            raise HunterError(f"{path}:{line_number}: invalid JSON") from exc
        # normalise_record is called here rather than at the call site, so its failures
        # must carry the line number too. Without this, a single malformed field aborts
        # the whole packet with no indication of WHICH line to fix, and the packet is
        # appended to daily, so the poison persists across runs.
        try:
            record = normalise_record(raw)
        except HunterError as exc:
            raise HunterError(f"{path}:{line_number}: {exc}") from exc
        yield line_number, record


def is_meaningful(record: dict[str, Any], evaluation: dict[str, Any], config: dict[str, Any]) -> bool:
    if not record.get("canonical_address"):
        return False
    price = record.get("price_usd")
    price_ok = isinstance(price, (int, float)) and price <= config["price_ceiling_usd"]
    investigate = bool(
        isinstance(price, (int, float))
        and price <= config.get("price_investigate_ceiling_usd", config["price_ceiling_usd"])
        and evaluation["priority"] == "A"
        and evaluation["score"] >= 70
    )
    pre_market = text_key(record.get("market_stage")) in {"pre market", "premarket", "coming soon", "proximamente"}
    price_ok = price_ok or record.get("credible_price_path") is True or investigate
    if pre_market and evaluation["priority"] == "A" and evaluation["score"] >= 65:
        price_ok = True
    if not price_ok or evaluation["exclusions"]:
        return False
    return (evaluation["priority"] == "A" and evaluation["score"] >= 50) or (
        evaluation["priority"] == "B" and evaluation["score"] >= 68
    )


def requires_price_path_investigation(record: dict[str, Any], config: dict[str, Any]) -> bool:
    price = record.get("price_usd")
    return bool(
        isinstance(price, (int, float))
        and price > config["price_ceiling_usd"]
        and record.get("credible_price_path") is not True
    )


def is_pre_market(record: dict[str, Any], evaluation: dict[str, Any]) -> bool:
    return bool(
        text_key(record.get("market_stage")) in {"pre market", "premarket", "coming soon", "proximamente"}
        and evaluation["priority"] == "A"
        and evaluation["score"] >= 65
    )


def is_act_now(record: dict[str, Any], evaluation: dict[str, Any], config: dict[str, Any]) -> bool:
    covered = record.get("covered_m2")
    units = record.get("units")
    price = record.get("price_usd")
    price_ok = (isinstance(price, (int, float)) and price <= config["price_ceiling_usd"]) or record.get("credible_price_path") is True
    condition = text_key(record.get("condition"))
    condition_ok = any(word in condition for word in ("new", "nuevo", "excellent", "excelente", "renovated", "reciclado"))
    return bool(
        evaluation["priority"] == "A"
        and record.get("canonical_address")
        and isinstance(units, (int, float)) and 2 <= units <= 8
        and record.get("controlled_entrance") is True
        and record.get("private_garden") is True
        and (record.get("private_pool") is True or record.get("pool_potential") is True)
        and (isinstance(covered, (int, float)) and covered >= 150 or record.get("exceptional_layout") is True)
        and price_ok
        and condition_ok
        and not evaluation["exclusions"]
    )


def format_money(value: Any) -> str:
    return f"USD {value:,.0f}" if isinstance(value, (int, float)) else "No verificado."


def format_ars(value: Any) -> str:
    return f"ARS {value:,.0f}" if isinstance(value, (int, float)) else "No verificado."


def format_units(value: Any, noun: str) -> str:
    return f"{int(value) if isinstance(value, (int, float)) else value} {noun}" if value is not None else "Not verified."


def lasalle_test(record: dict[str, Any], evaluation: dict[str, Any]) -> list[str]:
    units = record.get("units")
    unit_answer = f"{int(units)} homes/units — listing claim" if isinstance(units, (int, float)) else "Not verified."
    street = record.get("street_quality")
    street_answer = f"{street} — listing claim" if street else "Not verified."
    proximity = f"Yes — matched {evaluation['priority']} priority term: {evaluation['priority_term']}." if evaluation["priority"] else "Not verified."
    wife_fit = "Likely — inference from the documented fit factors; buyer preference is not independently verified." if evaluation["score"] >= 65 else "Not verified."
    return [
        f"1. Number of homes/units: {unit_answer}",
        f"2. Controlled entrance: {answer_bool(record.get('controlled_entrance'), record['evidence'].get('controlled_entrance'))}",
        f"3. Private garden: {answer_bool(record.get('private_garden'), record['evidence'].get('private_garden'))}",
        f"4. Private pool: {answer_bool(record.get('private_pool'), record['evidence'].get('private_pool'))}",
        f"5. Monthly expenses: {record.get('expenses') or 'Not verified.'}",
        f"6. Private-house feel: {house_like(record)}",
        f"7. Quiet, attractive, residential street: {street_answer}",
        f"8. Near preferred micro-area: {proximity}",
        f"9. Likely wife appeal versus Lasalle Chico: {wife_fit}",
    ]


def round_money(value: float, increment: int) -> int:
    return int(round(value / increment) * increment)


def negotiation_report(record: dict[str, Any], config: dict[str, Any]) -> str:
    estimate = record.get("negotiation")
    if not estimate:
        asking = record.get("price_usd")
        if isinstance(asking, (int, float)):
            heuristic = config["negotiation_heuristic"]
            increment = int(heuristic["round_to_usd"])
            closing_low, closing_high = [round_money(asking * value, increment) for value in heuristic["close_range_multipliers"]]
            opening_low, opening_high = [round_money(asking * value, increment) for value in heuristic["opening_range_multipliers"]]
            maximum = min(closing_high, int(config["price_ceiling_usd"]))
            return "\n".join(
                [
                    "- Base: inferencia inicial del agente; no reemplaza comparables ni motivación del vendedor.",
                    f"- Precio estimado de cierre: USD {closing_low:,.0f}–USD {closing_high:,.0f}",
                    f"- Oferta inicial sugerida: USD {opening_low:,.0f}–USD {opening_high:,.0f}",
                    f"- Máximo justificado: USD {maximum:,.0f}",
                ]
            )
        missing = "No estimado: faltan precio pedido y comparables verificables."
        return "\n".join(
            [
                f"- Precio estimado de cierre: {missing}",
                f"- Oferta inicial sugerida: {missing}",
                f"- Máximo justificado: {missing}",
            ]
        )
    price_range = estimate.get("probable_range_usd")
    range_text = f"USD {price_range[0]:,.0f}–USD {price_range[1]:,.0f}" if price_range else "No estimado."
    # "Base declarada", not "Base verificada". `basis` is free text supplied alongside the
    # figures it justifies, so nothing here has been independently verified. AGENTS.md
    # requires facts, agent claims, and inferences to be labelled distinctly, and every
    # other unconfirmed value in this alert prints "No verificado." -- claiming
    # verification for the one block a model could author would invert that.
    return "\n".join(
        [
            f"- Base declarada (no verificada): {estimate.get('basis') or 'No declarada.'}",
            f"- Precio estimado de cierre: {range_text}",
            f"- Objetivo realista: {format_money(estimate.get('realistic_target_usd'))}",
            f"- Oferta inicial sugerida: {format_money(estimate.get('suggested_opening_offer_usd'))}",
            f"- Máximo justificado: {format_money(estimate.get('maximum_justified_price_usd'))}",
        ]
    )


def candidate_features(record: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    if isinstance(record.get("covered_m2"), (int, float)):
        lines.append(f"{record['covered_m2']:,.0f} m² cubiertos")
    if isinstance(record.get("total_m2"), (int, float)):
        lines.append(f"{record['total_m2']:,.0f} m² total/lote (según aviso)")
    if isinstance(record.get("bedrooms"), (int, float)):
        lines.append(f"{record['bedrooms']:,.0f} dormitorios")
    if record.get("private_garden") is True:
        garden = f" de {record['garden_m2']:,.0f} m²" if isinstance(record.get("garden_m2"), (int, float)) else ""
        lines.append(f"jardín privado{garden}")
    if record.get("private_pool") is True:
        lines.append("pileta privada")
    elif record.get("pool_potential") is True:
        lines.append("potencial de pileta (según aviso)")
    if isinstance(record.get("parking"), (int, float)):
        lines.append(f"{record['parking']:,.0f} cocheras")
    if isinstance(record.get("units"), (int, float)):
        lines.append(f"complejo de {record['units']:,.0f} casas/unidades")
    if record.get("controlled_entrance") is True:
        lines.append("portón/acceso controlado")
    if record.get("expenses"):
        lines.append(f"expensas: {record['expenses']}")
    if record.get("condition"):
        lines.append(f"estado: {record['condition']} (según aviso)")
    if record.get("street_quality"):
        lines.append(f"calle: {record['street_quality']} (según aviso)")
    return lines


def lasalle_rating(record: dict[str, Any], evaluation: dict[str, Any], config: dict[str, Any]) -> int:
    units = record.get("units")
    condition = text_key(record.get("condition"))
    street = text_key(record.get("street_quality"))
    good_expenses = isinstance(record.get("expenses_ars"), (int, float)) and record["expenses_ars"] < config.get("expenses_review_ars", float("inf"))
    checks = [
        isinstance(units, (int, float)) and 2 <= units <= config.get("maximum_small_development_units", 8),
        record.get("controlled_entrance") is True,
        record.get("private_garden") is True,
        record.get("private_pool") is True or record.get("pool_potential") is True,
        good_expenses,
        house_like(record).startswith("Likely"),
        "quiet" in street or "tranquila" in street or "residencial" in street,
        evaluation["priority"] == "A",
        (isinstance(record.get("bedrooms"), (int, float)) and record["bedrooms"] >= 3) or record.get("exceptional_layout") is True,
        any(word in condition for word in ("new", "nuevo", "excellent", "excelente", "renovated", "reciclado")),
    ]
    return sum(checks)


def format_alert(row: sqlite3.Row, listing: sqlite3.Row, config: dict[str, Any]) -> str:
    record = json.loads(row["snapshot_json"])
    evaluation = evaluate(record, config)
    act_now = is_act_now(record, evaluation, config)
    pre_market = is_pre_market(record, evaluation)
    price_investigation = requires_price_path_investigation(record, config)
    title = "🚨 ACT NOW" if act_now else ("🟣 PRE-MARKET — CONTACTAR" if pre_market else ("🟡 INVESTIGAR PRECIO" if price_investigation else "⚡ VER HOY"))
    changes = json.loads(row["changes_json"])
    features = candidate_features(record)
    risks = evaluation["risks"] + evaluation["exclusions"]
    if changes:
        risks.insert(0, "Material changes: " + "; ".join(f"{field} changed" for field in changes))
    if price_investigation:
        risks.insert(0, "Precio por encima del techo: investigar comparables y motivación antes de descartar.")
    risk_text = "\n".join(f"- {risk}" for risk in dict.fromkeys(risks)) or "- No verificado."
    negotiation = negotiation_report(record, config)
    action = "👉 LLAMAR AHORA" if act_now else ("👉 CONTACTAR HOY" if pre_market else "👉 INVESTIGAR / VISITAR HOY")
    feature_text = "\n".join(f"- {item}" for item in features) or "- Datos físicos: No verificados."
    return "\n".join(
        [
            title,
            f"{record.get('canonical_address') or 'Dirección no verificada.'}",
            f"Precio pedido: {format_money(record.get('price_usd'))}",
            f"Publicado: {record.get('publication_date') or 'No verificado.'} · Detectada: {listing['first_seen']}",
            feature_text,
            f"Lasalle Chico test: {lasalle_rating(record, evaluation, config)}/10",
            "Fuente: " + record["source"],
            "Publicación original: " + record["url"],
            "Riesgos / pendientes:",
            risk_text,
            "Negociación:",
            negotiation,
            action,
        ]
    )


def report(conn: sqlite3.Connection, config: dict[str, Any], since: str | None) -> str:
    since = since or local_now().date().isoformat()
    rows = conn.execute(
        """
        WITH ranked AS (
          SELECT o.*, ROW_NUMBER() OVER (PARTITION BY o.listing_id ORDER BY o.observed_at DESC, o.id DESC) AS rank
          FROM observations o
          WHERE o.observed_at >= ? AND o.classification IN ('NEW', 'MATERIAL_CHANGE')
        )
        SELECT ranked.*, l.first_seen FROM ranked
        JOIN listings l ON l.id = ranked.listing_id
        WHERE ranked.rank = 1
        ORDER BY ranked.fit_score DESC, ranked.observed_at DESC
        """,
        (since,),
    ).fetchall()
    material: list[str] = []
    for row in rows:
        record = json.loads(row["snapshot_json"])
        evaluation = evaluate(record, config)
        if is_meaningful(record, evaluation, config):
            material.append(format_alert(row, row, config))
    return "\n\n".join(material) if material else "NO CHANGE — nothing worth visiting today."


def research_plan(config: dict[str, Any], local_sources: dict[str, Any], mode: str) -> str:
    locations_a = ' OR '.join(f'"{term}"' for term in config["priority_a_terms"][:7])
    locations_b = ' OR '.join(f'"{term}"' for term in config["priority_b_terms"])
    common = 'venta casa (jardin OR jardín) (pileta OR piscina OR alberca)'
    patterns = '"condominio" OR "complejo" OR "solo 6 unidades" OR "barrio chico" OR "acceso controlado"'
    pre_market = " OR ".join(f'\"{term}\"' for term in local_sources["pre_market_terms"])
    source_lines = [f"- {source['name']}: {source['website']} ({source['focus']})" for source in local_sources["sources"]]
    if mode == "change":
        return "\n".join(
            [
                "PASADA RÁPIDA DE CAMBIOS — no hagas una búsqueda exhaustiva.",
                "Revisa sólo avisos nuevos desde la pasada anterior, rebajas de precio y altas en las fuentes públicas ya vigiladas.",
                "Primero compara URL, dirección, fotos y precio con la historia local. No profundices un candidato hasta que pase el filtro inicial.",
                "", "Fuentes locales a revisar públicamente:", *source_lines,
                "", "Búsqueda focalizada Priority A:",
                f"- ({locations_a}) {common} ({patterns})",
                "- Revisa también los resultados públicos marcados como nuevo, reciente, hoy o próximamente.",
                "", "Si aparece un candidato con encaje alto, recién entonces ejecuta el análisis profundo de ubicación, riesgo, comparables y negociación.",
            ]
        )
    lines = [
        "PASADA PROFUNDA — usa sólo páginas públicas originales; no automatices portales ni eludas controles.",
        "", "Priority A, alta señal:",
        f"- ({locations_a}) {common} ({patterns})",
        f"- ({locations_a}) venta casa " + '"jardín" "pileta" 150 m2',
        "", "Priority B, sólo excepcional:",
        f"- ({locations_b}) {common} ({patterns})",
        "", "Repite Priority A por dominio público:",
        "- site:zonaprop.com.ar", "- site:argenprop.com", "- site:inmuebles.mercadolibre.com.ar OR site:casa.mercadolibre.com.ar",
        "- site:properati.com.ar (only if publicly reachable)",
        "", "Pre-market: vigila altas tempranas, desarrollos y 'próximamente' en estas fuentes locales:",
        *source_lines,
        "", "Consulta pre-market por fuente: ",
        f"- ({locations_a}) ({pre_market}) venta casa",
        "", "Por cada resultado prometedor, registra sólo hechos respaldados en research/YYYY-MM-DD.jsonl y ejecuta ingest + report.",
    ]
    return "\n".join(lines)


def command_init(args: argparse.Namespace) -> int:
    with connect(Path(args.database)) as conn:
        initialise(conn)
    print(f"Initialized persistent history at {args.database}")
    return 0


def command_ingest(args: argparse.Namespace) -> int:
    config = read_config(Path(args.config))
    results: list[dict[str, Any]] = []
    with connect(Path(args.database)) as conn:
        initialise(conn)
        for line_number, record in load_packet(Path(args.input)):
            try:
                result = ingest_record(conn, record, config)
            except HunterError as exc:
                raise HunterError(f"{args.input}:{line_number}: {exc}") from exc
            results.append(result)
    if not args.quiet:
        counts: dict[str, int] = {}
        for result in results:
            counts[result["classification"]] = counts.get(result["classification"], 0) + 1
        print(canonical_json({"imported": len(results), "classifications": counts, "results": results}))
    return 0


def command_report(args: argparse.Namespace) -> int:
    config = read_config(Path(args.config))
    with connect(Path(args.database)) as conn:
        initialise(conn)
        print(report(conn, config, args.since))
    return 0


def command_research_plan(args: argparse.Namespace) -> int:
    print(research_plan(read_config(Path(args.config)), read_local_sources(Path(args.sources)), args.mode))
    return 0


def command_history(args: argparse.Namespace) -> int:
    with connect(Path(args.database)) as conn:
        initialise(conn)
        rows = conn.execute(
            """SELECT o.observed_at, o.classification, o.fit_score, o.priority, l.canonical_address, o.url
               FROM observations o LEFT JOIN listings l ON l.id = o.listing_id
               ORDER BY o.id DESC LIMIT ?""",
            (args.limit,),
        ).fetchall()
    for row in rows:
        print(canonical_json(dict(row)))
    return 0


def command_review(args: argparse.Namespace) -> int:
    """Show matches withheld from alerts because the identity was not safe to infer."""
    with connect(Path(args.database)) as conn:
        initialise(conn)
        rows = conn.execute(
            """SELECT pm.observation_id, pm.score, pm.reasons_json,
                      o.listing_id AS candidate_listing_id, o.url AS candidate_url,
                      candidate.canonical_address AS candidate_address,
                      pm.listing_id AS possible_duplicate_listing_id,
                      possible.canonical_address AS possible_duplicate_address
               FROM possible_matches pm
               JOIN observations o ON o.id = pm.observation_id
               JOIN listings candidate ON candidate.id = o.listing_id
               JOIN listings possible ON possible.id = pm.listing_id
               ORDER BY pm.score DESC, pm.observation_id DESC"""
        ).fetchall()
    for row in rows:
        item = dict(row)
        item["reasons"] = json.loads(item.pop("reasons_json"))
        print(canonical_json(item))
    return 0


def command_resolve(args: argparse.Namespace) -> int:
    """Make an explicit human decision on an otherwise ambiguous cross-post."""
    with connect(Path(args.database)) as conn:
        initialise(conn)
        row = conn.execute(
            """SELECT pm.listing_id AS possible_duplicate_listing_id, o.listing_id AS candidate_listing_id
               FROM possible_matches pm JOIN observations o ON o.id = pm.observation_id
               WHERE pm.observation_id = ?""",
            (args.observation,),
        ).fetchone()
        if row is None:
            raise HunterError(f"no unresolved possible match for observation {args.observation}")
        candidate_id = row["candidate_listing_id"]
        target_id = row["possible_duplicate_listing_id"]
        if args.decision == "duplicate":
            aliases = conn.execute("SELECT kind, value FROM aliases WHERE listing_id = ?", (candidate_id,)).fetchall()
            for alias in aliases:
                conn.execute(
                    "INSERT OR IGNORE INTO aliases(kind, value, listing_id) VALUES (?, ?, ?)",
                    (alias["kind"], alias["value"], target_id),
                )
            conn.execute("DELETE FROM aliases WHERE listing_id = ?", (candidate_id,))
            conn.execute("UPDATE observations SET listing_id = ? WHERE listing_id = ?", (target_id, candidate_id))
            conn.execute("UPDATE observations SET classification = 'DUPLICATE' WHERE id = ?", (args.observation,))
            conn.execute("UPDATE listings SET review_state = 'resolved_duplicate' WHERE id = ?", (candidate_id,))
        else:
            conn.execute("UPDATE listings SET review_state = 'active' WHERE id = ?", (candidate_id,))
            conn.execute("UPDATE observations SET classification = 'NEW' WHERE id = ?", (args.observation,))
        conn.execute("DELETE FROM possible_matches WHERE observation_id = ?", (args.observation,))
        conn.commit()
    print(f"Resolved observation {args.observation} as {args.decision}.")
    return 0


def parser() -> argparse.ArgumentParser:
    base = argparse.ArgumentParser(description="Persistent, evidence-first San Isidro listing radar")
    base.add_argument("--database", default=str(DEFAULT_DATABASE), help="SQLite history path")
    base.add_argument("--config", default=str(DEFAULT_CONFIG), help="Targeting configuration JSON")
    base.add_argument("--sources", default=str(DEFAULT_LOCAL_SOURCES), help="Local source configuration JSON")
    sub = base.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="create the local history database").set_defaults(handler=command_init)
    ingest = sub.add_parser("ingest", help="import an evidence packet in JSONL")
    ingest.add_argument("--input", required=True, help="research JSONL packet")
    ingest.add_argument("--quiet", action="store_true", help="suppress import summary")
    ingest.set_defaults(handler=command_ingest)
    report_parser = sub.add_parser("report", help="write the daily alert report")
    report_parser.add_argument("--since", help="inclusive ISO date/time; defaults to today in Argentina")
    report_parser.set_defaults(handler=command_report)
    research = sub.add_parser("research-plan", help="print compliant public-web research queries")
    research.add_argument("--mode", choices=("deep", "change"), default="deep")
    research.set_defaults(handler=command_research_plan)
    history = sub.add_parser("history", help="inspect recent classification decisions")
    history.add_argument("--limit", type=int, default=20)
    history.set_defaults(handler=command_history)
    sub.add_parser("review", help="show withheld uncertain duplicate matches").set_defaults(handler=command_review)
    resolve = sub.add_parser("resolve", help="resolve one uncertain possible duplicate")
    resolve.add_argument("--observation", type=int, required=True)
    resolve.add_argument("--decision", choices=("duplicate", "separate"), required=True)
    resolve.set_defaults(handler=command_resolve)
    return base


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        return args.handler(args)
    except HunterError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

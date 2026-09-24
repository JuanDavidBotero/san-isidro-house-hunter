#!/usr/bin/env python3
"""MercadoLibre API client -- DORMANT for discovery. See the verdict below.

!! MercadoLibre's API cannot supply listing data to this project. !!

Measured on 2026-09-23 with a valid user-context token (authorization_code + PKCE,
scopes including `read` and `offline_access`), via tools/ml_probe.py:

    200  /users/me                      identity works
    200  /categories/MLA1459            public metadata, works even unauthenticated
    403  /sites/MLA/search  (all variants: category, q, seller_id, unauthenticated)
    403  /items/{id}                    so no per-listing enrichment either
    403  /users/{self}/items/search
    403  /sites/MLA/domain_discovery/search
    403  /trends/MLA/MLA1459

Every 403 is `PA_UNAUTHORIZED_RESULT_FROM_POLICIES` from their PolicyAgent. Both an
app-context token (client_credentials) and a user-context token are refused, so this is
a deliberate platform restriction on third-party access, not a scope or auth mistake.
Re-running the OAuth flow will not change it.

Discovery therefore lives in sweep.py. This module is kept for three reasons:

  1. `--probe`, `--auth-check`, `--auth-login`, `--try-client-credentials` remain useful
     diagnostics, and re-verify cheaply if MercadoLibre ever reopens access.
  2. `packet_path()`, `write_packet()` and `relevant()` are the shared packet helpers
     that sweep.py uses. They are source-agnostic and belong to the packet contract,
     not to MercadoLibre.
  3. `map_item()` and its tests encode a correct ML-item -> packet mapping. If access
     returns, discovery is one function call away rather than a rewrite.

Everything below still honours the project's evidence discipline:

  * It never invents a fact. A MercadoLibre attribute that is absent becomes null,
    which `hunter.py` renders as "Not verified."
  * It never reads facts out of the free-text description. Only the attribute ids
    listed in config/mercadolibre.json are trusted.
  * `price_usd` is written only when MercadoLibre reports currency_id == "USD".
    An ARS asking price is recorded separately and left out of `price_usd`,
    because silently mixing the two would corrupt every downstream price test.
  * Every emitted value records its origin in `evidence`, so no number reaches an
    alert without a traceable source.

It does not bypass CAPTCHAs, logins, or rate limits; on HTTP 401/403 it reports the
condition and exits rather than retrying around it.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path
from typing import Any, Optional

import hunter
from hunter import HunterError, clean_text, iso_now, local_now, text_key

# Credentials come from .env so a new shell or a launchd job still has them.
hunter.load_dotenv()

ROOT = Path(__file__).resolve().parent
DEFAULT_ML_CONFIG = ROOT / "config" / "mercadolibre.json"
DEFAULT_REFRESH_TOKEN_FILE = ROOT / "data" / "ml_refresh_token"
PKCE_FILE = ROOT / "data" / "ml_pkce.json"
API_ROOT = "https://api.mercadolibre.com"
TOKEN_URL = "https://api.mercadolibre.com/oauth/token"

# MercadoLibre reports these as value_name strings; map to the project's vocabulary.
CONDITION_WORDS = {
    "nuevo": "new",
    "new": "new",
    "usado": "used",
    "used": "used",
}


def read_ml_config(path: Path) -> dict[str, Any]:
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise HunterError(f"MercadoLibre configuration not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise HunterError(f"MercadoLibre configuration is not valid JSON: {path}") from exc
    for key in ("site", "category", "search_terms", "attribute_map"):
        if key not in config:
            raise HunterError(f"MercadoLibre configuration missing: {key}")
    if not isinstance(config["search_terms"], list) or not config["search_terms"]:
        raise HunterError("MercadoLibre configuration requires a non-empty search_terms array")
    return config


def token_request(fields: dict[str, str]) -> dict[str, Any]:
    """POST to MercadoLibre's token endpoint and return the parsed response."""
    body = urllib.parse.urlencode(fields).encode("utf-8")
    request = urllib.request.Request(
        TOKEN_URL,
        data=body,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        raise HunterError(
            f"MercadoLibre {fields.get('grant_type')} request failed ({exc.code}): {detail}"
        ) from exc
    except urllib.error.URLError as exc:
        raise HunterError(f"MercadoLibre token request could not reach the API: {exc.reason}") from exc
    if not payload.get("access_token"):
        raise HunterError(f"MercadoLibre token response contained no access_token: {payload}")
    return payload


def refresh_token_path() -> Optional[Path]:
    """Where the rotated refresh token is kept.

    Defaults to data/ml_refresh_token so a local run persists rotation without any
    configuration. data/ is gitignored, so the credential does not reach git.
    """
    return Path(os.environ.get("ML_REFRESH_TOKEN_FILE") or DEFAULT_REFRESH_TOKEN_FILE)


def store_pkce(verifier: str, state: str) -> None:
    PKCE_FILE.parent.mkdir(parents=True, exist_ok=True)
    PKCE_FILE.write_text(json.dumps({"verifier": verifier, "state": state}), encoding="utf-8")
    try:
        PKCE_FILE.chmod(0o600)
    except OSError:
        pass


def load_pkce_verifier() -> Optional[str]:
    if not PKCE_FILE.exists():
        return None
    try:
        return str(json.loads(PKCE_FILE.read_text(encoding="utf-8")).get("verifier") or "") or None
    except (json.JSONDecodeError, OSError):
        return None


def stored_refresh_token() -> Optional[str]:
    """Prefer a rotated token on disk over the original bootstrap secret.

    MercadoLibre invalidates a refresh token the moment it is used and returns a
    replacement. A scheduled job that keeps replaying the original secret works
    exactly once. So the rotated value is written to ML_REFRESH_TOKEN_FILE and read
    back in preference to the environment on subsequent runs.
    """
    path = refresh_token_path()
    if path and path.exists():
        token = path.read_text(encoding="utf-8").strip()
        if token:
            return token
    return os.environ.get("ML_REFRESH_TOKEN")


def store_refresh_token(token: str) -> None:
    path = refresh_token_path()
    if not path or not token:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(token + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass


def access_token() -> Optional[str]:
    """Obtain a bearer token, preferring a stored refresh token.

    MercadoLibre's auth guide documents authorization_code and refresh_token, while
    the app-creation console also offers a Client Credentials flow. Since the two
    disagree, this tries both: a stored refresh token first, then client_credentials
    as a fallback. Use `--try-client-credentials` to find out which your app supports.

    The critical operational detail, straight from their docs: a refresh token is
    SINGLE USE. Each refresh invalidates it and returns a replacement, and only the
    most recent one is accepted. A job that replays the original secret therefore
    works exactly once. So the replacement is persisted immediately, before the token
    is used for anything, and takes precedence over the bootstrap secret on the next
    run.
    """
    client_id = os.environ.get("ML_CLIENT_ID")
    client_secret = os.environ.get("ML_CLIENT_SECRET")
    if not client_id or not client_secret:
        return None

    refresh = stored_refresh_token()
    if not refresh:
        # MercadoLibre's auth guide lists only authorization_code and refresh_token
        # under unsupported_grant_type, but the app-creation form offers a
        # "Client Credentials" flow. The documentation and the console disagree, so
        # try it rather than assume: when it works it removes the browser step and
        # all of the single-use rotation machinery below.
        try:
            payload = token_request(
                {
                    "grant_type": "client_credentials",
                    "client_id": client_id,
                    "client_secret": client_secret,
                }
            )
        except HunterError as exc:
            raise HunterError(
                "No refresh token is available and client_credentials was refused "
                f"({exc}). Run `python3.13 fetch_ml.py --auth-url` for the one-time "
                "browser authorization."
            ) from exc
        return str(payload["access_token"])

    payload = token_request(
        {
            "grant_type": "refresh_token",
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh,
        }
    )
    rotated = payload.get("refresh_token")
    if rotated and rotated != refresh:
        # Persist before returning: if the caller crashes mid-run, the rotated token
        # is already saved. Losing it means redoing the browser authorization.
        store_refresh_token(str(rotated))
    return str(payload["access_token"])


def api_get(path: str, params: dict[str, Any], config: dict[str, Any], token: Optional[str]) -> dict[str, Any]:
    url = f"{API_ROOT}{path}?{urllib.parse.urlencode(params)}"
    headers = {
        "Accept": "application/json",
        "User-Agent": config.get("user_agent", "san-isidro-house-hunter/1.0"),
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        if exc.code in (401, 403):
            raise HunterError(
                f"MercadoLibre refused the request ({exc.code}). Set ML_CLIENT_ID and "
                f"ML_CLIENT_SECRET, or the endpoint now requires auth. Do not work around "
                f"this. Detail: {detail}"
            ) from exc
        if exc.code == 429:
            raise HunterError(
                "MercadoLibre rate-limited the request (429). Increase "
                "request_delay_seconds in config/mercadolibre.json and retry later."
            ) from exc
        raise HunterError(f"MercadoLibre API error ({exc.code}) on {path}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise HunterError(f"Could not reach the MercadoLibre API: {exc.reason}") from exc


def attribute_index(item: dict[str, Any]) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    for attribute in item.get("attributes") or []:
        attribute_id = attribute.get("id")
        if attribute_id:
            index[str(attribute_id)] = attribute
    return index


def attribute_number(attribute: dict[str, Any]) -> Optional[float]:
    """Prefer MercadoLibre's structured number over parsing its display string."""
    struct = attribute.get("value_struct")
    if isinstance(struct, dict) and isinstance(struct.get("number"), (int, float)):
        return struct["number"]
    for key in ("value_name", "value"):
        raw = attribute.get(key)
        if raw in (None, ""):
            continue
        digits = "".join(character for character in str(raw) if character.isdigit() or character == ".")
        if digits:
            try:
                return float(digits)
            except ValueError:
                return None
    return None


def attribute_bool(attribute: dict[str, Any]) -> Optional[bool]:
    raw = text_key(attribute.get("value_name") or attribute.get("value"))
    if raw in {"si", "yes", "true", "1"}:
        return True
    if raw in {"no", "false", "0"}:
        return False
    return None


def build_address(item: dict[str, Any]) -> Optional[str]:
    """Assemble an address from MercadoLibre's location block.

    MercadoLibre very often withholds the street number on real-estate listings. We
    record whatever is public and let `hunter.py` treat a missing address as a reason
    to withhold the alert, rather than guessing a block.
    """
    location = item.get("location") or {}
    address = item.get("address") or {}
    parts = [
        clean_text(location.get("address_line")),
        clean_text((location.get("neighborhood") or {}).get("name") or address.get("neighborhood")),
        clean_text((location.get("city") or {}).get("name") or address.get("city_name")),
    ]
    unique = list(dict.fromkeys(part for part in parts if part))
    return ", ".join(unique) if unique else None


def coordinates(item: dict[str, Any]) -> tuple[Optional[float], Optional[float]]:
    location = item.get("location") or {}
    latitude, longitude = location.get("latitude"), location.get("longitude")
    if isinstance(latitude, (int, float)) and isinstance(longitude, (int, float)):
        return float(latitude), float(longitude)
    return None, None


def map_item(item: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    """Translate one MercadoLibre item into a research-packet record.

    Returns a dict in exactly the shape research/README.md documents, so the output is
    indistinguishable from a hand-researched packet and flows through the existing
    normalise_record / ingest_record path unchanged.
    """
    permalink = clean_text(item.get("permalink"))
    if not permalink:
        raise HunterError(f"MercadoLibre item {item.get('id')} has no public permalink")

    record: dict[str, Any] = {
        "source": "Mercado Libre",
        "url": permalink,
        "source_listing_id": clean_text(item.get("id")),
        "canonical_address": build_address(item),
        "description": clean_text(item.get("title")),
        "observed_at": iso_now(),
        "market_stage": "listed",
    }
    evidence: dict[str, str] = {}

    # Price. Only a USD-denominated asking price may populate price_usd.
    price = item.get("price")
    currency = str(item.get("currency_id") or "").upper()
    if isinstance(price, (int, float)) and currency == "USD":
        record["price_usd"] = price
        evidence["price_usd"] = f"MercadoLibre API price field, currency_id=USD ({permalink})"
    elif isinstance(price, (int, float)) and currency:
        # Recorded for context, deliberately NOT converted: we have no verified rate.
        record["price_ars"] = price if currency == "ARS" else None
        record["fit_notes"] = f"Asking price published in {currency}, not USD; USD price not verified."
        evidence["price_currency"] = f"MercadoLibre API reports currency_id={currency} ({permalink})"

    attributes = attribute_index(item)

    for attribute_id, field in (config.get("attribute_map") or {}).items():
        if not field or attribute_id not in attributes:
            continue
        attribute = attributes[attribute_id]
        if field in hunter.NUMERIC_FIELDS:
            value = attribute_number(attribute)
            if value is None:
                continue
            record[field] = value
        else:
            value = clean_text(attribute.get("value_name"))
            if not value:
                continue
            record[field] = CONDITION_WORDS.get(text_key(value), value) if field == "condition" else value
        evidence[field] = f"MercadoLibre attribute {attribute_id} ({permalink})"

    for attribute_id, field in (config.get("boolean_attribute_map") or {}).items():
        if attribute_id not in attributes:
            continue
        value = attribute_bool(attributes[attribute_id])
        if value is None:
            continue
        # Never downgrade a confirmed True from one attribute with a False from another.
        if record.get(field) is True and value is False:
            continue
        record[field] = value
        evidence[field] = f"MercadoLibre attribute {attribute_id} ({permalink})"

    latitude, longitude = coordinates(item)
    if latitude is not None:
        record["latitude"], record["longitude"] = latitude, longitude
        evidence["coordinates"] = f"MercadoLibre location block ({permalink})"

    photos = [
        picture.get("secure_url") or picture.get("url")
        for picture in (item.get("pictures") or [])
    ]
    record["photo_urls"] = [url for url in photos if url][:12]

    record["evidence"] = evidence
    return record


def relevant(record: dict[str, Any], config: dict[str, Any]) -> bool:
    """Keep only listings that land in a Priority A or B micro-area.

    Reuses hunter.infer_priority so discovery and scoring can never disagree about
    what counts as the target geography. This is the filter that stops the packet
    from becoming the 'giant list of generic houses' the brief warns against.
    """
    priority, _ = hunter.infer_priority(record, config)
    return priority is not None


def search_items(
    ml_config: dict[str, Any],
    token: Optional[str],
    term: str,
    verbose: bool,
) -> list[dict[str, Any]]:
    collected: list[dict[str, Any]] = []
    page_size = int(ml_config.get("page_size", 50))
    max_pages = int(ml_config.get("max_pages", 10))
    delay = float(ml_config.get("request_delay_seconds", 1.0))
    for page in range(max_pages):
        params = {
            "category": ml_config["category"],
            "q": term,
            "limit": page_size,
            "offset": page * page_size,
        }
        if ml_config.get("operation"):
            params["OPERATION"] = ml_config["operation"]
        payload = api_get(f"/sites/{ml_config['site']}/search", params, ml_config, token)
        results = payload.get("results") or []
        collected.extend(results)
        if verbose:
            print(f"  '{term}' page {page + 1}: {len(results)} results", file=sys.stderr)
        total = (payload.get("paging") or {}).get("total")
        if len(results) < page_size or (isinstance(total, int) and (page + 1) * page_size >= total):
            break
        time.sleep(delay)
    return collected


def discover(ml_config: dict[str, Any], targeting: dict[str, Any], verbose: bool) -> list[dict[str, Any]]:
    token = access_token()
    if verbose:
        print(
            "Authenticating with app credentials" if token else "Trying unauthenticated access",
            file=sys.stderr,
        )
    seen_ids: set[str] = set()
    records: list[dict[str, Any]] = []
    skipped_off_target = 0
    skipped_unusable = 0
    for term in ml_config["search_terms"]:
        for item in search_items(ml_config, token, term, verbose):
            item_id = str(item.get("id") or "")
            if not item_id or item_id in seen_ids:
                continue
            seen_ids.add(item_id)
            try:
                record = map_item(item, ml_config)
            except HunterError:
                skipped_unusable += 1
                continue
            if not relevant(record, targeting):
                skipped_off_target += 1
                continue
            records.append(record)
        time.sleep(float(ml_config.get("request_delay_seconds", 1.0)))
    if verbose:
        print(
            f"Fetched {len(seen_ids)} unique items; kept {len(records)}; "
            f"dropped {skipped_off_target} outside Priority A/B; "
            f"dropped {skipped_unusable} without a usable public link.",
            file=sys.stderr,
        )
    return records


def packet_path(directory: Path, when: Optional[str] = None) -> Path:
    return directory / f"{when or local_now().date().isoformat()}.jsonl"


def write_packet(records: list[dict[str, Any]], path: Path) -> int:
    """Append to today's packet without duplicating URLs already written.

    Append rather than overwrite so several discovery passes in one day compose, and
    so a hand-researched record added by a human is never destroyed by a later
    automated run.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    existing: set[str] = set()
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                existing.add(str(json.loads(line).get("url") or ""))
            except json.JSONDecodeError:
                continue
    fresh = [record for record in records if record["url"] not in existing]
    if fresh:
        with path.open("a", encoding="utf-8") as handle:
            for record in fresh:
                handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    return len(fresh)


def command_auth_check(args: argparse.Namespace) -> int:
    """Verify the stored credentials can actually reach search, and report precisely."""
    client_id = os.environ.get("ML_CLIENT_ID")
    client_secret = os.environ.get("ML_CLIENT_SECRET")
    if not client_id or not client_secret:
        print(
            "ML_CLIENT_ID and ML_CLIENT_SECRET are not set.\n\n"
            "1. Create an app: https://developers.mercadolibre.com.ar/devcenter\n"
            "   - Redirect URI must be a STATIC https URL (no variable parts).\n"
            "   - Enable the 'read' and 'offline_access' scopes. Without\n"
            "     offline_access MercadoLibre returns no refresh_token and the\n"
            "     scheduled run cannot work.\n"
            "2. export ML_CLIENT_ID=... ML_CLIENT_SECRET=...\n"
            "3. python3.13 fetch_ml.py --auth-url\n",
            file=sys.stderr,
        )
        return 2

    if not stored_refresh_token():
        print(
            "Credentials are set but there is no refresh token yet.\n"
            "MercadoLibre only supports authorization_code and refresh_token, so a\n"
            "one-time browser authorization is required:\n\n"
            "  python3.13 fetch_ml.py --auth-url\n",
            file=sys.stderr,
        )
        return 2

    print("Refreshing access token...")
    token = access_token()
    print("  token obtained and any rotated refresh token persisted.")

    ml_config = read_ml_config(Path(args.ml_config))
    try:
        payload = api_get(
            f"/sites/{ml_config['site']}/search",
            {"category": ml_config["category"], "limit": 1},
            ml_config,
            token,
        )
    except HunterError as exc:
        print(f"  search: REFUSED\n  {exc}\n", file=sys.stderr)
        print(
            "Per MercadoLibre's error reference a 403 here means one of: the app is\n"
            "missing the 'read' scope, the IP is blocked, or the token belongs to a\n"
            "different user. Check the app's scopes first.",
            file=sys.stderr,
        )
        return 1
    total = (payload.get("paging") or {}).get("total")
    print(f"  search: OK ({total} results in category {ml_config['category']})")
    print("\nAuth is working. You can now run: ./run.sh --dry-run")
    return 0


def command_auth_url(args: argparse.Namespace) -> int:
    """Print the one-time browser authorization URL, with PKCE and state."""
    client_id = os.environ.get("ML_CLIENT_ID")
    redirect = os.environ.get("ML_REDIRECT_URI")
    if not client_id:
        print("error: ML_CLIENT_ID is not set", file=sys.stderr)
        return 2
    if not redirect:
        print(
            "error: ML_REDIRECT_URI is not set.\n"
            "It must match your app's configured Redirect URI EXACTLY -- MercadoLibre\n"
            "rejects any mismatch, and the URL cannot contain variable parts.\n"
            "Example: export ML_REDIRECT_URI=https://example.com/callback",
            file=sys.stderr,
        )
        return 2

    # PKCE is optional per app, but harmless when unused and required when enabled.
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).decode("ascii").rstrip("=")
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
        .decode("ascii")
        .rstrip("=")
    )
    state = secrets.token_urlsafe(16)
    store_pkce(verifier, state)

    query = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect,
        "state": state,
    }
    if not args.no_pkce:
        query["code_challenge"] = challenge
        query["code_challenge_method"] = "S256"
    params = urllib.parse.urlencode(query)
    exchange_flags = " --no-pkce" if args.no_pkce else ""
    print(
        "Open this URL in a browser, signed in as the ACCOUNT ADMINISTRATOR\n"
        "(a collaborator/operator account cannot grant access -- MercadoLibre returns\n"
        "invalid_operator_user_id):\n\n"
        f"  https://auth.mercadolibre.com.ar/authorization?{params}\n\n"
        "You will be redirected to a page that fails to load. That is expected -- the\n"
        "code is in the browser's ADDRESS BAR, not on the page:\n"
        f"  {redirect}?code=TG-xxxxx&state={state}\n\n"
        "Copy the value after code=, stopping before any &, then run this IMMEDIATELY\n"
        "(the code is single-use and expires in about 10 minutes):\n\n"
        f"  python3.13 fetch_ml.py --exchange-code PASTE_CODE_HERE{exchange_flags}\n\n"
        "Do not re-run --auth-url before exchanging: each run generates a new PKCE\n"
        "verifier and invalidates the previous URL.\n"
    )
    return 0


def clean_authorization_code(raw: str) -> str:
    """Validate the pasted authorization code and strip common copy artifacts.

    MercadoLibre codes are TG-<hex>-<numeric user id>. Selecting a code out of a
    browser address bar very easily grabs one extra character from the following
    query parameter, and ML answers that with an opaque `invalid_grant` that looks
    identical to an expired code. Catching it here saves a round trip through the
    whole browser flow.
    """
    code = (raw or "").strip().strip("'\"")
    # A full URL pasted instead of just the code.
    if "code=" in code:
        parsed = urllib.parse.parse_qs(urllib.parse.urlsplit(code).query)
        if parsed.get("code"):
            code = parsed["code"][0]
    code = code.split("&")[0].strip()

    match = re.fullmatch(r"(TG-[0-9a-fA-F]+-\d+)([A-Za-z]*)", code)
    if not match:
        raise HunterError(
            f"'{code}' does not look like a MercadoLibre authorization code.\n"
            "Expected the form TG-<hex>-<numeric user id>, for example\n"
            "TG-0000000000000000000000aa-12345678\n"
            "Copy strictly between 'code=' and the next '&' in the address bar."
        )
    cleaned, trailing = match.group(1), match.group(2)
    if trailing:
        print(
            f"note: dropped trailing '{trailing}' from the code -- MercadoLibre codes "
            f"end in a numeric user id, so that character came from the next query\n"
            f"parameter (probably &state=...). Using: {cleaned}",
            file=sys.stderr,
        )
    return cleaned


def command_exchange_code(args: argparse.Namespace) -> int:
    """Exchange the one-time authorization code for access and refresh tokens."""
    client_id = os.environ.get("ML_CLIENT_ID")
    client_secret = os.environ.get("ML_CLIENT_SECRET")
    redirect = os.environ.get("ML_REDIRECT_URI")
    if not client_id or not client_secret or not redirect:
        print(
            "error: ML_CLIENT_ID, ML_CLIENT_SECRET and ML_REDIRECT_URI must all be set",
            file=sys.stderr,
        )
        return 2
    fields = {
        "grant_type": "authorization_code",
        "client_id": client_id,
        "client_secret": client_secret,
        "code": clean_authorization_code(args.exchange_code),
        "redirect_uri": redirect,
    }
    verifier = None if args.no_pkce else load_pkce_verifier()
    if verifier:
        fields["code_verifier"] = verifier

    # PKCE is per-app optional. If the app does not have it enabled, sending a
    # code_verifier can itself cause invalid_grant -- and so can a stale verifier from
    # an earlier --auth-url run. Retrying without it isolates PKCE as the cause
    # instead of leaving you to guess between three identical-looking failures.
    try:
        payload = token_request(fields)
    except HunterError as exc:
        if verifier and "invalid_grant" in str(exc):
            print(
                "First attempt failed with invalid_grant. Retrying without PKCE, in\n"
                "case the app does not have it enabled or the stored verifier is stale...",
                file=sys.stderr,
            )
            fields.pop("code_verifier", None)
            try:
                payload = token_request(fields)
            except HunterError as retry_exc:
                if "code_verifier is a required parameter" in str(retry_exc):
                    raise HunterError(
                        "This app REQUIRES PKCE, so the retry without it was rejected.\n"
                        "The first attempt did send a verifier and still failed, which "
                        "means the code itself was bad -- almost always expired (~10 min) "
                        "or already used.\n\n"
                        "Use the single-step flow, which leaves no window for the code to "
                        "expire:\n"
                        "  python3.13 fetch_ml.py --auth-login"
                    ) from retry_exc
                raise HunterError(
                    f"{retry_exc}\n\n"
                    "Both attempts failed. In order of "
                    "likelihood:\n"
                    "  1. The code expired (~10 min) or was already used. Codes are "
                    "single-use -- run --auth-url again and exchange immediately.\n"
                    "  2. ML_REDIRECT_URI does not match the app's registered Redirect "
                    f"URI exactly. Currently sending: {redirect}\n"
                    "  3. You authorized with a collaborator account rather than the "
                    "account administrator."
                ) from retry_exc
            print("Retry without PKCE succeeded. Disable 'Requiere PKCE' on the app, "
                  "or always pass --no-pkce.", file=sys.stderr)
        else:
            raise
    refresh = payload.get("refresh_token")
    if not refresh:
        print(
            "error: MercadoLibre returned no refresh_token. The app is almost certainly\n"
            "missing the 'offline_access' scope. Add it, then redo --auth-url.\n"
            f"scope returned: {payload.get('scope')!r}",
            file=sys.stderr,
        )
        return 1

    store_refresh_token(str(refresh))
    path = refresh_token_path()
    print(
        json.dumps(
            {
                "scope": payload.get("scope"),
                "user_id": payload.get("user_id"),
                "access_token_expires_in_seconds": payload.get("expires_in"),
                "refresh_token": refresh,
                "saved_to": str(path) if path else None,
            },
            indent=2,
        )
    )
    print(
        "\nStore that refresh_token as the ML_REFRESH_TOKEN repository secret.\n"
        "It is SINGLE USE: the first scheduled run consumes it and receives a\n"
        "replacement. See 'MercadoLibre auth' in the README for how the workflow\n"
        "persists the rotated value, and note it expires after 6 months.",
        file=sys.stderr,
    )
    return 0


def command_try_client_credentials(args: argparse.Namespace) -> int:
    """Test whether this app can use client_credentials, and whether search accepts it.

    Worth knowing before committing to the interactive flow: client_credentials is
    stateless, so it needs no browser step, no single-use token rotation, and no PAT
    in CI. Getting a token is not sufficient, though -- the token also has to be
    accepted for search, so both are checked.
    """
    client_id = os.environ.get("ML_CLIENT_ID")
    client_secret = os.environ.get("ML_CLIENT_SECRET")
    if not client_id or not client_secret:
        print("error: set ML_CLIENT_ID and ML_CLIENT_SECRET first", file=sys.stderr)
        return 2

    print("1. Requesting a token with grant_type=client_credentials...")
    try:
        payload = token_request(
            {
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": client_secret,
            }
        )
    except HunterError as exc:
        detail = str(exc)
        print(f"   REFUSED: {detail}\n", file=sys.stderr)
        # invalid_client and unsupported_grant_type mean completely different things.
        # Reporting "this flow is unavailable" for a mistyped secret would send you
        # down the wrong path entirely.
        if "invalid_client" in detail:
            print(
                "That is a CREDENTIALS error, not a verdict on the flow.\n"
                "MercadoLibre rejected the client_id/client_secret pair itself, so\n"
                "client_credentials has not actually been tested yet.\n\n"
                "Check that ML_CLIENT_SECRET holds the real Secret Key (not a\n"
                "placeholder), has no surrounding quotes or whitespace, and belongs to\n"
                f"app {client_id}. Then re-run this command.",
                file=sys.stderr,
            )
        elif "unsupported_grant_type" in detail:
            print(
                "This app cannot use client_credentials -- MercadoLibre rejected the\n"
                "grant type itself. Enable the flow in the app settings, or use the\n"
                "browser flow:\n"
                "  python3.13 fetch_ml.py --auth-url",
                file=sys.stderr,
            )
        else:
            print(
                "Use the browser flow instead:\n"
                "  python3.13 fetch_ml.py --auth-url",
                file=sys.stderr,
            )
        return 1
    print(f"   OK. scope={payload.get('scope')!r} expires_in={payload.get('expires_in')}")

    print("2. Calling search with that token...")
    ml_config = read_ml_config(Path(args.ml_config))
    try:
        result = api_get(
            f"/sites/{ml_config['site']}/search",
            {"category": ml_config["category"], "limit": 1},
            ml_config,
            str(payload["access_token"]),
        )
    except HunterError as exc:
        print(f"   REFUSED: {exc}\n", file=sys.stderr)
        print(
            "The token is valid but search rejects it. Use the browser flow:\n"
            "  python3.13 fetch_ml.py --auth-url",
            file=sys.stderr,
        )
        return 1
    total = (result.get("paging") or {}).get("total")
    print(f"   OK. {total} results in category {ml_config['category']}")
    print(
        "\nClient Credentials works. Set only ML_CLIENT_ID and ML_CLIENT_SECRET as\n"
        "repository secrets -- no browser step, no ML_REFRESH_TOKEN, no GH_PAT, and\n"
        "the token-rotation steps in the workflow are unnecessary."
    )
    return 0


def command_auth_login(args: argparse.Namespace) -> int:
    """Authorize and exchange in one sitting, without a copy-paste gap.

    MercadoLibre authorization codes expire in about 10 minutes and are single use.
    Running --auth-url and --exchange-code as separate steps leaves a window in which
    the code dies, and the resulting invalid_grant is indistinguishable from a real
    misconfiguration. This does both halves back to back: it opens the browser, waits
    for you to paste the redirect URL, and exchanges immediately.
    """
    client_id = os.environ.get("ML_CLIENT_ID")
    client_secret = os.environ.get("ML_CLIENT_SECRET")
    redirect = os.environ.get("ML_REDIRECT_URI")
    if not client_id or not client_secret or not redirect:
        print(
            "error: ML_CLIENT_ID, ML_CLIENT_SECRET and ML_REDIRECT_URI must all be set",
            file=sys.stderr,
        )
        return 2

    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).decode("ascii").rstrip("=")
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
        .decode("ascii")
        .rstrip("=")
    )
    state = secrets.token_urlsafe(16)
    store_pkce(verifier, state)

    query = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect,
        "state": state,
    }
    if not args.no_pkce:
        query["code_challenge"] = challenge
        query["code_challenge_method"] = "S256"
    url = f"https://auth.mercadolibre.com.ar/authorization?{urllib.parse.urlencode(query)}"

    print("Opening the authorization page in your browser.")
    print("Sign in as the ACCOUNT ADMINISTRATOR and approve.\n")
    print(f"  {url}\n")
    try:
        webbrowser.open(url)
    except Exception:
        print("(could not open a browser automatically; copy the URL above)")

    print(
        "You will land on a page that fails to load. That is expected.\n"
        "Copy the ENTIRE address bar and paste it here, then press Enter:\n"
    )
    try:
        pasted = input("redirect URL> ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\naborted", file=sys.stderr)
        return 130

    if not pasted:
        print("error: nothing pasted", file=sys.stderr)
        return 2

    returned_state = urllib.parse.parse_qs(urllib.parse.urlsplit(pasted).query).get("state", [None])[0]
    if returned_state and returned_state != state:
        print(
            f"error: state mismatch. Expected {state}, got {returned_state}.\n"
            "That URL is from a different authorization attempt. Re-run this command.",
            file=sys.stderr,
        )
        return 2

    code = clean_authorization_code(pasted)
    fields = {
        "grant_type": "authorization_code",
        "client_id": client_id,
        "client_secret": client_secret,
        "code": code,
        "redirect_uri": redirect,
    }
    if not args.no_pkce:
        fields["code_verifier"] = verifier

    print("\nExchanging the code...")
    payload = token_request(fields)
    refresh = payload.get("refresh_token")
    if not refresh:
        print(
            "error: MercadoLibre returned no refresh_token. The app is almost certainly\n"
            f"missing the 'offline_access' scope. scope returned: {payload.get('scope')!r}",
            file=sys.stderr,
        )
        return 1
    store_refresh_token(str(refresh))
    print(
        json.dumps(
            {
                "scope": payload.get("scope"),
                "user_id": payload.get("user_id"),
                "access_token_expires_in_seconds": payload.get("expires_in"),
                "refresh_token_saved_to": str(refresh_token_path()),
            },
            indent=2,
        )
    )
    print("\nAuthorized. Now run: python3.13 fetch_ml.py --auth-check")
    return 0


def command_probe(args: argparse.Namespace) -> int:
    """One minimal request, to tell a config error apart from an auth or network error."""
    ml_config = read_ml_config(Path(args.ml_config))
    token = access_token()
    payload = api_get(
        f"/sites/{ml_config['site']}/search",
        {"category": ml_config["category"], "limit": 1},
        ml_config,
        token,
    )
    total = (payload.get("paging") or {}).get("total")
    print(
        json.dumps(
            {
                "authenticated": bool(token),
                "category": ml_config["category"],
                "total_results_for_category": total,
                "verdict": "reachable" if isinstance(total, int) and total > 0 else "reachable but zero results; check the category id",
            },
            indent=2,
        )
    )
    return 0


def command_fetch(args: argparse.Namespace) -> int:
    ml_config = read_ml_config(Path(args.ml_config))
    targeting = hunter.read_config(Path(args.config))
    records = discover(ml_config, targeting, verbose=not args.quiet)
    path = packet_path(Path(args.output_dir), args.date)
    if args.dry_run:
        for record in records:
            print(json.dumps(record, ensure_ascii=False, sort_keys=True))
        print(f"[dry-run] {len(records)} records; would append to {path}", file=sys.stderr)
        return 0
    written = write_packet(records, path)
    print(json.dumps({"discovered": len(records), "written": written, "packet": str(path)}))
    return 0


def parser() -> argparse.ArgumentParser:
    base = argparse.ArgumentParser(description="MercadoLibre public-API discovery for the San Isidro radar")
    base.add_argument("--ml-config", default=str(DEFAULT_ML_CONFIG))
    base.add_argument("--config", default=str(hunter.DEFAULT_CONFIG))
    base.add_argument("--output-dir", default=str(ROOT / "research"))
    base.add_argument("--date", help="packet date (YYYY-MM-DD); defaults to today in Argentina")
    base.add_argument("--dry-run", action="store_true", help="print records instead of writing the packet")
    base.add_argument("--quiet", action="store_true")
    base.add_argument("--probe", action="store_true", help="check API reachability and category id, then exit")
    base.add_argument("--auth-check", action="store_true", help="determine which OAuth flow this app supports")
    base.add_argument(
        "--try-client-credentials",
        action="store_true",
        help="test the stateless client_credentials flow (no browser step needed if it works)",
    )
    base.add_argument(
        "--auth-login",
        action="store_true",
        help="authorize and exchange in one step (recommended: no window for the code to expire)",
    )
    base.add_argument("--auth-url", action="store_true", help="print the one-time browser authorization URL")
    base.add_argument("--exchange-code", help="exchange a one-time authorization code for tokens")
    base.add_argument(
        "--no-pkce",
        action="store_true",
        help="omit PKCE (use if the app does not have 'Requiere PKCE' enabled)",
    )
    return base


def main(argv: Optional[list[str]] = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.try_client_credentials:
            return command_try_client_credentials(args)
        if args.auth_login:
            return command_auth_login(args)
        if args.auth_check:
            return command_auth_check(args)
        if args.auth_url:
            return command_auth_url(args)
        if args.exchange_code:
            return command_exchange_code(args)
        return command_probe(args) if args.probe else command_fetch(args)
    except HunterError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Agentic discovery pass: public-web research -> research JSONL packet.

Why this exists: MercadoLibre's search API is closed to third-party apps. A valid
user-context token with `read` scope is still refused by their PolicyAgent
(PA_UNAUTHORIZED_RESULT_FROM_POLICIES), so no OAuth flow opens it. This module is the
discovery path that does work, and it reaches sources the ML API never could --
Zonaprop, Argenprop, local San Isidro agencies, developer pages, owner-direct posts.

It drives the `claude` CLI in headless mode (`claude -p`), so it uses the Claude Code
subscription already installed on this machine rather than a separate API key.

THE CENTRAL RISK, and how it is handled:

An LLM reading listing pages will happily produce plausible numbers. A fabricated
`covered_m2` or `expenses_ars` is worse than a missing one, because the whole point of
this radar is that its alerts can be trusted. Prompting alone does not prevent that.
So fabrication is blocked structurally, in code, not by asking nicely:

  1. Every factual field must be accompanied by an `evidence` entry quoting the
     listing. A field without evidence is DROPPED before it reaches history --
     see enforce_evidence(). This is enforced in code, so inventing a number does not
     fail loudly, it simply has no effect.
  2. A record without a public original URL is dropped entirely. NOTE: the URL is
     never fetched here, so it is checked for shape, not for existence. A dead link can
     still reach the report; the link in the alert is what you click to confirm.
  3. A URL that looks like a search-results or listing-index page is dropped: the
     project requires original listing pages, never search URLs.
  4. Output is validated against the packet contract, and anything unparseable raises
     rather than being silently skipped.
  5. Fields a discovery pass must not produce at all are stripped: see
     FORBIDDEN_FROM_SWEEP.

  LIMIT worth stating plainly: the evidence check confirms that a quote EXISTS and is
  not a restatement of the value. It cannot confirm the quote is really on the page,
  because that would need a second fetch and a text match. So this raises the cost of
  fabrication substantially; it does not make it impossible.

Everything that survives is written in the same format `research/README.md` documents,
so it flows through the existing normalise_record/ingest_record path unchanged and is
indistinguishable downstream from a hand-researched record.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

import hunter
from hunter import HunterError, clean_text, iso_now, text_key

hunter.load_dotenv()

import fetch_ml

ROOT = Path(__file__).resolve().parent
DEFAULT_PROMPT = ROOT / "prompts" / "sweep.md"
AGENTS_FILE = ROOT / "AGENTS.md"

# Facts that must be backed by an evidence quote.
#
# This set is deliberately broad. An adversarial review found that anything left out
# becomes a fabrication channel, because almost every field in this project feeds either
# the alert text, the Priority A gate, or the dedup matcher:
#
#   canonical_address / zone / description -> hunter.infer_priority(), which decides
#       Priority A, which is a hard requirement of is_act_now(). An invented "a 3 cuadras
#       del Club Nautico" in a description was enough to put a property in front of the
#       buyer.
#   broker / broker_phone / photo_urls / distinctive_features / description -> the fuzzy
#       dedup signals in hunter.fuzzy_score(). A zero-evidence record could score 0.86
#       and be merged into an unrelated real listing, corrupting its history.
#   risks -> printed verbatim in the alert's "Riesgos / pendientes" block.
#
# Only genuine non-claims are exempt: source, url, observed_at, fit_notes, evidence.
EVIDENCE_REQUIRED = (
    hunter.NUMERIC_FIELDS
    | hunter.BOOLEAN_FIELDS
    | {"expenses", "condition", "orientation", "property_type", "street_quality",
       "publication_date", "market_stage", "listing_state",
       # Location claims: the most consequential field in the record.
       "canonical_address", "zone",
       # Dedup and alert-text inputs.
       "description", "broker", "broker_phone", "photo_urls",
       "distinctive_features", "risks"}
)

# Fields the sweep must never supply, regardless of evidence.
#
#   negotiation      A discovery pass has no business pricing a house. hunter.py renders
#                    a supplied negotiation block under "Base verificada", and its own
#                    gate is only that a free-text `basis` exists -- written by the same
#                    model, in the same object. Stripping it here forces the honest
#                    config-driven heuristic in hunter.negotiation_report() instead,
#                    which labels itself an inference.
#   source_listing_id  A hard identity alias in hunter.add_aliases(). One fabricated id
#                    silently collapses two different houses into one history row.
#                    The canonical URL is already a reliable alias.
FORBIDDEN_FROM_SWEEP = ("negotiation", "source_listing_id")

# An evidence quote must plausibly BE a quote. A one-character or echo-of-the-value
# string satisfies a mere key-presence check while proving nothing.
MIN_EVIDENCE_CHARS = 12

# A URL that indexes many properties is not an original listing page.
#
# Detection is allowlist-FIRST, because a false positive here is worse than a false
# negative: silently discarding a real candidate is invisible, while an index page that
# slips through is obvious in the report. A pure blocklist is also dangerously easy to
# get wrong -- "/propiedades/" looks like a search marker but appears in every ORIGINAL
# Zonaprop URL (/propiedades/clasificado/...).
#
# An original listing page essentially always carries a listing id.
ORIGINAL_URL_PATTERNS = (
    r"/propiedades/clasificado/",          # zonaprop original listing
    r"--\d{6,}",                           # argenprop: ...--12345678
    r"/MLA-?\d{6,}",                       # mercadolibre item
    r"-\d{7,}\.html?$",                    # generic portal slug ending in a listing id
    r"/(propiedad|inmueble|ficha|detalle|listing|property)[/-]",
)

SEARCH_URL_MARKERS = (
    # generic search/index
    "/searchresult", "search?", "/search/", "/busqueda", "/buscar", "/resultados",
    "/listado", "/filtro", "/ordenar", "?q=", "&q=", "/s/", "_desde_",
    # zonaprop index: /casas-venta-san-isidro.html
    "-venta-san-isidro", "-venta-beccar", "-orden-",
    # argenprop index: /casas/venta/san-isidro
    "/casas/venta", "/casas/alquiler", "/departamentos/venta", "/inmuebles/venta",
    # mercadolibre index
    "/inmuebles/casas/", "/inmuebles/venta/",
    # generic index slugs
    "/casas-en-venta", "/propiedades-en-venta", "/inmuebles-en-venta",
)


def claude_binary() -> str:
    for candidate in (
        os.environ.get("CLAUDE_BIN"),
        shutil.which("claude"),
        str(Path.home() / ".local" / "bin" / "claude"),
        str(Path.home() / ".claude" / "local" / "claude"),
    ):
        if candidate and Path(candidate).exists():
            return candidate
    raise HunterError(
        "The `claude` CLI was not found. Install Claude Code, or set CLAUDE_BIN to its "
        "path. The agentic sweep drives it in headless mode so it can use your existing "
        "subscription instead of a separate API key."
    )


def build_prompt(prompt_path: Path, max_results: int) -> str:
    """Assemble the sweep instruction: the buyer's brief, the recurring task, and a
    hard output contract.

    Both input files are REQUIRED. An earlier version substituted an empty string for a
    missing file, which meant a renamed or deleted prompt produced a sweep that ran with
    no instructions and returned plausible-looking rubbish. Silent degradation is the
    worst outcome for an unattended job, so this fails loudly instead.
    """
    if not AGENTS_FILE.exists():
        raise HunterError(
            f"The buyer brief is missing: {AGENTS_FILE}\n"
            "Without it the sweep has no idea what to look for. Restore the file."
        )
    if not prompt_path.exists():
        raise HunterError(
            f"The sweep instruction is missing: {prompt_path}\n"
            "Without it the sweep has no task. Restore the file, or pass --prompt with "
            "the correct path."
        )
    brief = AGENTS_FILE.read_text(encoding="utf-8")
    task = prompt_path.read_text(encoding="utf-8")
    schema_fields = json.loads((ROOT / "listing_schema.json").read_text(encoding="utf-8"))
    # listing_schema.json documents the full packet contract, including fields a HUMAN
    # researcher may supply. Do not advertise the ones a sweep must never produce.
    advertised = [
        field for field in schema_fields.get("fields", [])
        if field not in FORBIDDEN_FROM_SWEEP and field != "negotiation_notes"
    ]

    return f"""You are the discovery stage of a real-estate radar. Search the public web
and return structured findings. You are NOT writing a report for a human -- your entire
output is consumed by a program.

=== BUYER BRIEF ===
{brief}

=== WHICH PARTS OF THAT BRIEF ARE YOURS ===
The brief above describes the whole radar, not only your stage. You are DISCOVERY. Your
job is to find candidates and record evidence.

These parts of the brief are handled by the program that consumes your output, so do not
attempt them and do not let them shape your JSON:
  - Freshness: NEW / MATERIAL_CHANGE / OLD / DUPLICATE / UNCERTAIN. You are stateless and
    cannot know what was seen before. Report everything you find.
  - Negotiation: asking price versus probable range, target, opening offer, maximum
    justified price. Do NOT estimate these. The program computes them from config.
  - The alert format, the ACT NOW decision, and the daily report.

=== THIS RUN ===
{task}

=== SEARCH IN SPANISH (RIOPLATENSE) ===
This is the Buenos Aires market. Argentine listings are written in Spanish, so SEARCH IN
SPANISH. English queries return almost nothing on these portals.

Use the vocabulary a local listing actually uses:
  venta / en venta            (not "for sale")
  casa, chalet, PH            (property types; PH is usually a REJECT, see the brief)
  jardin propio, patio        (private garden)
  pileta, piscina             (pool)
  m2 cubiertos                (covered area)
  m2 totales, lote, terreno   (lot / total area)
  dormitorios, ambientes      (bedrooms; "3 ambientes" counts rooms, NOT bedrooms --
                               a 3-ambientes is typically 2 bedrooms, so do not confuse
                               the two, and record the one the listing actually states)
  banos                       (bathrooms)
  cochera, garage             (parking)
  expensas                    (monthly common expenses)
  acceso controlado, porton   (controlled entrance)
  a estrenar, refaccionado, impecable, excelente estado   (condition)
  a refaccionar, a reciclar   (needs major work -- a REJECT signal)
  proximamente, preventa, pozo, emprendimiento            (pre-market signals)
  complejo, condominio, barrio chico, 6 casas             (small development)
  dueno directo, particular   (owner-direct)

Search both accented and unaccented spellings, since listings are inconsistent:
jardin/jardín, banos/baños, proximamente/próximamente, Nautico/Náutico.

Report facts in the language the listing uses. Do not translate an address. Numeric
values must be the figures the listing prints.

=== HOW TO SEARCH ===
Use web search and fetch public listing pages. Cover, as available:
  - Zonaprop, Argenprop, Mercado Libre listing pages (public pages only)
  - local San Isidro / Beccar agency sites and their public inventory
  - developer and new-development pages, including "proximamente" / pre-market
  - owner-direct and public social posts

ALWAYS open the original listing page before recording any fact. A search-results
snippet is where wrong m2 and wrong prices come from: it truncates, and it sometimes
shows a different unit in the same complex.

Do NOT bypass logins, CAPTCHAs, paywalls, robots restrictions, or rate limits. If a
site blocks access, skip it and say so in `skipped`. Never fabricate a result to fill
a gap.

=== OUTPUT CONTRACT (strict) ===
Return ONE JSON object and nothing else. No prose, no markdown fence, no commentary.

{{
  "listings": [ <record>, ... ],
  "skipped": [ "zonaprop: blocked by bot protection", ... ],
  "searched": [ "query or site actually checked", ... ]
}}

Each <record>:
  REQUIRED: "source" (site/agency name), "url" (the ORIGINAL listing page, never a
  search-results URL), "canonical_address" (as published).

  "canonical_address" also NEEDS its own "evidence" entry, quoting the location wording
  the page prints. It is the field the Priority A gate reads, so it is treated as a
  factual claim, not as an identity label: without an evidence quote it is dropped and
  the whole record is then discarded for having no address.

  OPTIONAL, include ONLY what the page actually states:
{json.dumps(advertised, indent=4)}
  plus: price_usd, covered_m2, total_m2, garden_m2, bedrooms, bathrooms, parking,
  units, expenses, expenses_ars, private_garden, private_pool, pool_potential,
  controlled_entrance, condition, property_type, street_quality, description, broker,
  broker_phone, photo_urls, distinctive_features, publication_date, market_stage,
  owner_direct, exceptional_layout, high_traffic, large_development,
  needs_major_renovation, security_concern, flood_risk.

  DO NOT emit "negotiation" or "source_listing_id". Discovery does not price a house,
  and a guessed listing id silently merges two different properties. Both are stripped
  by the consuming program, so supplying them is wasted effort.

=== THE RULE THAT MATTERS MOST ===
Every factual field you include MUST have a matching entry in that record's
"evidence" object, quoting or closely paraphrasing the listing text that supports it:

  "covered_m2": 170,
  "expenses_ars": 185000,
  "evidence": {{
    "canonical_address": "Breadcrumb reads 'San Isidro > Lasalle', title 'Casa en Juan Bautista de Lasalle 1600'.",
    "covered_m2": "Listing states '170 m2 cubiertos'.",
    "expenses_ars": "Listing states 'Expensas $185.000'."
  }}

Each evidence value must be a real quote or close paraphrase of at least a dozen
characters. A single character, or the value echoed back ("170"), does not count and the
field is dropped.

A field without evidence WILL BE DISCARDED by the consuming program, so including one
is wasted effort. If the page does not state something, OMIT the field. Do not infer,
estimate, convert currencies, or carry a number over from a similar property.

Only report price_usd when the listing is priced in USD. If it is in pesos, omit
price_usd and note the currency in fit_notes. Argentine listings quote both; read which
symbol the page uses, since "$" alone means pesos and "USD"/"U$S" means dollars.

Return at most {max_results} listings, best-fitting first. Prefer precision over
volume: three well-evidenced Priority A candidates beat twenty vague ones.
"""


# Tools the sweep agent may use, and the ones it must never be able to reach.
#
# An earlier version passed --permission-mode bypassPermissions, which does NOT restrict
# the tool set -- it approves everything. Combined with --allowedTools it reads like a
# sandbox while actually granting Bash, Write and Edit to an unattended agent running in
# a directory whose .env holds live credentials. That was wrong.
#
# So: an explicit deny-list, and no blanket bypass. The sweep only reads the web.
SWEEP_ALLOWED_TOOLS = "WebSearch,WebFetch"
SWEEP_DENIED_TOOLS = "Bash,Write,Edit,NotebookEdit,Task,Agent,Workflow"


def build_command(binary: str, prompt: str) -> list[str]:
    """The single definition of how the CLI is invoked, so the sweep and the --check-tools
    diagnostic cannot drift apart on their permission policy."""
    return [
        binary,
        "-p",
        prompt,
        "--output-format", "text",
        "--allowedTools", SWEEP_ALLOWED_TOOLS,
        "--disallowedTools", SWEEP_DENIED_TOOLS,
    ]


def run_claude(prompt: str, timeout: int, verbose: bool) -> str:
    binary = claude_binary()
    command = build_command(binary, prompt)
    if verbose:
        print(
            f"running: {binary} -p <prompt> "
            "--allowedTools WebSearch,WebFetch --disallowedTools Bash,Write,Edit,...",
            file=sys.stderr,
        )

    # Proxy handling, settled empirically rather than by reasoning.
    #
    # WebFetch executes CLIENT-side, from this machine, so on a managed Mac it goes
    # through the corporate proxy, which refuses general web traffic ("proxy refused
    # the connection", curl exit 56). Direct egress from this machine does work. So the
    # proxy variables are stripped by default, which is the opposite of what you would
    # guess if you assumed the fetching happened on Anthropic's servers.
    #
    # If stripping the proxy ever breaks the CLI's own connection to Anthropic rather
    # than its fetches, set SWEEP_KEEP_PROXY=1 to inherit the environment unchanged.
    environment = dict(os.environ)
    if not os.environ.get("SWEEP_KEEP_PROXY"):
        for variable in (
            "HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
            "ALL_PROXY", "all_proxy", "APPLE_CLAUDE_CODE_PROXY_URL",
        ):
            environment.pop(variable, None)
        if verbose:
            print(
                "  proxy variables stripped so WebFetch can reach the open web "
                "(set SWEEP_KEEP_PROXY=1 to keep them)",
                file=sys.stderr,
            )
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=environment,
        )
    except FileNotFoundError as exc:
        raise HunterError(f"Could not execute {binary}: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise HunterError(
            f"The sweep exceeded {timeout}s. Raise --timeout, or narrow the search in "
            f"prompts/sweep.md."
        ) from exc
    if completed.returncode != 0:
        raise HunterError(
            f"claude exited {completed.returncode}: "
            f"{(completed.stderr or completed.stdout or '').strip()[:500]}"
        )
    return completed.stdout


def extract_json(raw: str) -> dict[str, Any]:
    """Pull the JSON object out of the model's output.

    Asked for bare JSON, a model still sometimes wraps it in a fence or adds a line of
    preamble. Tolerate that, but never guess at malformed JSON -- a parse failure is
    reported so a bad run is visible rather than silently empty.
    """
    text = raw.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1)
    else:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            text = text[start : end + 1]
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise HunterError(
            f"The sweep did not return parseable JSON ({exc}). First 400 characters of "
            f"the output:\n{raw.strip()[:400]}"
        ) from exc
    if not isinstance(payload, dict):
        raise HunterError("The sweep returned JSON that is not an object")
    return payload


def is_search_url(url: str) -> bool:
    """True when the URL indexes many properties rather than being one listing page.

    Allowlist first: if the URL carries a listing id in a known original-page shape, it
    is a listing page even if it also contains an index-looking substring.
    """
    lowered = url.lower()
    if any(re.search(pattern, lowered) for pattern in ORIGINAL_URL_PATTERNS):
        return False
    return any(marker in lowered for marker in SEARCH_URL_MARKERS)


def is_real_evidence(value: Any, claimed: Any) -> bool:
    """Reject evidence that satisfies a key-presence check while proving nothing.

    A bare "x", or a string that is just the value echoed back ("170"), is not a quote
    from a listing. This does not and cannot verify that the quote is genuine -- only a
    fetch of the page could -- but it removes the cheapest way past the guard.
    """
    text = clean_text(value)
    if not text or len(text) < MIN_EVIDENCE_CHARS:
        return False
    # "170" as evidence for covered_m2=170 is a restatement, not a source.
    return text.strip() != str(claimed).strip()


def coerce_types(record: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Drop fields hunter.normalise_record would reject, instead of letting one bad
    field abort the whole packet.

    hunter.parse_number/parse_bool raise HunterError on malformed input, and
    hunter.command_ingest aborts the entire import on the first raise. Since the packet
    file is appended to and persists, a single malformed field from one sweep would
    poison every later run over the same file -- losing every other genuine find. So
    each value is pre-flighted here and dropped individually if it will not survive.
    """
    cleaned = dict(record)
    dropped: list[str] = []
    for field in list(cleaned):
        value = cleaned[field]
        if value is None:
            continue
        try:
            if field in hunter.NUMERIC_FIELDS:
                hunter.parse_number(value, field)
            elif field in hunter.BOOLEAN_FIELDS:
                hunter.parse_bool(value, field)
            elif field in {"photo_urls", "distinctive_features", "risks"}:
                hunter.clean_list(value, field)
        except HunterError:
            cleaned.pop(field)
            dropped.append(field)
    return cleaned, dropped


def enforce_evidence(record: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Drop every factual field that lacks a supporting evidence quote.

    This is the structural anti-fabrication guard. The prompt asks the model not to
    invent facts; this makes inventing them ineffective, which is a much stronger
    guarantee than asking.

    It also strips the fields a discovery pass must never supply at all: see
    FORBIDDEN_FROM_SWEEP.
    """
    evidence = record.get("evidence")
    evidence = evidence if isinstance(evidence, dict) else {}
    dropped: list[str] = []
    cleaned = dict(record)

    for field in FORBIDDEN_FROM_SWEEP:
        if field in cleaned:
            cleaned.pop(field)
            dropped.append(field)

    for field in list(cleaned):
        if field in EVIDENCE_REQUIRED and not is_real_evidence(
            evidence.get(field), cleaned.get(field)
        ):
            cleaned.pop(field)
            dropped.append(field)
    return cleaned, dropped


def validate_records(payload: dict[str, Any], verbose: bool) -> tuple[list[dict[str, Any]], dict[str, int]]:
    listings = payload.get("listings")
    if not isinstance(listings, list):
        raise HunterError("The sweep response contained no 'listings' array")

    kept: list[dict[str, Any]] = []
    counts = {"received": len(listings), "no_url": 0, "search_url": 0, "no_address": 0,
              "unparseable": 0, "fields_dropped": 0, "bad_types_dropped": 0}

    for raw in listings:
        if not isinstance(raw, dict):
            counts["unparseable"] += 1
            continue
        url = clean_text(raw.get("url")) or ""
        if not url.startswith(("http://", "https://")):
            counts["no_url"] += 1
            continue
        if is_search_url(url):
            # The project requires original listing pages; a search URL cannot be
            # deduplicated or revisited reliably.
            counts["search_url"] += 1
            if verbose:
                print(f"  dropped search-results URL: {url[:90]}", file=sys.stderr)
            continue

        record, dropped = enforce_evidence(raw)
        counts["fields_dropped"] += len(dropped)
        if dropped and verbose:
            print(f"  {url[:60]}: dropped unevidenced {', '.join(sorted(dropped))}", file=sys.stderr)

        # Pre-flight types AFTER the evidence pass, so a malformed value cannot abort
        # the ingest of the whole packet later.
        record, bad_types = coerce_types(record)
        counts["bad_types_dropped"] += len(bad_types)
        if bad_types and verbose:
            print(f"  {url[:60]}: dropped malformed {', '.join(sorted(bad_types))}", file=sys.stderr)

        # Checked after the evidence pass: an unevidenced address is dropped above, and
        # hunter.py withholds an alert without one, so there is nothing to keep.
        if not clean_text(record.get("canonical_address")):
            counts["no_address"] += 1
            continue

        record["source"] = clean_text(record.get("source")) or "Web sweep"
        # Stamped, not setdefault: a model-supplied past date would place the record
        # before the report's `since` cutoff and hide it from every report.
        record["observed_at"] = iso_now()
        record.setdefault("market_stage", "listed")
        # Record how this was found, so a sweep-sourced fact is auditable later.
        record["fit_notes"] = " ".join(
            filter(None, [clean_text(record.get("fit_notes")), "Discovered by agentic web sweep."])
        )
        kept.append(record)

    return kept, counts


def command_check_tools(args: argparse.Namespace) -> int:
    """Report which web tools the headless CLI actually has, and whether egress works.

    The sweep depends on two things that are easy to get silently wrong on a managed
    machine: the CLI must expose a web tool, and that tool must be able to reach the
    open internet. A failure in either looks identical to "no listings today", which is
    the one outcome this project must never produce silently.
    """
    binary = claude_binary()
    print(f"claude binary: {binary}")

    probe = (
        "Answer in exactly three lines, nothing else.\n"
        "Line 1: TOOLS= followed by a comma-separated list of the tool names you can "
        "call right now.\n"
        "Line 2: fetch https://example.com and reply FETCH=OK or "
        "FETCH=FAIL:<short reason>\n"
        "Line 3: run a web search for 'zonaprop san isidro' and reply "
        "SEARCH=OK:<first result title> or SEARCH=FAIL:<short reason>"
    )

    for label, keep_proxy in (("proxy stripped (the sweep default)", False), ("proxy inherited", True)):
        print(f"\n=== {label} ===")
        environment = dict(os.environ)
        if not keep_proxy:
            for variable in (
                "HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
                "ALL_PROXY", "all_proxy", "APPLE_CLAUDE_CODE_PROXY_URL",
            ):
                environment.pop(variable, None)
        try:
            completed = subprocess.run(
                build_command(binary, probe),
                capture_output=True, text=True, timeout=args.timeout, env=environment,
            )
        except subprocess.TimeoutExpired:
            print(f"  timed out after {args.timeout}s")
            continue
        output = (completed.stdout or completed.stderr or "").strip()
        for line in output.splitlines():
            if line.strip():
                print(f"  {line.strip()[:160]}")

    print(
        "\nInterpretation:\n"
        "  FETCH=OK under 'proxy stripped'  -> the sweep will work as configured.\n"
        "  FETCH=OK only under 'proxy inherited' -> set SWEEP_KEEP_PROXY=1.\n"
        "  Both FAIL -> this machine cannot reach listing sites. Run the radar from a\n"
        "    personal machine or network instead; do not try to work around the proxy.\n"
        "  TOOLS missing WebSearch -> the sweep relies on WebFetch alone, which needs\n"
        "    concrete URLs. Recall will be much worse, so prefer a machine where search\n"
        "    is available."
    )
    return 0


def command_sweep(args: argparse.Namespace) -> int:
    # Read every config BEFORE the sweep. A config error discovered afterwards would
    # throw away a completed 15-minute search and every listing in it.
    targeting = hunter.read_config(Path(args.config))
    prompt = build_prompt(Path(args.prompt), args.max_results)
    if args.print_prompt:
        print(prompt)
        return 0

    raw = run_claude(prompt, args.timeout, verbose=not args.quiet)
    if args.raw_output:
        Path(args.raw_output).write_text(raw, encoding="utf-8")

    payload = extract_json(raw)
    records, counts = validate_records(payload, verbose=not args.quiet)

    for note in payload.get("skipped") or []:
        print(f"  skipped: {note}", file=sys.stderr)

    # Apply the same Priority A/B geography filter the API path uses, so both
    # discovery routes agree on what counts as on-target.
    on_target = [record for record in records if fetch_ml.relevant(record, targeting)]
    counts["off_target"] = len(records) - len(on_target)

    path = fetch_ml.packet_path(Path(args.output_dir), args.date)
    if args.dry_run:
        for record in on_target:
            print(json.dumps(record, ensure_ascii=False, sort_keys=True))
        print(f"[dry-run] {len(on_target)} records; would append to {path}", file=sys.stderr)
        print(json.dumps(counts, indent=2), file=sys.stderr)
        return 0

    written = fetch_ml.write_packet(on_target, path)
    print(json.dumps(
        {"discovered": len(on_target), "written": written, "packet": str(path), "filtered": counts},
        ensure_ascii=False,
    ))
    return 0


def parser() -> argparse.ArgumentParser:
    base = argparse.ArgumentParser(
        description="Agentic public-web discovery for the San Isidro radar"
    )
    base.add_argument("--prompt", default=str(DEFAULT_PROMPT))
    base.add_argument("--config", default=str(hunter.DEFAULT_CONFIG))
    base.add_argument("--output-dir", default=str(ROOT / "research"))
    base.add_argument("--date", help="packet date (YYYY-MM-DD); defaults to today in Argentina")
    base.add_argument("--max-results", type=int, default=12)
    base.add_argument("--timeout", type=int, default=900, help="seconds before the sweep is abandoned")
    base.add_argument("--dry-run", action="store_true", help="print records instead of writing the packet")
    base.add_argument("--print-prompt", action="store_true", help="show the assembled prompt and exit")
    base.add_argument("--raw-output", help="also save the model's raw output to this path")
    base.add_argument("--quiet", action="store_true")
    base.add_argument(
        "--check-tools",
        action="store_true",
        help="report which web tools the headless CLI has and whether egress works",
    )
    return base


def main(argv: Optional[list[str]] = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.check_tools:
            return command_check_tools(args)
        return command_sweep(args)
    except HunterError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

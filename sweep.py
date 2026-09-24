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
     see enforce_evidence(). The model cannot smuggle a number past this.
  2. A record without a working public original URL is dropped entirely.
  3. A URL that looks like a search-results or listing-index page is dropped: the
     project requires original listing pages, never search URLs.
  4. Output is validated against the packet contract, and anything unparseable is
     reported rather than silently skipped.

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

# Facts that must be backed by an evidence quote. Identity and provenance fields
# (source, url, observed_at) are exempt because they are not claims about the property.
EVIDENCE_REQUIRED = (
    hunter.NUMERIC_FIELDS
    | hunter.BOOLEAN_FIELDS
    | {"expenses", "condition", "orientation", "property_type", "street_quality",
       "publication_date", "market_stage", "listing_state"}
)

# A URL that indexes many properties is not an original listing page.
SEARCH_URL_MARKERS = (
    "/searchresult", "search?", "/listado", "/busqueda", "/s/", "?q=", "&q=",
    "/casas-en-venta", "/propiedades-en-venta", "/inmuebles-en-venta",
    "/resultados", "/filtro", "/ordenar",
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

    return f"""You are the discovery stage of a real-estate radar. Search the public web
and return structured findings. You are NOT writing a report for a human -- your entire
output is consumed by a program.

=== BUYER BRIEF ===
{brief}

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

  OPTIONAL, include ONLY what the page actually states:
{json.dumps(schema_fields.get("fields", []), indent=4)}
  plus: price_usd, covered_m2, total_m2, garden_m2, bedrooms, bathrooms, parking,
  units, expenses, expenses_ars, private_garden, private_pool, pool_potential,
  controlled_entrance, condition, property_type, street_quality, description, broker,
  broker_phone, photo_urls, distinctive_features, publication_date, market_stage,
  owner_direct, exceptional_layout, high_traffic, large_development,
  needs_major_renovation, security_concern, flood_risk.

=== THE RULE THAT MATTERS MOST ===
Every factual field you include MUST have a matching entry in that record's
"evidence" object, quoting or closely paraphrasing the listing text that supports it:

  "covered_m2": 170,
  "expenses_ars": 185000,
  "evidence": {{
    "covered_m2": "Listing states '170 m2 cubiertos'.",
    "expenses_ars": "Listing states 'Expensas $185.000'."
  }}

A field without evidence WILL BE DISCARDED by the consuming program, so including one
is wasted effort. If the page does not state something, OMIT the field. Do not infer,
estimate, convert currencies, or carry a number over from a similar property.

Only report price_usd when the listing is priced in USD. If it is in pesos, omit
price_usd and note the currency in fit_notes. Argentine listings quote both; read which
symbol the page uses, since "$" alone means pesos and "USD"/"U$S" means dollars.

Return at most {max_results} listings, best-fitting first. Prefer precision over
volume: three well-evidenced Priority A candidates beat twenty vague ones.
"""


def run_claude(prompt: str, timeout: int, verbose: bool) -> str:
    binary = claude_binary()
    command = [
        binary,
        "-p",
        prompt,
        "--output-format", "text",
        "--allowedTools", "WebSearch,WebFetch",
        "--permission-mode", "bypassPermissions",
    ]
    if verbose:
        print(f"running: {binary} -p <prompt> --allowedTools WebSearch,WebFetch", file=sys.stderr)

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
    lowered = url.lower()
    return any(marker in lowered for marker in SEARCH_URL_MARKERS)


def enforce_evidence(record: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Drop every factual field that lacks a supporting evidence entry.

    This is the structural anti-fabrication guard. The prompt asks the model not to
    invent facts; this makes inventing them ineffective, which is a much stronger
    guarantee than asking.
    """
    evidence = record.get("evidence")
    evidence = evidence if isinstance(evidence, dict) else {}
    supported = {key for key, value in evidence.items() if clean_text(value)}
    dropped: list[str] = []
    cleaned = dict(record)
    for field in list(cleaned):
        if field in EVIDENCE_REQUIRED and field not in supported:
            cleaned.pop(field)
            dropped.append(field)
    return cleaned, dropped


def validate_records(payload: dict[str, Any], verbose: bool) -> tuple[list[dict[str, Any]], dict[str, int]]:
    listings = payload.get("listings")
    if not isinstance(listings, list):
        raise HunterError("The sweep response contained no 'listings' array")

    kept: list[dict[str, Any]] = []
    counts = {"received": len(listings), "no_url": 0, "search_url": 0, "no_address": 0,
              "unparseable": 0, "fields_dropped": 0}

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
        if not clean_text(raw.get("canonical_address")):
            counts["no_address"] += 1
            continue

        record, dropped = enforce_evidence(raw)
        counts["fields_dropped"] += len(dropped)
        if dropped and verbose:
            print(f"  {url[:60]}: dropped unevidenced {', '.join(sorted(dropped))}", file=sys.stderr)

        record["source"] = clean_text(record.get("source")) or "Web sweep"
        record.setdefault("observed_at", iso_now())
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
                [binary, "-p", probe, "--output-format", "text",
                 "--allowedTools", "WebSearch,WebFetch",
                 "--permission-mode", "bypassPermissions"],
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
    targeting = hunter.read_config(Path(args.config))
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

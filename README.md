# San Isidro House Hunter

This is a working, evidence-first recurring real-estate radar. It has no portal
scraper: discovery is an agentic sweep over public original listing pages, and a local
SQLite history records what was found and prevents repeat alerts. MercadoLibre's API is
not a discovery source and measurably cannot be one, see
[MercadoLibre API: the measured 403 verdict](#mercadolibre-api-the-measured-403-verdict).
It runs unattended on a daily schedule, see [Run it unattended](#run-it-unattended).

## Recommended architecture

RADAR -> HISTORY/DEDUP -> ANALYST -> DEAL HUNTER -> ALERT

Do not start with three independent agents. Start with one reliable radar for 7–10 days, then split analysis and deal hunting if volume justifies it.

## Minimum viable architecture

```text
Public discovery (agentic web sweep) → evidence packet (.jsonl)
                                     → SQLite history + deduplication
                                     → Lasalle Chico evaluation → daily report
```

The discovery layer is `sweep.py`; see [Run it unattended](#run-it-unattended). It
searches publicly accessible original listings on Mercado
Libre, Zonaprop, Argenprop, Properati when reachable, and buyer-approved local
agency/developer/owner-direct pages. It does not scrape, sign in, defeat CAPTCHAs,
or use private/unofficial APIs. The report always retains the original listing URL,
not a search-result URL.

The SQLite layer stores each observation and its snapshot, direct source identities,
canonical URLs, fuzzy match rationale, and ambiguous possible duplicates. It uses:

- exact source listing IDs and canonical URLs first;
- then address, broker phone, photo URLs, dimensions, description overlap, and
  distinctive features;
- `UNCERTAIN` rather than an alert whenever the possible duplicate confidence is
  insufficient.

The first MVP intentionally uses public photo URL overlap only. Add downloaded-image
perceptual hashing only after the initial 7–10 day trial demonstrates that cross-post
deduplication needs it; it should not be a prerequisite for a safe first launch.

## Run the local core

No packages need to be installed; the core uses Python's standard library and
SQLite.

```sh
python3 hunter.py init
python3 hunter.py research-plan --mode deep
python3 hunter.py research-plan --mode change
python3 hunter.py ingest --input research/YYYY-MM-DD.jsonl --quiet
python3 hunter.py report
```

See [research/README.md](research/README.md) for the evidence-packet format. Values
that cannot be supported by a public original listing must be omitted or `null`.

Use a historical cut-off to reissue a report safely:

```sh
python3 hunter.py report --since 2026-09-20
```

Inspect the audit trail without changing it:

```sh
python3 hunter.py history --limit 50
```

`UNCERTAIN` records are intentionally withheld from alerts. Review and explicitly
resolve them before the next report; this avoids silently merging two homes that
happen to share an address or broker:

```sh
python3 hunter.py review
python3 hunter.py resolve --observation 42 --decision duplicate
# or: python3 hunter.py resolve --observation 42 --decision separate
```

## Run it unattended

The local core above still expects a human to produce the evidence packet. These pieces
close that gap and make the radar actually run on a schedule:

| Piece | What it does |
| --- | --- |
| `sweep.py` | **Discovery.** Drives the local `claude` CLI in headless mode (`claude -p`, WebSearch and WebFetch only) over public listing pages, and writes `research/YYYY-MM-DD.jsonl`. This is the primary discovery path. |
| `hunter.py` | Persistent SQLite history, cross-post dedup, freshness classification, Lasalle fit scoring, negotiation estimates, the daily report. Consumes the packet. |
| `notify.py` | Delivers the report to Telegram and email instead of stdout. |
| `run.sh` | One full pass: init, sweep, ingest, report, deliver. |
| `install-schedule.sh` | Installs the daily `launchd` job on this Mac. **This is the primary hosting path.** |
| `.github/workflows/daily.yml` | Optional cloud alternative. Needs an `ANTHROPIC_API_KEY` secret, because a GitHub runner has no Claude Code subscription to drive. |
| `fetch_ml.py` | Dormant for discovery. Retained for the OAuth diagnostics and for the shared packet helpers (`packet_path`, `write_packet`, `relevant`) that `sweep.py` imports. |

The reason discovery is an agentic sweep rather than an API client is measured, not
aesthetic: MercadoLibre refuses every search and item endpoint with `403` even to a
valid user token, and the sweep also reaches sources an ML client never could, such as
Zonaprop, Argenprop, local San Isidro agency inventory, developer pages, and
owner-direct posts.

One full pass, locally:

```sh
./run.sh --dry-run   # discover + report, resolve routing, send nothing
./run.sh             # the real thing
```

**Python 3.9 or newer is required** (`hunter.py` uses `zoneinfo`). `run.sh` probes for
a usable interpreter, because on a machine with Anaconda on the PATH `python3` often
resolves to an older base environment and fails with
`ModuleNotFoundError: No module named 'zoneinfo'`. When calling the scripts directly,
name the interpreter explicitly:

```sh
python3.13 hunter.py report
```

Override the probe with `PYTHON=/path/to/python3 ./run.sh`. `sweep.py` finds the Claude
CLI on the `PATH`; set `CLAUDE_BIN` if it lives somewhere unusual, and `SWEEP_ARGS` to
pass extra sweep flags through `run.sh`.

### What is automated and what is not

Discovery is automated for every named source at once, because the sweep does ordinary
public-web research rather than talking to one vendor's API. What it will not do is
bypass a login, a CAPTCHA, a paywall, a robots restriction, or a rate limit. A blocked
site is reported in the sweep's `skipped` list and left alone. That is a deliberate
limit, not an omission: it keeps the project on the right side of `AGENTS.md`.

The sweep instruction is assembled from two files, and both are required: `AGENTS.md`,
the buyer brief, and `prompts/sweep.md`, the recurring task (override the second with
`--prompt`). A missing file is a hard error rather than an empty string, because a sweep
that runs with no instructions returns plausible-looking rubbish, and silent degradation
is the worst outcome for a job nobody is watching. Inspect the assembled prompt with
`sweep.py --print-prompt`.

An automated record and a hand-researched one use the same packet format, so they are
indistinguishable downstream and can coexist in the same daily file. The packet writer
appends and never overwrites, and an append is idempotent on the listing URL, so a
second pass on the same day does not duplicate anything.

### The evidence rule, and what it costs you

This is the most important design decision in the project, so it is worth being blunt
about the trade-off it buys.

An LLM reading listing pages will happily produce a plausible `covered_m2` or
`expenses_ars`. A fabricated number is worse than a missing one here, because the entire
value of the radar is that its alerts can be trusted. Prompting a model not to invent
facts is not a guarantee. So the rule is enforced **in code**, by
`sweep.py`'s `enforce_evidence()`: every factual field must have a matching entry in that
record's `evidence` object quoting the listing, and a field without one is dropped before
it ever reaches history. Inventing a number is not blocked by persuasion, it is made
useless. `tests/test_sweep.py` pins the behaviour, including the case where the evidence
string is present but empty.

Three further drops happen at the same stage: a record with no working public URL, a
record whose URL is a search-results or index page rather than an original listing, and
a record with no published address.

**The consequence you will actually notice.** Facts that listings rarely publish, such
as the number of homes in the development, whether the entrance is controlled, and the
monthly expenses, stay absent, and the report renders them as `Not verified.` But
`🚨 ACT NOW` requires exactly those facts to be present and true. So a sweep-only
discovery usually lands at `⚡ VER HOY`, not at `🚨 ACT NOW`, and you or an agent enrich
it from there. That is intended. The tiers mean something only if the facts behind them
are real, and a radar that reached `ACT NOW` on inference would be a radar you stopped
believing.

### Price safety

`price_usd` is populated only when the listing itself is priced in USD. A peso price is
recorded as context and explicitly flagged, never converted, because there is no
verified exchange rate anywhere in the pipeline and inventing one would corrupt every
downstream price test. Every value carries its origin in the `evidence` map, so no figure
reaches an alert without a traceable source. `tests/test_automation.py` and
`tests/test_sweep.py` assert both properties.

### Set up the daily local schedule

This is the supported way to run the radar. It needs no cloud account and no API key,
because the sweep uses the Claude Code subscription already on this Mac.

**1. Create the local credential file.** A `launchd` job inherits almost no environment,
so `.env` is the only place a scheduled run gets credentials from:

```sh
cp .env.example .env
chmod 600 .env
```

Fill in the Telegram and SMTP values. Leave the MercadoLibre block empty; discovery does
not use it. Delivery channels are individually optional, and a channel with missing
credentials is skipped with a warning rather than failing the run.

```
TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, EMAIL_FROM, EMAIL_TO
```

**2. Confirm delivery works** before you trust a job you will not be watching:

```sh
python3 notify.py --text "radar test" --force-telegram
```

**3. Do one full pass by hand**, discovering and reporting but sending nothing:

```sh
./run.sh --dry-run
```

**4. Install the schedule:**

```sh
chmod +x install-schedule.sh       # one time, if it is not executable yet
./install-schedule.sh              # daily at 08:00 local time
./install-schedule.sh --at 7:30    # or at a specific local time
./install-schedule.sh --status     # loaded? last exit code? tail of logs/radar.log
./install-schedule.sh --run-now    # trigger a run immediately, for testing
./install-schedule.sh --uninstall  # remove the schedule; history in data/ is untouched
```

Re-running the installer reloads the job cleanly, so changing the time is just
`--at` again. Output goes to `logs/radar.log`; follow a run with `tail -f logs/radar.log`.

**Why `launchd` and not `cron`.** `launchd` runs a job that was missed because the Mac
was asleep or powered off, as soon as it wakes. `cron` silently skips it. On a laptop
that is the difference between a radar that works and one that quietly does not, and the
quiet failure is the dangerous one because it looks exactly like a calm market.

**The schedule uses the Mac's local time zone, not Argentina time.** From Amsterdam,
08:00 local is 03:00 or 04:00 in Buenos Aires, which is fine for a morning sweep of
overnight listings. When the machine moves to Buenos Aires around September 2026 the job
follows the system clock automatically and fires at 08:00 ART instead. Nothing to
migrate, but do not expect the effective Argentine hour to stay the same across the move.

Two smaller notes on the scheduled environment, both already handled for you: a
`launchd` job starts with a minimal `PATH` that excludes `/opt/homebrew/bin` where a
Homebrew Python usually lives, so the installer writes a `PATH` that `run.sh` can probe
along; and `run.sh` unsets the proxy variables for the run, because on an Apple-managed
Mac a local proxy hijacks outbound traffic and portal requests die with a connection
error.

### MercadoLibre API: the measured 403 verdict

Do not set up MercadoLibre auth as a prerequisite. It buys you nothing. This is recorded
here because it is the single most expensive thing to rediscover.

Measured on 2026-09-23 with a valid **user-context** token (`authorization_code` + PKCE,
`read` and `offline_access` scopes), via `tools/ml_probe.py`:

| Result | Endpoint |
| --- | --- |
| `200` | `/users/me` |
| `200` | `/categories/MLA1459` (public metadata, works even unauthenticated) |
| `403` | `/sites/MLA/search` in every variant: category, `q`, `seller_id`, unauthenticated |
| `403` | `/items/{id}`, so not even per-listing enrichment |
| `403` | `/users/{self}/items/search` |
| `403` | `/sites/MLA/domain_discovery/search` |
| `403` | `/trends/MLA/MLA1459` |

Every `403` is `PA_UNAUTHORIZED_RESULT_FROM_POLICIES` from MercadoLibre's PolicyAgent,
with an app-context token and with a user-context token alike. This is a deliberate
platform restriction on third-party access, not a scope or auth mistake, and re-running
the OAuth flow will not change it. MercadoLibre listings still reach the radar, as
public listing pages read by the sweep.

The OAuth machinery is kept as a diagnostic, so that if access ever reopens it can be
re-verified in a minute rather than rebuilt:

```sh
python3.13 fetch_ml.py --auth-login    # one-time browser authorization
python3.13 fetch_ml.py --auth-check    # which OAuth flow the app supports
python3.13 fetch_ml.py --probe         # API reachability and category id
python3.13 tools/ml_probe.py           # the full endpoint map above, one token
```

If those ever start returning `200` for search, `fetch_ml.py`'s `map_item()` and its
tests already encode a correct ML-item to packet mapping, so enabling it is a function
call rather than a rewrite.

### Alert routing

A report with candidates goes to Telegram *and* email. A `NO CHANGE` report goes to
email only, and Telegram stays silent. A radar that pings your phone every morning
gets muted within a week, and a muted radar is worse than none because it still looks
like it is working. The daily email is the proof-of-life that the job ran.

If the run itself fails, the cloud path sends a Telegram warning saying so explicitly.
Today's silence must never be mistaken for a quiet market. Locally, `--status` and
`logs/radar.log` serve the same purpose.

### Optional: the GitHub Actions path

`.github/workflows/daily.yml` runs the same chain in the cloud at 11:00 UTC, which is
08:00 ART year round since Argentina has no DST. It is an alternative, not a
requirement, and it differs from the local run in one way that matters: `sweep.py` drives
the `claude` CLI, and a runner has no Claude Code subscription, so the workflow needs an
`ANTHROPIC_API_KEY` repository secret and fails loudly with an explanatory error when it
is missing. Add the delivery credentials as repository secrets too, under
**Settings → Secrets and variables → Actions**, then trigger **Actions → Daily radar →
Run workflow** once with `dry_run` checked.

#### Where history lives in CI

This applies to the cloud path only. A local run simply keeps its history in `data/`,
which persists because it is the same machine every day.

`data/*.sqlite3` and `research/*.jsonl` are gitignored because they hold broker contact
details, but the freshness model collapses if history does not survive between runs: a
runner starts empty, so every listing would look `NEW` every morning. The workflow
therefore force-writes state to a dedicated `state` branch, which keeps `main` clean
while giving durable storage and a diffable audit trail of every price move.
`actions/cache` is deliberately not used: caches are evicted silently, and a silent
eviction looks exactly like a flood of new discoveries. The state globs deliberately do
not match `data/ml_refresh_token`; a live credential does not belong in git history,
private repo or not.

To inspect or reset the accumulated cloud history:

```sh
git fetch origin state && git checkout origin/state -- data research
git push origin --delete state   # start history over from empty
```

### Tests

```sh
PYTHONPATH=.:tests python3 -m unittest test_hunter test_automation test_sweep
```

## Codex

This section and the **Claude Code** one below describe alternative hosts, kept for
reference. The primary schedule is the local `launchd` job documented in
[Set up the daily local schedule](#set-up-the-daily-local-schedule).

Use this repository as the project context. `AGENTS.md` is already at the repo root.
After the manual trial, create a Codex Automation with `schedule.md` as the recurring
instruction. The automation needs normal public-web research capability and write
access to this repository so it can retain the local SQLite history.

Choose the **local project** environment for this radar, rather than an isolated
worktree: `data/house_hunter.sqlite3` is intentionally local cumulative state and
must survive from one run to the next. Keep the desktop app and computer running for
scheduled local-project work. The database is excluded from Git because it can retain
broker contact details and research history.

## Three-pass cadence and pre-market sources

The recommended cadence is 07:00 ART for the full scan and quick change scans at
11:00 and 17:00. The quick scans only look for new public listings, price changes,
and items that appeared since the prior scan; they trigger deeper work only for a
candidate that clears the initial Lasalle Chico screen.

[local_sources.json](config/local_sources.json) contains the verified public sites
for Rene Martin, Zárate Gestión Inmobiliaria, Morel, NARVAEZ, and Varela Kramer.
It is deliberately a public-source watchlist, not a contact or bulk-message tool.
The deep scan checks their public inventory, public developer pages, and public
"próximamente"/new-development signals before they are replicated by portals.

The current hard stops are explicit and editable: fewer than 120 m² of documented
private garden, more than 8 units, ARS 500k+ monthly expenses, or a buyer-rejected
micro-location. A very high-fit Priority A home up to USD 410k is labeled
`🟡 INVESTIGAR PRECIO`, never `ACT NOW`, until a credible route under the hard USD
380k ceiling is documented.

Codex supports background scheduled automations. Use the current GPT-5.6-Sol setting rather than any retired GPT-5.5 setting.

## Claude Code

Use the repo as the working directory. Use `/schedule` for a cloud routine if available on your plan, or Claude Managed Agents scheduled deployments. Paste the contents of `schedule.md` as the recurring prompt.

## First run

1. Create a private Git repository.
2. Add these files.
3. Run the radar manually once using the JSONL packet workflow.
4. Inspect false positives and adjust `config/targeting.json`—especially the explicit
   micro-area terms—before expanding geography.
5. Run daily for 7–10 days and review `UNCERTAIN` matches.
6. Add image hashing or separate analyst/deal-hunter roles only if the evidence shows
   they improve recall without increasing false alerts.

## Important

Portals can block automation or restrict scraping. Do not bypass access controls. Prefer public pages, permitted browser access, official APIs/feeds, or manual review links.

## Target behavior

The agent should find the next Lasalle Chico before it becomes obvious, not produce a giant list of generic houses.

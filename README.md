# San Isidro House Hunter

This is a working, evidence-first recurring real-estate radar. It has no portal
scraper: discovery uses MercadoLibre's official public API plus ordinary
browser/search research, while a local SQLite history records what was found and
prevents repeat alerts. It runs unattended on a daily schedule — see
[Run it unattended](#run-it-unattended).

## Recommended architecture

RADAR -> HISTORY/DEDUP -> ANALYST -> DEAL HUNTER -> ALERT

Do not start with three independent agents. Start with one reliable radar for 7–10 days, then split analysis and deal hunting if volume justifies it.

## Minimum viable architecture

```text
Public discovery (Codex web/browser) → evidence packet (.jsonl)
                                      → SQLite history + deduplication
                                      → Lasalle Chico evaluation → daily report
```

The discovery layer searches publicly accessible original listings on Mercado
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

The local core above still expects a human to produce the evidence packet. Three
pieces close that gap and make the radar actually run on a schedule:

| Piece | What it does |
| --- | --- |
| `fetch_ml.py` | Turns MercadoLibre's public REST API into a research packet automatically. |
| `notify.py` | Delivers the report to Telegram and email instead of stdout. |
| `.github/workflows/daily.yml` | Runs the whole chain daily at 08:00 ART and persists history. |

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
python3.13 fetch_ml.py --probe
```

Override the probe with `PYTHON=/path/to/python3 ./run.sh`.

### What is automated and what is not

`fetch_ml.py` covers MercadoLibre only, because it is the one named source with a
documented, permitted programmatic interface. Zonaprop, Argenprop, the local agency
pages, and owner-direct listings are still discovered the way `research-plan`
describes, by a human or an agent doing ordinary public-web research. That is a
deliberate limit, not an omission: it keeps the project on the right side of
`AGENTS.md`'s "do not bypass access controls" rule.

Both paths write the same packet format, so an automated discovery and a
hand-researched one are indistinguishable downstream. Automated and manual records
can coexist in the same daily packet; `fetch_ml.py` appends and never overwrites.

Facts MercadoLibre does not publish as structured attributes — unit count,
controlled entrance, expenses, street quality — stay `null`, which the report renders
as `Not verified.` A MercadoLibre-only discovery will therefore rarely reach
`🚨 ACT NOW` on its own. It reaches `⚡ VER HOY`, and you or an agent enrich it. This
is intended: the alert tiers mean something only if the facts behind them are real.

### Price safety

`price_usd` is populated only when MercadoLibre reports `currency_id == "USD"`. A
peso price is recorded as context and explicitly flagged, never converted, because
there is no verified rate in the pipeline. Every value `fetch_ml.py` emits carries its
origin in the `evidence` map, so no figure reaches an alert without a traceable
source. `tests/test_automation.py` asserts both properties.

### Setup

Confirm MercadoLibre auth works (see **MercadoLibre auth** below — a token is
required, an unauthenticated call returns `403`):

```sh
python3.13 fetch_ml.py --auth-check
```

Then confirm the category id is right:

```sh
python3.13 fetch_ml.py --probe
```

Zero results means the category id in [config/mercadolibre.json](config/mercadolibre.json)
is wrong, not that the market is empty.

Confirm delivery credentials before trusting the schedule:

```sh
python3 notify.py --text "radar test" --force-telegram
```

Environment variables. Delivery channels are optional — a channel with missing
credentials is skipped with a warning rather than failing the run. The MercadoLibre
values are not optional; discovery cannot run without them.

```
ML_CLIENT_ID, ML_CLIENT_SECRET, ML_REDIRECT_URI, ML_REFRESH_TOKEN   # required
TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, EMAIL_FROM, EMAIL_TO
GH_PAT              # fine-grained PAT, Secrets:write -- CI token rotation only
```

For the scheduled run, add the same names as repository secrets under
**Settings → Secrets and variables → Actions**. Then trigger **Actions → Daily radar
→ Run workflow** once with `dry_run` checked.

### MercadoLibre auth

MercadoLibre requires a token for search — an unauthenticated request returns
`403 forbidden`. Their OAuth supports **only** `authorization_code` and
`refresh_token` (anything else returns `unsupported_grant_type`), so there is no
stateless machine-to-machine shortcut and a one-time browser authorization is
unavoidable.

**One-time setup.** Create an app at
[developers.mercadolibre.com.ar/devcenter](https://developers.mercadolibre.com.ar/devcenter):

- The **Redirect URI** must be a static `https` URL with no variable parts, and every
  later request must match it character for character.
- Enable the **`read`** and **`offline_access`** scopes. Without `offline_access`
  MercadoLibre returns no refresh token at all and the scheduled run cannot work.

Then:

```sh
export ML_CLIENT_ID=...          # "App ID"
export ML_CLIENT_SECRET=...      # "Secret Key"
export ML_REDIRECT_URI=...       # exactly as configured on the app

python3.13 fetch_ml.py --auth-url          # prints the URL; open and authorize
python3.13 fetch_ml.py --exchange-code TG-xxxxx
python3.13 fetch_ml.py --auth-check        # confirms search actually works
```

Sign in as the **account administrator**. A collaborator/operator account cannot
grant access — MercadoLibre returns `invalid_operator_user_id`. The authorization code
expires in about 10 minutes.

**Token rotation is the part that bites.** A refresh token is single use: every
refresh invalidates it and returns a replacement, and only the most recent one is
accepted. A job that replays the original secret works exactly once. So:

- Locally, the rotated token is written to `data/ml_refresh_token` (gitignored, mode
  600) and preferred over `ML_REFRESH_TOKEN` on the next run. Nothing to maintain.
- In CI, the workflow writes the rotated token back to the `ML_REFRESH_TOKEN`
  repository secret via `gh secret set`. That needs a **fine-grained PAT with
  `Secrets: write`** on this repo, stored as the `GH_PAT` secret — the built-in
  `GITHUB_TOKEN` cannot write secrets. Without `GH_PAT` the run fails loudly rather
  than silently burning the token.

The credential goes back into repository secrets rather than onto the `state` branch
on purpose: a live token does not belong in git history, private repo or not.

Refresh tokens also expire after **6 months**, and an app unused for 4 months is
invalidated. When that happens `--auth-check` reports `invalid_grant`; redo
`--auth-url`.

Two other documented `403` causes worth knowing, since they look identical to missing
auth: a **blocked IP**, and **missing scopes**. If `--auth-check` works locally but
the scheduled run gets 403, suspect IP blocking on the GitHub runner and check
MercadoLibre's "Gestionar IPs de una aplicación" settings.

### Alert routing

A report with candidates goes to Telegram *and* email. A `NO CHANGE` report goes to
email only, and Telegram stays silent. A radar that pings your phone every morning
gets muted within a week, and a muted radar is worse than none because it still looks
like it is working. The daily email is the proof-of-life that the job ran.

If the workflow itself fails, it sends a Telegram warning saying so explicitly —
today's silence must never be mistaken for a quiet market.

### Where history lives in CI

`data/*.sqlite3` and `research/*.jsonl` are gitignored because they hold broker
contact details, but the freshness model collapses if history does not survive
between runs. The workflow therefore force-writes state to a dedicated `state`
branch, which keeps `main` clean while giving durable storage and a diffable audit
trail of every price move. `actions/cache` is deliberately not used: caches are
evicted silently, and a silent eviction looks exactly like a flood of new
discoveries.

To inspect or reset the accumulated history:

```sh
git fetch origin state && git checkout origin/state -- data research
git push origin --delete state   # start history over from empty
```

### Tests

```sh
PYTHONPATH=.:tests python3 -m unittest test_hunter test_automation
```

## Codex

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

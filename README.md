# San Isidro House Hunter

This is a ready-to-use starter for a recurring real-estate radar.

## Recommended architecture

RADAR -> HISTORY/DEDUP -> ANALYST -> DEAL HUNTER -> ALERT

Do not start with three independent agents. Start with one reliable radar for 7–10 days, then split analysis and deal hunting if volume justifies it.

## Codex

Use this repository as the project context. Put `AGENTS.md` at the repo root. Then create a Codex Automation with `schedule.md` as the recurring instruction.

Codex supports background scheduled automations. Use the current GPT-5.6-Sol setting rather than any retired GPT-5.5 setting.

## Claude Code

Use the repo as the working directory. Use `/schedule` for a cloud routine if available on your plan, or Claude Managed Agents scheduled deployments. Paste the contents of `schedule.md` as the recurring prompt.

## First run

1. Create a private Git repository.
2. Add these files.
3. Run the radar manually once.
4. Inspect false positives and adjust the geographic boundary.
5. Run daily for 7–10 days.
6. Add persistent history (SQLite/JSON) and screenshot/photo deduplication.
7. Only then add separate analyst/deal-hunter subagents.

## Important

Portals can block automation or restrict scraping. Do not bypass access controls. Prefer public pages, permitted browser access, official APIs/feeds, or manual review links.

## Target behavior

The agent should find the next Lasalle Chico before it becomes obvious, not produce a giant list of generic houses.

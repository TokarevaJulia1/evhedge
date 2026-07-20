@echo off
rem =========================================================================
rem BLAST Bounty 2026 Season 2 (CS2): pre-match odds watcher.
rem
rem Every 15 minutes pulls into data\blast2026.db:
rem   - the winner outright board (32 teams, YES/NO),
rem   - Match Winner leg prices for every BLAST Bounty match (once Gamma
rem     lists them -- as of 2026-07-16 no match events exist yet under the
rem     "cs2" tag; the tournament's first Ro32 match (FURIA-Sharks) is
rem     2026-07-24. Same mechanism as EWC, nothing to change once they
rem     appear -- title_filter is deliberately "Bounty 2026 Season 2"
rem     (no leading Blast/BLAST) since Gamma's own event titles for this
rem     tournament are inconsistently cased ("BLAST Bounty..." on the
rem     winner event, "Blast Bounty..." on every prop event seen so far).
rem   - resolves for finished maps/series,
rem   - auto-recorded predictions the first time each leg's book clears
rem     the quality trigger (see evhedge/auto_predict.py), model half fed
rem     by --stage-ranks (all 32 teams start at n=5 -- see the file's own
rem     header for the update protocol every round after Ro32/Ro16).
rem
rem ORDER OF OPERATIONS: aliases (Spirit/Falcons/Aurora -> existing Dota
rem canon) and both config files (this repo's commit) must already be
rem committed/on disk BEFORE this script's first run -- same race the EWC
rem watcher caused once already (see CHANGELOG.md): a fresh `pull` process
rem spawns every 15 minutes and reads whatever's on disk AT THAT MOMENT,
rem so starting the loop before the alias/config files exist means the
rem first pass(es) canonicalize against an incomplete map.
rem
rem A match that has gone LIVE is skipped automatically inside
rem `evhedge pull` (the "skipped ... live" column): the pre-match price
rem series ends at throw-in, in-play prices are not entry prices.
rem Stop with Ctrl+C (or close the window).
rem
rem --stage-ranks configs\blast_bounty_s2_stage_ranks.yaml MAINTENANCE
rem DISCIPLINE: stale every round played, predictions are immutable -- a
rem prediction fixed against a stale n is a permanently lost calibration
rem point. Update it BEFORE the next round's books list (evening after
rem results, ahead of the next morning's listings) -- checked via
rem `evhedge autopredict status`'s "с моделью" column.
rem
rem PandaScore step (independent source: schedules/results, deadline
rem warnings) needs PANDASCORE_TOKEN set in THIS environment before the
rem loop starts -- not committed anywhere, set it yourself
rem (`set PANDASCORE_TOKEN=...` before running this script, or a
rem permanent user/system env var).
rem --league-id 4321 = BLAST Premier (the parent league, confirmed live
rem 2026-07-20) -- PandaScore had NOT yet created a "BLAST Bounty 2026
rem Season 2" series/matches as of that check (same timing gap as the
rem Gamma match-listing one above); syncing by league_id returns 0 rows
rem harmlessly until PandaScore adds it, no code change needed then.
rem `deadlines --hours 2` prints its own warning (via warn_console) for
rem any leg starting inside 2h with no predictions row yet -- that IS
rem the watcher-loop warning-log requirement, not a separate mechanism.
rem =========================================================================

cd /d "%~dp0.."

:loop
echo.
echo [%date% %time%] evhedge pull...
python -m evhedge.cli pull --tournament "BLAST Bounty 2026 Season 2" ^
  --board "blast-bounty-2026-season-2-winner-20260709030103861:winner" ^
  --matches "cs2:Bounty 2026 Season 2" --matches-since 2026-07-20 ^
  --db data\blast2026.db ^
  --stage-ranks configs\blast_bounty_s2_stage_ranks.yaml
if errorlevel 1 echo [%date% %time%] pull failed (network?) -- retrying in 15 min.

echo [%date% %time%] evhedge pandascore sync...
python -m evhedge.cli pandascore sync --db data\blast2026.db ^
  --tournament "BLAST Bounty 2026 Season 2" --league-id 4321
if errorlevel 1 (
  echo [%date% %time%] pandascore sync failed -- PANDASCORE_TOKEN set? network?
) else (
  python -m evhedge.cli deadlines --db data\blast2026.db ^
    --tournament "BLAST Bounty 2026 Season 2" --hours 2
)

timeout /t 900 /nobreak >nul
goto loop

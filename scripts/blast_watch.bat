@echo off
rem =========================================================================
rem BLAST Bounty 2026 Season 2 (CS2): pre-match odds watcher.
rem
rem Every 15 minutes pulls into data\blast2026.db:
rem   - the winner outright board (32 teams, YES/NO),
rem   - Match Winner leg prices for every BLAST Bounty match. CORRECTED
rem     2026-07-24 against the real live matches (as of 2026-07-16 these
rem     didn't exist yet, so this was guessed and wrong on both counts):
rem       - tag is "counter-strike-2", NOT "cs2" -- confirmed live via a
rem         real match event's own tags ([esports, counter-strike-2,
rem         games, sports]); "cs2" silently returned 0 match events the
rem         whole time, never errored.
rem       - title_filter is "BLAST Bounty" (all-caps BLAST, no "2026
rem         Season 2", no "Qualifier"), NOT "Bounty 2026 Season 2" --
rem         real match titles read like "Counter-Strike: Sharks vs FURIA
rem         (BO3) - BLAST Bounty Qualifier", which never contained the
rem         old filter string. "BLAST Bounty" (exact case) is chosen
rem         deliberately: it does NOT match the prop-bet events (those
rem         are titled "Blast Bounty 2026 Season 2: ..." -- lowercase
rem         "last", different from match titles' all-caps "BLAST") --
rem         confirmed no false-positive overlap. NOTE: this stage's
rem         matches are labeled "Qualifier"; if stage-2 (QF/SF/GF) later
rem         turns out to use a differently-worded title, this filter may
rem         need ANOTHER correction on contact with THAT live branch --
rem         same discipline as everywhere else in this project.
rem       - --matches-since is ALSO corrected: 2026-07-15, not 2026-07-20.
rem         Gotcha confirmed live: this param filters on the Gamma EVENT's
rem         own creation/listing date, NOT the real match's startTime --
rem         these match events were all created 2026-07-17 even though
rem         their matches run 2026-07-21 through 07-24, so the old
rem         2026-07-20 cutoff silently excluded every one of them (0
rem         markets seen, no error -- easy to miss).
rem   - resolves for finished maps/series (per-map resolves for CS2 now
rem     work too -- collect.py previously only recognized Dota's "Game N
rem     Winner" market naming; CS2 uses "Map N Winner", fixed in the
rem     same commit as this file),
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
rem --league-id 5426 = "BLAST Bounty" -- CORRECTED 2026-07-21. Originally
rem guessed as 4321 "BLAST Premier" (a genuinely related, similarly-named
rem league) before any real BLAST Bounty match existed in PandaScore to
rem check against -- wrong: PandaScore tracks BLAST Bounty as its OWN
rem top-level league, not nested under BLAST Premier. Confirmed live by
rem looking up a real team's (Vitality, id 3455) upcoming match directly
rem and reading its actual league_id/name off the response, rather than
rem trusting the guessed id another time. tournament_id 21474 ("Qualifier",
rem this stage) also confirmed live via GET /tournaments/{id}/brackets --
rem that endpoint returns the FULL known stage structure in one call,
rem including not-yet-decided future rounds as TBD-vs-TBD placeholders
rem (24 rows: 16 Round-of-32 pairs + 8 Round-of-16 slots waiting on them).
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
  --matches "counter-strike-2:BLAST Bounty" --matches-since 2026-07-15 ^
  --db data\blast2026.db ^
  --stage-ranks configs\blast_bounty_s2_stage_ranks.yaml
if errorlevel 1 echo [%date% %time%] pull failed (network?) -- retrying in 15 min.

echo [%date% %time%] evhedge pandascore sync...
python -m evhedge.cli pandascore sync --db data\blast2026.db ^
  --tournament "BLAST Bounty 2026 Season 2" --league-id 5426
if errorlevel 1 (
  echo [%date% %time%] pandascore sync failed -- PANDASCORE_TOKEN set? network?
) else (
  python -m evhedge.cli deadlines --db data\blast2026.db ^
    --tournament "BLAST Bounty 2026 Season 2" --hours 2
)

timeout /t 900 /nobreak >nul
goto loop

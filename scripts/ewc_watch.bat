@echo off
rem =========================================================================
rem EWC 2026 Dota 2: pre-match odds watcher.
rem
rem Every 15 minutes pulls into data\ewc2026.db:
rem   - the winner outright board (24 teams, YES/NO),
rem   - the region-winner aggregate board,
rem   - Match Winner series prices for every EWC match (leg snapshots),
rem   - resolves for finished games.
rem
rem A match that has gone LIVE is skipped automatically inside
rem `evhedge pull` (the "skipped ... live" column): the pre-match price
rem series ends at throw-in, in-play prices are not entry prices.
rem New match days keep appearing through July 19 -- the loop just keeps
rem running. Stop with Ctrl+C (or close the window).
rem =========================================================================

cd /d "%~dp0.."

:loop
echo.
echo [%date% %time%] evhedge pull...
python -m evhedge.cli pull --tournament "EWC 2026 Dota 2" ^
  --board "ewc-dota-2-winner-20260622213517296:winner" ^
  --board "which-region-will-win-the-2026-esports-world-cup-dota-2-tournament-20260701152000046:region_winner" ^
  --matches "dota-2:Esports World Cup" --matches-since 2026-07-01 ^
  --db data\ewc2026.db
if errorlevel 1 echo [%date% %time%] pull failed (network?) -- retrying in 15 min.

timeout /t 900 /nobreak >nul
goto loop

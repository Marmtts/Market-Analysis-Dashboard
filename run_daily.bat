@echo off
REM =========================================================
REM run_daily.bat - uruchamia XTB Trend Watch z aktywnym venv
REM =========================================================
REM Użycie w Harmonogramie zadań Windows:
REM   Program/skrypt:       C:\xtb_trend_watch\run_daily.bat
REM   Rozpocznij w (Start in): C:\xtb_trend_watch
REM
REM Nie musisz pamiętać dokładnej ścieżki do python.exe w venv -
REM ten skrypt sam ją wylicza względem swojej własnej lokalizacji.

cd /d "%~dp0"
call venv\Scripts\activate.bat
python -m src.main --config config.yaml >> logs\daily_run.log 2>&1

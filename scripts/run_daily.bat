@echo off
REM =========================================================
REM run_daily.bat - uruchamia Market Analysis Dashboard z aktywnym venv
REM =========================================================
REM Użycie w Harmonogramie zadań Windows:
REM   Program/skrypt:       C:\xtb_trend_watch\scripts\run_daily.bat
REM   Rozpocznij w (Start in): C:\xtb_trend_watch  (albo zostaw puste -
REM                             skrypt i tak ustawia katalog roboczy sam)
REM
REM Nie musisz pamiętać dokładnej ścieżki do python.exe w venv -
REM ten skrypt sam ją wylicza względem KATALOGU GŁÓWNEGO repo (jeden
REM poziom nad scripts/, gdzie leży ten plik).

cd /d "%~dp0.."
call venv\Scripts\activate.bat
python -m src.main --config config.yaml >> logs\daily_run.log 2>&1

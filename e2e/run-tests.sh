#!/usr/bin/env bash
# Odpala ŚWIEŻĄ instancję dashboardu (osobna baza SQLite w katalogu tymczasowym,
# pusta watchlista, bez analizy w tle - patrz test-config.yaml) na osobnym
# porcie, uruchamia testy Playwrighta, potem sprząta - zero dotykania Twoich
# prawdziwych danych w data/xtb_trend_watch.db czy Twojego działającego serwera.
set -euo pipefail
cd "$(dirname "$0")/.."

PORT="${E2E_PORT:-8123}"
TMP_DB_DIR="$(mktemp -d)"
export XTW_DB_PATH="$TMP_DB_DIR/test.db"
export PYTHONIOENCODING=utf-8

# Lokalnie (Windows) odpalamy z venv jak reszta projektu; w CI (ubuntu-latest,
# brak venv - zależności idą prosto z pip install) wystarczy python3 na PATH.
if [ -f venv/Scripts/python.exe ]; then
  PYTHON=venv/Scripts/python.exe
else
  PYTHON=python3
fi

"$PYTHON" -m src.web_app --config e2e/test-config.yaml --port "$PORT" &
SERVER_PID=$!
cleanup() {
  kill "$SERVER_PID" 2>/dev/null || true
  rm -rf "$TMP_DB_DIR"
}
trap cleanup EXIT

echo "Czekam na serwer testowy na porcie $PORT..."
for _ in $(seq 1 30); do
  if curl -sf "http://localhost:$PORT/" > /dev/null; then
    echo "Serwer gotowy."
    break
  fi
  sleep 1
done

cd e2e
mvn test -DbaseUrl="http://localhost:$PORT" "$@"

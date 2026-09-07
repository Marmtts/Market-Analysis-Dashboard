"""
db.py
------
Lekka warstwa persystencji (SQLite, bez ORM) dla dashboardu webowego:
- watchlista spółek (dodawanie/usuwanie z poziomu UI, przetrwa restart),
- cache ostatnich wyników analizy (żeby dashboard od razu pokazał coś
  sensownego przy starcie, zanim skończy się pierwszy cykl analizy w tle).

Baza tworzona jest automatycznie przy pierwszym uruchomieniu w
`data/xtb_trend_watch.db`. Jeśli watchlista w bazie jest pusta, zasilamy ją
startowo z `watchlist` w config.yaml (jednorazowo - potem config.yaml już
nie jest źródłem prawdy dla watchlisty, jest nim baza).
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from .json_utils import sanitize_for_json

DB_PATH = Path("data") / "xtb_trend_watch.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS watchlist (
    ticker TEXT PRIMARY KEY,
    xtb_symbol TEXT NOT NULL,
    name TEXT NOT NULL,
    added_at TEXT NOT NULL DEFAULT (datetime('now')),
    source TEXT NOT NULL DEFAULT 'manual'   -- 'manual' | 'seed' | 'discovery'
);

CREATE TABLE IF NOT EXISTS results_cache (
    id INTEGER PRIMARY KEY CHECK (id = 1),  -- zawsze jeden wiersz - nadpisywany
    generated_at TEXT NOT NULL,
    payload_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS run_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL DEFAULT (datetime('now')),
    level TEXT NOT NULL,     -- 'info' | 'warning' | 'error'
    message TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS score_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    ts TEXT NOT NULL DEFAULT (datetime('now')),
    score REAL,
    category TEXT NOT NULL,
    signal TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'watchlist'   -- 'watchlist' | 'discovery'
);
CREATE INDEX IF NOT EXISTS idx_score_history_ticker ON score_history(ticker, ts);

CREATE TABLE IF NOT EXISTS discovery_history (
    ticker TEXT PRIMARY KEY,
    first_proposed_at TEXT NOT NULL DEFAULT (datetime('now')),
    last_proposed_at TEXT NOT NULL DEFAULT (datetime('now')),
    times_proposed INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS portfolio (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    shares REAL NOT NULL,
    buy_price REAL NOT NULL,
    buy_date TEXT NOT NULL,
    notes TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'open',   -- 'open' | 'closed'
    sell_price REAL,
    sell_date TEXT,
    currency TEXT NOT NULL DEFAULT 'USD'
);

CREATE TABLE IF NOT EXISTS portfolio_equity_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL DEFAULT (datetime('now')),
    currency TEXT NOT NULL,
    total_value REAL NOT NULL,
    total_cost REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_portfolio_equity_currency ON portfolio_equity_history(currency, ts);

CREATE TABLE IF NOT EXISTS portfolio_equity_combined (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL DEFAULT (datetime('now')),
    base_currency TEXT NOT NULL,
    total_value REAL NOT NULL,
    total_cost REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_portfolio_equity_combined ON portfolio_equity_combined(base_currency, ts);


CREATE TABLE IF NOT EXISTS training_examples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    headline TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT '',
    published_date TEXT NOT NULL,        -- YYYY-MM-DD dnia publikacji
    price_at_headline REAL,
    price_fwd_5d REAL,
    price_fwd_10d REAL,
    price_fwd_20d REAL,
    return_pct_5d REAL,
    return_pct_10d REAL,
    return_pct_20d REAL,
    llm_score REAL,                       -- ocena sentymentu LLM w tamtym "momencie"
    llm_summary TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_training_examples_ticker ON training_examples(ticker, published_date);
-- UNIQUE zapobiega duplikatom przy ponownym uruchomieniu collect_training_data.py
-- (np. po przerwaniu w połowie) - pozwala bezpiecznie wznowić od miejsca przerwania.
CREATE UNIQUE INDEX IF NOT EXISTS idx_training_examples_unique
    ON training_examples(ticker, headline, published_date);
"""

# Ile "zdjęć" werdyktu trzymamy na spółkę w score_history - zapobiega
# nieograniczonemu rozrostowi bazy przy wielu miesiącach regularnych cykli.
MAX_SCORE_HISTORY_PER_TICKER = 500


def init_db(seed_watchlist: list[dict] | None = None) -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _connect() as conn:
        conn.executescript(_SCHEMA)
        # Migracje: kolumny dodane po utworzeniu bazy przez wcześniejszą wersję
        # kodu - CREATE TABLE IF NOT EXISTS ich nie doda do istniejącej tabeli.
        for stmt in [
            "ALTER TABLE score_history ADD COLUMN market_price REAL",
            "ALTER TABLE portfolio ADD COLUMN status TEXT NOT NULL DEFAULT 'open'",
            "ALTER TABLE portfolio ADD COLUMN sell_price REAL",
            "ALTER TABLE portfolio ADD COLUMN sell_date TEXT",
            "ALTER TABLE portfolio ADD COLUMN currency TEXT NOT NULL DEFAULT 'USD'",
        ]:
            try:
                conn.execute(stmt)
            except sqlite3.OperationalError:
                pass  # kolumna już istnieje
        if seed_watchlist:
            count = conn.execute("SELECT COUNT(*) FROM watchlist").fetchone()[0]
            if count == 0:
                for c in seed_watchlist:
                    conn.execute(
                        "INSERT OR IGNORE INTO watchlist (ticker, xtb_symbol, name, source) "
                        "VALUES (?, ?, ?, 'seed')",
                        (c["ticker"], c.get("xtb_symbol", c["ticker"]), c.get("name", c["ticker"])),
                    )
        conn.commit()


@contextmanager
def _connect():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def get_watchlist() -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT ticker, xtb_symbol, name, source FROM watchlist ORDER BY added_at ASC"
        ).fetchall()
        return [dict(r) for r in rows]


def add_company(ticker: str, name: str, xtb_symbol: str | None = None,
                 source: str = "manual") -> bool:
    """Zwraca True, jeśli dodano; False, jeśli ticker już istnieje."""
    ticker = ticker.strip().upper()
    if not ticker:
        return False
    with _connect() as conn:
        try:
            conn.execute(
                "INSERT INTO watchlist (ticker, xtb_symbol, name, source) VALUES (?, ?, ?, ?)",
                (ticker, xtb_symbol or f"{ticker} (sprawdź w XTB)", name or ticker, source),
            )
            conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False


def remove_company(ticker: str) -> bool:
    ticker = ticker.strip().upper()
    with _connect() as conn:
        cur = conn.execute("DELETE FROM watchlist WHERE ticker = ?", (ticker,))
        conn.commit()
        return cur.rowcount > 0


def save_results_cache(generated_at: str, payload: dict) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO results_cache (id, generated_at, payload_json) VALUES (1, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET generated_at = excluded.generated_at, "
            "payload_json = excluded.payload_json",
            (generated_at, json.dumps(payload, ensure_ascii=False)),
        )
        conn.commit()


def load_results_cache() -> dict | None:
    with _connect() as conn:
        row = conn.execute("SELECT generated_at, payload_json FROM results_cache WHERE id = 1").fetchone()
        if not row:
            return None
        payload = json.loads(row["payload_json"])
        payload["generated_at"] = row["generated_at"]
        # sanityzacja na wypadek starych wpisów zapisanych PRZED poprawką NaN
        return sanitize_for_json(payload)


def log_event(level: str, message: str) -> None:
    with _connect() as conn:
        conn.execute("INSERT INTO run_log (level, message) VALUES (?, ?)", (level, message))
        # trzymamy tylko ostatnie 500 wpisów, żeby log nie rósł w nieskończoność
        conn.execute(
            "DELETE FROM run_log WHERE id NOT IN "
            "(SELECT id FROM run_log ORDER BY id DESC LIMIT 500)"
        )
        conn.commit()


def get_recent_logs(limit: int = 100) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT ts, level, message FROM run_log ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in reversed(rows)]


# =====================================================================
# Historia werdyktów per spółka (do znaczników na wykresie ceny)
# =====================================================================

def _trim_score_history(conn, ticker: str) -> None:
    """Przycina score_history do MAX_SCORE_HISTORY_PER_TICKER najnowszych
    wpisów (wg ts) na spółkę - bez tego tabela rosłaby bez ograniczeń przy
    wielu miesiącach cykli na żywo lub przy masowym backfillu historycznym."""
    conn.execute(
        "DELETE FROM score_history WHERE ticker = ? AND id NOT IN "
        "(SELECT id FROM score_history WHERE ticker = ? ORDER BY ts DESC LIMIT ?)",
        (ticker, ticker, MAX_SCORE_HISTORY_PER_TICKER),
    )


def record_score_snapshot(ticker: str, score: float | None, category: str,
                           signal: str, source: str = "watchlist",
                           market_price: float | None = None) -> None:
    """Zapisuje 'zdjęcie' wyniku analizy dla spółki w danym momencie -
    używane potem do rysowania znaczników (WARTO/UNIKAJ/...) na wykresie
    cenowym oraz do panelu skuteczności (porównanie werdyktu z późniejszą
    ceną tej samej spółki)."""
    ticker = ticker.upper()
    with _connect() as conn:
        conn.execute(
            "INSERT INTO score_history (ticker, score, category, signal, source, market_price) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (ticker, score, category, signal, source, market_price),
        )
        _trim_score_history(conn, ticker)
        conn.commit()


def record_score_snapshot_at(ticker: str, ts: str, score: float | None, category: str,
                              signal: str, source: str, market_price: float | None) -> None:
    """Jak record_score_snapshot(), ale pozwala jawnie podać znacznik czasu -
    używane przez backfill_history.py do wstawiania SYMULOWANYCH, historycznych
    'zdjęć' werdyktu z konkretną datą z przeszłości (np. sprzed roku)."""
    ticker = ticker.upper()
    with _connect() as conn:
        conn.execute(
            "INSERT INTO score_history (ticker, ts, score, category, signal, source, market_price) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (ticker, ts, score, category, signal, source, market_price),
        )
        _trim_score_history(conn, ticker)
        conn.commit()


def clear_backfill_history(ticker: str | None = None) -> int:
    """Usuwa wcześniej wygenerowane wpisy backfillu (source='backfill') -
    przydatne przed ponownym uruchomieniem skryptu z innymi parametrami,
    żeby nie mnożyć duplikatów przy kolejnych próbach."""
    with _connect() as conn:
        if ticker:
            cur = conn.execute(
                "DELETE FROM score_history WHERE source = 'backfill' AND ticker = ?",
                (ticker.upper(),),
            )
        else:
            cur = conn.execute("DELETE FROM score_history WHERE source = 'backfill'")
        conn.commit()
        return cur.rowcount


def record_discovery_proposal(ticker: str) -> None:
    """Zapamiętuje, że AI zaproponowało tę spółkę - używane do 'cooldownu'
    (patrz get_recently_proposed_tickers), żeby nie proponować w kółko
    tego samego kandydata w kolejnych cyklach."""
    ticker = ticker.strip().upper()
    with _connect() as conn:
        conn.execute(
            "INSERT INTO discovery_history (ticker) VALUES (?) "
            "ON CONFLICT(ticker) DO UPDATE SET "
            "last_proposed_at = datetime('now'), times_proposed = times_proposed + 1",
            (ticker,),
        )
        conn.commit()


def get_recently_proposed_tickers(cooldown_days: int = 14) -> list[str]:
    """Zwraca tickery zaproponowane przez AI w ciągu ostatnich cooldown_days
    dni - te są wykluczane z kolejnych propozycji, żeby uniknąć powtórek."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT ticker FROM discovery_history WHERE last_proposed_at >= datetime('now', ?)",
            (f"-{cooldown_days} days",),
        ).fetchall()
        return [r["ticker"] for r in rows]


def get_effectiveness_stats(min_age_days: int = 14) -> dict:
    """Ocenia PRZESZŁE werdykty narzędzia w świetle PÓŹNIEJSZEJ, najnowszej
    znanej ceny tej samej spółki - wyłącznie na podstawie danych już
    zebranych w score_history, bez żadnych dodatkowych zapytań API.

    Dla każdej spółki: werdykty starsze niż `min_age_days` względem
    najnowszego zapisu tej spółki są porównywane z ceną z tego najnowszego
    zapisu. To orientacyjny 'rachunek sumienia', nie pełny, rygorystyczny
    backtest (do tego służy backtest.py)."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT ticker, ts, category, market_price FROM score_history "
            "WHERE market_price IS NOT NULL ORDER BY ticker ASC, ts ASC"
        ).fetchall()

    by_ticker: dict[str, list[dict]] = {}
    for r in rows:
        by_ticker.setdefault(r["ticker"], []).append(dict(r))

    buckets: dict[str, list[float]] = {}

    for ticker, entries in by_ticker.items():
        if len(entries) < 2:
            continue
        latest = entries[-1]
        try:
            latest_ts = datetime.strptime(latest["ts"], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        latest_price = latest["market_price"]

        for entry in entries[:-1]:
            try:
                entry_ts = datetime.strptime(entry["ts"], "%Y-%m-%d %H:%M:%S")
            except ValueError:
                continue
            if (latest_ts - entry_ts).days < min_age_days:
                continue
            entry_price = entry["market_price"]
            if not entry_price:
                continue
            forward_return_pct = (latest_price - entry_price) / entry_price * 100
            buckets.setdefault(entry["category"], []).append(forward_return_pct)

    result = {}
    for category, returns in buckets.items():
        wins = [r for r in returns if r > 0]
        result[category] = {
            "count": len(returns),
            "win_rate_pct": round(len(wins) / len(returns) * 100, 1),
            "avg_return_pct": round(sum(returns) / len(returns), 2),
            "best_pct": round(max(returns), 2),
            "worst_pct": round(min(returns), 2),
        }
    return sanitize_for_json(result)


def get_score_history(ticker: str, limit: int = 400) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT ts, score, category, signal, source FROM score_history "
            "WHERE ticker = ? ORDER BY ts ASC LIMIT ?",
            (ticker.upper(), limit),
        ).fetchall()
        return sanitize_for_json([dict(r) for r in rows])

# =====================================================================
# Portfel użytkownika (posiadane pozycje)
# =====================================================================

def add_position(ticker: str, shares: float, buy_price: float, buy_date: str, notes: str = "") -> int:
    ticker = ticker.strip().upper()
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO portfolio (ticker, shares, buy_price, buy_date, notes) VALUES (?, ?, ?, ?, ?)",
            (ticker, shares, buy_price, buy_date, notes),
        )
        conn.commit()
        return cur.lastrowid


def get_portfolio(status: str = "open") -> list[dict]:
    """status: 'open' (domyślnie, aktywne pozycje), 'closed' (sprzedane),
    albo 'all' (obie)."""
    with _connect() as conn:
        if status == "all":
            rows = conn.execute(
                "SELECT * FROM portfolio ORDER BY buy_date DESC"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM portfolio WHERE status = ? ORDER BY buy_date DESC", (status,)
            ).fetchall()
        return [dict(r) for r in rows]


def remove_position(position_id: int) -> bool:
    with _connect() as conn:
        cur = conn.execute("DELETE FROM portfolio WHERE id = ?", (position_id,))
        conn.commit()
        return cur.rowcount > 0


def update_position(position_id: int, ticker: str, shares: float, buy_price: float,
                     buy_date: str, notes: str = "") -> bool:
    ticker = ticker.strip().upper()
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE portfolio SET ticker = ?, shares = ?, buy_price = ?, buy_date = ?, notes = ? "
            "WHERE id = ?",
            (ticker, shares, buy_price, buy_date, notes, position_id),
        )
        conn.commit()
        return cur.rowcount > 0


def close_position(position_id: int, sell_price: float, sell_date: str) -> bool:
    """Oznacza pozycję jako sprzedaną - NIE usuwa jej, zachowuje w historii
    zamkniętych transakcji do rozliczenia zrealizowanego zysku/straty."""
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE portfolio SET status = 'closed', sell_price = ?, sell_date = ? WHERE id = ?",
            (sell_price, sell_date, position_id),
        )
        conn.commit()
        return cur.rowcount > 0


def reopen_position(position_id: int) -> bool:
    """Cofa sprzedaż (np. pomyłkowe kliknięcie) - przywraca pozycję do
    statusu 'open' i czyści dane sprzedaży."""
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE portfolio SET status = 'open', sell_price = NULL, sell_date = NULL WHERE id = ?",
            (position_id,),
        )
        conn.commit()
        return cur.rowcount > 0


def set_position_currency(position_id: int, currency: str) -> None:
    with _connect() as conn:
        conn.execute("UPDATE portfolio SET currency = ? WHERE id = ?", (currency, position_id))
        conn.commit()


def get_closed_summary() -> dict:
    """Zbiorcze statystyki WSZYSTKICH zamkniętych transakcji, osobno per
    waluta (żeby nie sumować USD z PLN) - liczba transakcji, win rate,
    łączny zrealizowany zysk/strata."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT shares, buy_price, sell_price, currency FROM portfolio WHERE status = 'closed'"
        ).fetchall()

    by_currency: dict[str, dict] = {}
    for r in rows:
        currency = r["currency"] or "USD"
        realized = (r["sell_price"] - r["buy_price"]) * r["shares"]
        bucket = by_currency.setdefault(currency, {"count": 0, "wins": 0, "realized_pl": 0.0})
        bucket["count"] += 1
        bucket["realized_pl"] += realized
        if realized > 0:
            bucket["wins"] += 1

    result = {}
    for currency, b in by_currency.items():
        result[currency] = {
            "count": b["count"],
            "win_rate_pct": round(b["wins"] / b["count"] * 100, 1) if b["count"] else 0,
            "realized_pl": round(b["realized_pl"], 2),
        }
    return result

    # =====================================================================
# Dane treningowe (fine-tuning / kalibracja sentymentu LLM)
# =====================================================================

def _sanitize_text(s) -> str:
    """Usuwa uszkodzone/samotne surogaty Unicode (np. rozbite emoji z newsów
    StockTwits/Twitter przechodzących przez Finnhub), które SQLite/UTF-8 nie
    potrafi bezpiecznie zapisać. Reszta tekstu zostaje nietknięta."""
    if s is None:
        return ""
    return str(s).encode("utf-8", errors="surrogatepass").decode("utf-8", errors="ignore")


def add_training_example(record: dict) -> int | None:
    """Zwraca id wstawionego wiersza, albo None jeśli dokładnie taki sam
    przykład (ten sam ticker+nagłówek+data) już istniał (patrz UNIQUE index) -
    dzięki temu ponowne uruchomienie skryptu zbierającego dane jest bezpieczne
    i nie tworzy duplikatów."""
    with _connect() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO training_examples "
            "(ticker, headline, source, published_date, price_at_headline, "
            "price_fwd_5d, price_fwd_10d, price_fwd_20d, "
            "return_pct_5d, return_pct_10d, return_pct_20d, llm_score, llm_summary) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (record["ticker"], _sanitize_text(record["headline"]), _sanitize_text(record.get("source", "")),
             record["published_date"], record.get("price_at_headline"),
             record.get("price_fwd_5d"), record.get("price_fwd_10d"), record.get("price_fwd_20d"),
             record.get("return_pct_5d"), record.get("return_pct_10d"), record.get("return_pct_20d"),
             record.get("llm_score"), _sanitize_text(record.get("llm_summary", ""))),
        )
        conn.commit()
        return cur.lastrowid if cur.rowcount > 0 else None


def training_example_exists(ticker: str, headline: str, published_date: str) -> bool:
    """Sprawdza, czy dany przykład JUŻ jest w bazie - używane do pomijania
    (bez ponownego, kosztownego wywołania LLM) nagłówków przetworzonych
    w poprzednim, przerwanym uruchomieniu collect_training_data.py."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM training_examples WHERE ticker = ? AND headline = ? AND published_date = ? LIMIT 1",
            (ticker.upper(), _sanitize_text(headline), published_date),
        ).fetchone()
        return row is not None


def get_training_examples(ticker: str | None = None) -> list[dict]:
    with _connect() as conn:
        if ticker:
            rows = conn.execute(
                "SELECT * FROM training_examples WHERE ticker = ? ORDER BY published_date ASC",
                (ticker.upper(),),
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM training_examples ORDER BY ticker, published_date ASC").fetchall()
        return [dict(r) for r in rows]


def count_training_examples() -> int:
    with _connect() as conn:
        return conn.execute("SELECT COUNT(*) FROM training_examples").fetchone()[0]

# =====================================================================
# Krzywa kapitału portfela (equity curve) - "zdjęcia" łącznej wartości
# w czasie, osobno per waluta, zapisywane przy każdym pełnym cyklu analizy.
# =====================================================================

def record_portfolio_equity_snapshot(currency: str, total_value: float, total_cost: float) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO portfolio_equity_history (currency, total_value, total_cost) VALUES (?, ?, ?)",
            (currency, total_value, total_cost),
        )
        # Limit rozrostu - 2000 punktów na walutę to przy cyklu co godzinę
        # ponad 80 dni historii, więcej niż potrzeba do sensownego wykresu.
        conn.execute(
            "DELETE FROM portfolio_equity_history WHERE currency = ? AND id NOT IN "
            "(SELECT id FROM portfolio_equity_history WHERE currency = ? ORDER BY ts DESC LIMIT 2000)",
            (currency, currency),
        )
        conn.commit()


def get_portfolio_equity_curve(currency: str) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT ts, total_value, total_cost FROM portfolio_equity_history "
            "WHERE currency = ? ORDER BY ts ASC",
            (currency.upper(),),
        ).fetchall()
        return [dict(r) for r in rows]


def get_equity_currencies() -> list[str]:
    with _connect() as conn:
        rows = conn.execute("SELECT DISTINCT currency FROM portfolio_equity_history").fetchall()
        return [r["currency"] for r in rows]


def record_portfolio_equity_combined_snapshot(base_currency: str, total_value: float, total_cost: float) -> None:
    """Zapisuje 'zdjęcie' ŁĄCZNEJ wartości portfela (wszystkie waluty
    przeliczone na base_currency KURSEM Z MOMENTU TEGO CYKLU) - dzięki temu
    historia w czasie jest dokładna, nie przybliżeniem dzisiejszym kursem
    zastosowanym wstecz."""
    with _connect() as conn:
        conn.execute(
            "INSERT INTO portfolio_equity_combined (base_currency, total_value, total_cost) VALUES (?, ?, ?)",
            (base_currency.upper(), total_value, total_cost),
        )
        conn.execute(
            "DELETE FROM portfolio_equity_combined WHERE base_currency = ? AND id NOT IN "
            "(SELECT id FROM portfolio_equity_combined WHERE base_currency = ? ORDER BY ts DESC LIMIT 2000)",
            (base_currency.upper(), base_currency.upper()),
        )
        conn.commit()


def get_portfolio_equity_combined_curve(base_currency: str) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT ts, total_value, total_cost FROM portfolio_equity_combined "
            "WHERE base_currency = ? ORDER BY ts ASC",
            (base_currency.upper(),),
        ).fetchall()
        return [dict(r) for r in rows]
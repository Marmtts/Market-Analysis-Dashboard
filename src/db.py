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
import math
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

CREATE TABLE IF NOT EXISTS news_cache (
    ticker TEXT NOT NULL,
    month_key TEXT NOT NULL,        -- "YYYY-MM"
    headlines_json TEXT NOT NULL,   -- lista Headline zserializowana jako JSON
    fetched_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (ticker, month_key)
);

CREATE TABLE IF NOT EXISTS fx_rate_cache (
    cache_key TEXT PRIMARY KEY,
    rate REAL NOT NULL,
    stored_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS dividends (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    currency TEXT NOT NULL DEFAULT 'USD',
    pay_date TEXT NOT NULL,
    amount_gross REAL NOT NULL,
    withholding_tax REAL,           -- podatek u źródła potrącony przez brokera (dodatnia liczba) - NULL, jeśli nieznany
    source TEXT NOT NULL DEFAULT 'manual',   -- 'manual' | 'xtb_import'
    xtb_cash_op_id TEXT,            -- ID operacji gotówkowej z raportu XTB - do idempotentnego importu
    notes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_dividends_ticker ON dividends(ticker, pay_date);
CREATE UNIQUE INDEX IF NOT EXISTS idx_dividends_xtb_cash_op ON dividends(xtb_cash_op_id)
    WHERE xtb_cash_op_id IS NOT NULL;
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
            # Własne poziomy użytkownika (opcjonalne) - nadpisują stop-loss z ATR
            # i dodają własną cenę docelową do alertów cenowych.
            "ALTER TABLE portfolio ADD COLUMN custom_stop REAL",
            "ALTER TABLE portfolio ADD COLUMN custom_target REAL",
            # Rzeczywisty kurs wymiany (natywna waluta -> portfolio.base_currency)
            # zastosowany przy KONKRETNEJ transakcji - broker (np. XTB) dolicza
            # własną marżę do kursu rynkowego, więc "ile naprawdę zapłaciłem/
            # dostałem w PLN" różni się od przeliczenia bieżącym/NBP kursem.
            # Wypełniane automatycznie przy imporcie XTB (patrz xtb_import.py -
            # Purchase/Sale Value z raportu) albo ręcznie przy dodawaniu pozycji.
            # Używane WYŁĄCZNIE w widokach informacyjnych (portfolio łączne,
            # krzywa TWR) - podsumowanie podatkowe zostaje przy kursie NBP,
            # bo tego wymaga prawo, niezależnie od realnego kursu brokera.
            "ALTER TABLE portfolio ADD COLUMN buy_fx_rate REAL",
            "ALTER TABLE portfolio ADD COLUMN sell_fx_rate REAL",
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

def add_position(ticker: str, shares: float, buy_price: float, buy_date: str, notes: str = "",
                 custom_stop: float | None = None, custom_target: float | None = None,
                 buy_fx_rate: float | None = None) -> int:
    ticker = ticker.strip().upper()
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO portfolio (ticker, shares, buy_price, buy_date, notes, custom_stop, custom_target, "
            "buy_fx_rate) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (ticker, shares, buy_price, buy_date, notes, custom_stop, custom_target, buy_fx_rate),
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
                     buy_date: str, notes: str = "",
                     custom_stop: float | None = None, custom_target: float | None = None,
                     buy_fx_rate: float | None = None) -> bool:
    ticker = ticker.strip().upper()
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE portfolio SET ticker = ?, shares = ?, buy_price = ?, buy_date = ?, notes = ?, "
            "custom_stop = ?, custom_target = ?, buy_fx_rate = ? WHERE id = ?",
            (ticker, shares, buy_price, buy_date, notes, custom_stop, custom_target, buy_fx_rate, position_id),
        )
        conn.commit()
        return cur.rowcount > 0


def close_position(position_id: int, sell_price: float, sell_date: str,
                    sell_fx_rate: float | None = None) -> bool:
    """Oznacza pozycję jako sprzedaną - NIE usuwa jej, zachowuje w historii
    zamkniętych transakcji do rozliczenia zrealizowanego zysku/straty.
    sell_fx_rate - opcjonalny, rzeczywisty kurs wymiany brokera przy tej
    transakcji (np. z marżą XTB); używany tylko do przeliczeń poglądowych
    (podsumowanie łączne, TWR), nigdy do podsumowania podatkowego (NBP)."""
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE portfolio SET status = 'closed', sell_price = ?, sell_date = ?, "
            "sell_fx_rate = COALESCE(?, sell_fx_rate) WHERE id = ?",
            (sell_price, sell_date, sell_fx_rate, position_id),
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


def get_position_cash_flows(currency: str | None = None) -> list[dict]:
    """Zwraca WSZYSTKIE zdarzenia przepływu gotówki portfela (kupno = wpływ,
    sprzedaż = odpływ), na bazie tabeli `portfolio` (otwarte i zamknięte
    pozycje). Każde kupno/sprzedaż ma już dokładną datę i kwotę w tej
    tabeli - to JEST historia przepływów potrzebna do policzenia
    prawdziwego (TWR) zwrotu portfela, bez dodawania osobnej tabeli
    przepływów. `currency` opcjonalnie filtruje do jednej waluty notowania
    (krzywa per-walutowa); bez filtra - wszystkie pozycje (krzywa łączna,
    przeliczana na walutę bazową w report.py)."""
    with _connect() as conn:
        query = ("SELECT ticker, shares, buy_price, buy_date, sell_price, sell_date, currency, "
                  "buy_fx_rate, sell_fx_rate FROM portfolio")
        params: tuple = ()
        if currency:
            query += " WHERE currency = ?"
            params = (currency.upper(),)
        rows = [dict(r) for r in conn.execute(query, params).fetchall()]

    flows: list[dict] = []
    for r in rows:
        flows.append({
            "date": r["buy_date"], "amount": r["shares"] * r["buy_price"],
            "currency": r["currency"], "kind": "in", "fx_rate": r.get("buy_fx_rate"),
        })
        if r["sell_date"] and r["sell_price"] is not None:
            flows.append({
                "date": r["sell_date"], "amount": r["shares"] * r["sell_price"],
                "currency": r["currency"], "kind": "out", "fx_rate": r.get("sell_fx_rate"),
            })
    return flows

# =====================================================================
# Cache newsów historycznych (Finnhub) - unika ponownego pobierania tych
# samych miesięcy nagłówków w KAŻDYM cyklu. Miesiące STARSZE niż bieżący
# są traktowane jako "zamknięte" (nigdy się nie zmienią) i cache'owane
# BEZTERMINOWO - tylko bieżący, wciąż "otwarty" miesiąc jest odświeżany
# przy każdym cyklu (patrz news_history.py).
# =====================================================================

def get_cached_news_months(ticker: str) -> dict[str, list[dict]]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT month_key, headlines_json FROM news_cache WHERE ticker = ?",
            (ticker.upper(),),
        ).fetchall()
        return {r["month_key"]: json.loads(r["headlines_json"]) for r in rows}


def save_news_month_to_cache(ticker: str, month_key: str, headlines: list[dict]) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO news_cache (ticker, month_key, headlines_json, fetched_at) "
            "VALUES (?, ?, ?, datetime('now')) "
            "ON CONFLICT(ticker, month_key) DO UPDATE SET "
            "headlines_json = excluded.headlines_json, fetched_at = excluded.fetched_at",
            (ticker.upper(), month_key, json.dumps(headlines, ensure_ascii=False)),
        )
        conn.commit()

# =====================================================================
# Trwały cache kursów walut - przetrwa restart serwera. Kurs HISTORYCZNY
# (konkretny dzień, do podsumowania podatkowego) nie zmienia się nigdy,
# więc cache'ujemy go WIECZNIE (ttl_seconds=None). Kurs BIEŻĄCY ma krótkie
# TTL (patrz fx_rates.py) - trwałość na dysku i tak oszczędza jedno
# zapytanie sieciowe zaraz po restarcie serwera.
# =====================================================================

def get_cached_fx_rate(cache_key: str, ttl_seconds: int | None) -> float | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT rate, stored_at FROM fx_rate_cache WHERE cache_key = ?", (cache_key,)
        ).fetchone()
    if not row:
        return None
    if ttl_seconds is not None:
        stored_at = datetime.strptime(row["stored_at"], "%Y-%m-%d %H:%M:%S")
        age_seconds = (datetime.utcnow() - stored_at).total_seconds()
        if age_seconds > ttl_seconds:
            return None
    return row["rate"]


def save_fx_rate_to_cache(cache_key: str, rate: float) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO fx_rate_cache (cache_key, rate, stored_at) VALUES (?, ?, datetime('now')) "
            "ON CONFLICT(cache_key) DO UPDATE SET rate = excluded.rate, stored_at = excluded.stored_at",
            (cache_key, rate),
        )
        conn.commit()


# =====================================================================
# Import raportu XTB - idempotentny (bezpieczny do wielokrotnego uruchamiania)
# =====================================================================

def get_imported_xtb_ids() -> set[str]:
    """Zwraca zbiór ID pozycji XTB już wcześniej zaimportowanych (odczytane
    ze znacznika [XTB:<id>] w notatkach) - używane do pominięcia duplikatów
    przy ponownym imporcie tego samego lub nowszego pliku."""
    with _connect() as conn:
        rows = conn.execute("SELECT notes FROM portfolio WHERE notes LIKE '%[XTB:%'").fetchall()
    ids = set()
    for r in rows:
        notes = r["notes"] or ""
        start = notes.find("[XTB:")
        if start == -1:
            continue
        end = notes.find("]", start)
        if end != -1:
            ids.add(notes[start + 5:end])
    return ids


def backfill_xtb_fx_rate(xtb_id: str, buy_fx_rate: float | None, sell_fx_rate: float | None) -> bool:
    """Uzupełnia buy_fx_rate/sell_fx_rate dla JUŻ zaimportowanej pozycji XTB
    (dopasowanej po znaczniku [XTB:<id>] w notatce), TYLKO tam gdzie te pola
    są jeszcze puste (COALESCE) - nigdy nie nadpisuje ręcznie wpisanego kursu.
    Pozwala uzupełnić kursy w pozycjach zaimportowanych PRZED wprowadzeniem
    tej funkcji przez zwykłe ponowne wczytanie tego samego pliku XTB, bez
    tworzenia duplikatów (import i tak je pomija jako już zaimportowane -
    ten backfill działa właśnie w tej samej ścieżce pominięcia)."""
    if buy_fx_rate is None and sell_fx_rate is None:
        return False
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE portfolio SET buy_fx_rate = COALESCE(buy_fx_rate, ?), "
            "sell_fx_rate = COALESCE(sell_fx_rate, ?) "
            "WHERE notes LIKE ? AND (buy_fx_rate IS NULL OR sell_fx_rate IS NULL)",
            (buy_fx_rate, sell_fx_rate, f"%[XTB:{xtb_id}]%"),
        )
        conn.commit()
        return cur.rowcount > 0


def close_previously_imported_xtb_position(xtb_id: str, sell_price: float, sell_date: str,
                                            buy_fx_rate: float | None = None,
                                            sell_fx_rate: float | None = None) -> bool:
    """Zamyka pozycję zaimportowaną WCZEŚNIEJ z XTB jako otwartą (dopasowaną po
    znaczniku [XTB:<id>] w notatce), gdy TA SAMA pozycja pojawia się jako
    zamknięta w nowszym eksporcie - bez tego ponowny import po sprzedaży u
    brokera zostawiałby pozycję wiecznie otwartą w dashboardzie, bo import
    traktuje znany xtb_id jako już zaimportowany i tylko dogrywa kurs
    (patrz backfill_xtb_fx_rate). Działa tylko na pozycjach status='open' -
    już zamknięte tym samym mechanizmem obsługuje backfill_xtb_fx_rate."""
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE portfolio SET status = 'closed', sell_price = ?, sell_date = ?, "
            "buy_fx_rate = COALESCE(buy_fx_rate, ?), sell_fx_rate = COALESCE(sell_fx_rate, ?) "
            "WHERE notes LIKE ? AND status = 'open'",
            (sell_price, sell_date, buy_fx_rate, sell_fx_rate, f"%[XTB:{xtb_id}]%"),
        )
        conn.commit()
        return cur.rowcount > 0


def get_imported_dividend_xtb_ids() -> set[str]:
    """Zbiór ID operacji gotówkowych (Cash Operations) już zaimportowanych jako
    dywidendy - używane do pominięcia duplikatów przy ponownym imporcie tego
    samego lub nowszego raportu XTB."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT xtb_cash_op_id FROM dividends WHERE xtb_cash_op_id IS NOT NULL"
        ).fetchall()
    return {r["xtb_cash_op_id"] for r in rows}


def add_dividend(ticker: str, currency: str, pay_date: str, amount_gross: float,
                  withholding_tax: float | None = None, source: str = "manual",
                  xtb_cash_op_id: str | None = None, notes: str = "") -> int:
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO dividends (ticker, currency, pay_date, amount_gross, withholding_tax, "
            "source, xtb_cash_op_id, notes) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (ticker, currency, pay_date, amount_gross, withholding_tax, source, xtb_cash_op_id, notes),
        )
        conn.commit()
        return cur.lastrowid


def get_dividends(ticker: str | None = None) -> list[dict]:
    with _connect() as conn:
        if ticker:
            rows = conn.execute(
                "SELECT * FROM dividends WHERE ticker = ? ORDER BY pay_date DESC, id DESC", (ticker,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM dividends ORDER BY pay_date DESC, id DESC").fetchall()
    return [dict(r) for r in rows]


def delete_dividend(dividend_id: int) -> bool:
    with _connect() as conn:
        cur = conn.execute("DELETE FROM dividends WHERE id = ?", (dividend_id,))
        conn.commit()
        return cur.rowcount > 0


def add_position_full(ticker: str, shares: float, buy_price: float, buy_date: str,
                       notes: str = "", status: str = "open",
                       sell_price: float | None = None, sell_date: str | None = None,
                       currency: str = "USD", buy_fx_rate: float | None = None,
                       sell_fx_rate: float | None = None) -> int:
    """Jak add_position(), ale pozwala od razu ustawić status/sprzedaż/walutę -
    używane przez import XTB, żeby zamknięte transakcje trafiały od razu
    jako zamknięte, a nie jako otwarte wymagające ręcznej sprzedaży.
    buy_fx_rate/sell_fx_rate - rzeczywisty kurs z raportu XTB, patrz komentarz
    przy migracji kolumn w init_db()."""
    ticker = ticker.strip().upper()
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO portfolio (ticker, shares, buy_price, buy_date, notes, status, "
            "sell_price, sell_date, currency, buy_fx_rate, sell_fx_rate) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (ticker, shares, buy_price, buy_date, notes, status, sell_price, sell_date, currency,
             buy_fx_rate, sell_fx_rate),
        )
        conn.commit()
        return cur.lastrowid


# =====================================================================
# Kopia zapasowa (eksport/import JSON) - watchlista + pełny portfel
# (otwarte i zamknięte pozycje). Cache'y, historia werdyktów i krzywe
# kapitału celowo NIE są częścią kopii - odbudowują się z kolejnych cykli.
# =====================================================================

BACKUP_APP_ID = "xtb_trend_watch"
BACKUP_VERSION = 1
MAX_BACKUP_ROWS = 5000


def export_backup() -> dict:
    with _connect() as conn:
        watchlist = [dict(r) for r in conn.execute(
            "SELECT ticker, xtb_symbol, name, source FROM watchlist ORDER BY added_at ASC"
        ).fetchall()]
        portfolio = [dict(r) for r in conn.execute(
            "SELECT ticker, shares, buy_price, buy_date, notes, status, sell_price, sell_date, "
            "currency, custom_stop, custom_target, buy_fx_rate, sell_fx_rate FROM portfolio ORDER BY id ASC"
        ).fetchall()]
        dividends = [dict(r) for r in conn.execute(
            "SELECT ticker, currency, pay_date, amount_gross, withholding_tax, source, notes "
            "FROM dividends ORDER BY id ASC"
        ).fetchall()]
    return {
        "app": BACKUP_APP_ID,
        "version": BACKUP_VERSION,
        "exported_at": datetime.now().isoformat(timespec="seconds"),
        "watchlist": watchlist,
        "portfolio": portfolio,
        "dividends": dividends,
    }


def _valid_date_str(value) -> bool:
    try:
        datetime.strptime(str(value), "%Y-%m-%d")
        return True
    except ValueError:
        return False


def _positive_number(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def import_backup(data) -> dict:
    """Wczytuje kopię zapasową ATOMOWO (albo wszystko, albo nic - przy
    nieoczekiwanym błędzie transakcja jest wycofywana) i IDEMPOTENTNIE:
    spółki już obecne na watchliście oraz pozycje identyczne z istniejącymi
    są pomijane, więc wielokrotny import tego samego pliku nie tworzy
    duplikatów. Niepoprawne wiersze są pomijane z ostrzeżeniem."""
    if not isinstance(data, dict) or data.get("app") != BACKUP_APP_ID:
        raise ValueError("To nie jest plik kopii zapasowej XTB Trend Watch.")
    if data.get("version") != BACKUP_VERSION:
        raise ValueError(f"Nieobsługiwana wersja kopii zapasowej: {data.get('version')!r}.")

    watchlist = data.get("watchlist") or []
    portfolio = data.get("portfolio") or []
    dividends = data.get("dividends") or []  # opcjonalne - brak w kopiach sprzed tej funkcji
    if not isinstance(watchlist, list) or not isinstance(portfolio, list) or not isinstance(dividends, list):
        raise ValueError("Uszkodzona struktura pliku kopii zapasowej.")
    if len(watchlist) + len(portfolio) + len(dividends) > MAX_BACKUP_ROWS:
        raise ValueError(f"Zbyt duży plik kopii (limit {MAX_BACKUP_ROWS} wierszy).")

    result = {"watchlist_added": 0, "watchlist_skipped": 0,
              "positions_added": 0, "positions_skipped": 0,
              "dividends_added": 0, "dividends_skipped": 0, "warnings": []}

    def warn(msg: str) -> None:
        if len(result["warnings"]) < 20:
            result["warnings"].append(msg)

    with _connect() as conn:
        for i, c in enumerate(watchlist, 1):
            ticker = str(c.get("ticker", "")).strip().upper() if isinstance(c, dict) else ""
            if not ticker:
                warn(f"Watchlista, wiersz {i}: brak tickera - pominięto.")
                continue
            cur = conn.execute(
                "INSERT OR IGNORE INTO watchlist (ticker, xtb_symbol, name, source) VALUES (?, ?, ?, ?)",
                (ticker, str(c.get("xtb_symbol") or f"{ticker} (sprawdź w XTB)"),
                 str(c.get("name") or ticker), str(c.get("source") or "backup")),
            )
            result["watchlist_added" if cur.rowcount > 0 else "watchlist_skipped"] += 1

        for i, p in enumerate(portfolio, 1):
            if not isinstance(p, dict):
                warn(f"Portfel, wiersz {i}: nieprawidłowy format - pominięto.")
                continue
            ticker = str(p.get("ticker", "")).strip().upper()
            shares = _positive_number(p.get("shares"))
            buy_price = _positive_number(p.get("buy_price"))
            buy_date = p.get("buy_date")
            status = p.get("status") or "open"
            if not ticker or shares is None or buy_price is None or not _valid_date_str(buy_date) \
                    or status not in ("open", "closed"):
                warn(f"Portfel, wiersz {i} ({ticker or '?'}): niepoprawne dane pozycji - pominięto.")
                continue

            sell_price = sell_date = None
            if status == "closed":
                sell_price = _positive_number(p.get("sell_price"))
                sell_date = p.get("sell_date")
                if sell_price is None or not _valid_date_str(sell_date):
                    warn(f"Portfel, wiersz {i} ({ticker}): zamknięta pozycja bez poprawnej sprzedaży - pominięto.")
                    continue

            currency = str(p.get("currency") or "USD").strip().upper()[:10]
            custom_stop = _positive_number(p.get("custom_stop"))
            custom_target = _positive_number(p.get("custom_target"))
            buy_fx_rate = _positive_number(p.get("buy_fx_rate"))
            sell_fx_rate = _positive_number(p.get("sell_fx_rate"))
            notes = _sanitize_text(p.get("notes", ""))

            duplicate = conn.execute(
                "SELECT 1 FROM portfolio WHERE ticker = ? AND shares = ? AND buy_price = ? AND buy_date = ? "
                "AND status = ? AND IFNULL(sell_price, -1) = IFNULL(?, -1) AND IFNULL(sell_date, '') = IFNULL(?, '') "
                "LIMIT 1",
                (ticker, shares, buy_price, buy_date, status, sell_price, sell_date),
            ).fetchone()
            if duplicate:
                result["positions_skipped"] += 1
                continue

            # Spółka z portfela musi być na watchliście, żeby była analizowana.
            conn.execute(
                "INSERT OR IGNORE INTO watchlist (ticker, xtb_symbol, name, source) VALUES (?, ?, ?, 'portfolio')",
                (ticker, f"{ticker} (sprawdź w XTB)", ticker),
            )
            conn.execute(
                "INSERT INTO portfolio (ticker, shares, buy_price, buy_date, notes, status, sell_price, "
                "sell_date, currency, custom_stop, custom_target, buy_fx_rate, sell_fx_rate) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (ticker, shares, buy_price, buy_date, notes, status, sell_price, sell_date,
                 currency, custom_stop, custom_target, buy_fx_rate, sell_fx_rate),
            )
            result["positions_added"] += 1

        for i, d in enumerate(dividends, 1):
            if not isinstance(d, dict):
                warn(f"Dywidendy, wiersz {i}: nieprawidłowy format - pominięto.")
                continue
            ticker = str(d.get("ticker", "")).strip().upper()
            amount_gross = _positive_number(d.get("amount_gross"))
            pay_date = d.get("pay_date")
            if not ticker or amount_gross is None or not _valid_date_str(pay_date):
                warn(f"Dywidendy, wiersz {i} ({ticker or '?'}): niepoprawne dane - pominięto.")
                continue
            currency = str(d.get("currency") or "USD").strip().upper()[:10]
            withholding_tax = _positive_number(d.get("withholding_tax"))
            source = str(d.get("source") or "backup")
            notes = _sanitize_text(d.get("notes", ""))

            duplicate = conn.execute(
                "SELECT 1 FROM dividends WHERE ticker = ? AND pay_date = ? AND currency = ? "
                "AND amount_gross = ? LIMIT 1",
                (ticker, pay_date, currency, amount_gross),
            ).fetchone()
            if duplicate:
                result["dividends_skipped"] += 1
                continue

            conn.execute(
                "INSERT INTO dividends (ticker, currency, pay_date, amount_gross, withholding_tax, "
                "source, notes) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (ticker, currency, pay_date, amount_gross, withholding_tax, source, notes),
            )
            result["dividends_added"] += 1

        conn.commit()
    return result

"""
backfill_gap.py
----------------
Uzupełnia KONKRETNĄ dziurę w danych powstałą, gdy dashboard (cykl live) nie
działał przez jakiś czas - inaczej niż backfill_history.py (który cofa się o
lata i buduje gęstą próbkę co --step-days), ten skrypt skanuje podany zakres
dat dzień po dniu i wstawia TYLKO te dni, które jeszcze nie mają zapisu
(sprawdza przed wstawieniem) - bezpieczny do wielokrotnego uruchamiania, nie
mnoży duplikatów.

Uzupełnia dwie rzeczy, niezależnie od siebie (każda może mieć inną dziurę):
1. score_history (panel "Skuteczność narzędzia") - dla spółek z watchlisty,
   tą samą metodologią co backfill_history.py: CAUSAL (tylko dane znane "do
   tamtego dnia"), sentyment/fundamenty/kontekst makro pominięte - patrz
   zastrzeżenia w docstringu backfill_history.py, dotyczą tu identycznie.
2. krzywą kapitału portfela (portfolio_equity_history + _combined) - z
   historycznych cen zamknięcia (yfinance) i historycznych kursów walut dla
   pozycji, które były otwarte danego dnia (licząc też pozycje zamknięte
   PO tym dniu - były wtedy jeszcze w portfelu).

Domyślny zakres dat (bez --start/--end): ostatnie 30 dni do wczoraj - na tyle
szeroko, żeby złapać typowy przestój, bez potrzeby osobnego wykrywania "od
kiedy dokładnie jest dziura" (dni, które już mają dane, są i tak pomijane).

Użycie:
    python -m src.backfill_gap --config config.yaml
    python -m src.backfill_gap --config config.yaml --start 2026-10-20 --end 2026-10-28
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import yaml
from rich.console import Console

sys.path.append(str(Path(__file__).resolve().parent.parent))

from src import db
from src.market_data import fetch_history
from src.technical_analysis import analyze_multi_timeframe
from src.llm_sentiment import SentimentResult
from src.report import combine_scores
from src.fx_rates import get_historical_fx_rate

console = Console()


def _trading_days(history: pd.DataFrame, start: str, end: str) -> list[str]:
    """Rzeczywiste sesje giełdowe (nie kalendarzowe dni robocze - te nie
    wiedzą o świętach giełdowych) z historii cen, w podanym zakresie."""
    dates = history.index.strftime("%Y-%m-%d")
    return sorted(d for d in dates if start <= d <= end)


def backfill_score_history_gap(companies: list[dict], start: str, end: str, cfg: dict) -> int:
    ma_long = cfg["technical"].get("ma_long", 200)
    min_len = max(ma_long, 252) + 5
    inserted = 0

    for c in companies:
        ticker = c["ticker"]
        try:
            history = fetch_history(ticker, period="2y", interval="1d")
        except Exception as exc:  # noqa: BLE001
            console.print(f"[yellow]Pomijam {ticker} (skuteczność): nie udało się pobrać danych ({exc})[/yellow]")
            continue

        session_dates = _trading_days(history, start, end)
        if not session_dates:
            continue
        existing = db.get_score_history_dates(ticker, start, end)
        missing = [d for d in session_dates if d not in existing]
        if not missing:
            continue

        filled = 0
        for day in missing:
            truncated = history[history.index.strftime("%Y-%m-%d") <= day]
            if len(truncated) < min_len:
                continue  # za mało historii do wiarygodnej SMA200/52-tyg. na ten dzień

            fifty_two_week_high = float(truncated["High"].tail(252).max())
            fifty_two_week_low = float(truncated["Low"].tail(252).min())
            technical = analyze_multi_timeframe(
                ticker=ticker, daily_history=truncated,
                fifty_two_week_high=fifty_two_week_high, fifty_two_week_low=fifty_two_week_low,
                cfg=cfg["technical"],
            )
            neutral_sentiment = SentimentResult(
                ticker=ticker, score=0.0,
                summary="Backfill luki: sentyment newsów pominięty (brak danych dzień-po-dniu).",
                method="none",
            )
            combined = combine_scores(
                technical, neutral_sentiment, cfg["scoring"],
                trend=None, fundamentals=None, threshold_adjustment=0.0,
            )
            market_price = float(truncated["Close"].iloc[-1])
            db.record_score_snapshot_at(
                ticker=ticker, ts=f"{day} 12:00:00", score=combined["final_score"],
                category=combined["category"], signal=technical.signal,
                source="backfill_gap", market_price=market_price,
            )
            filled += 1

        if filled:
            console.print(f"  {ticker}: uzupełniono {filled} dni.")
        inserted += filled

    return inserted


def backfill_equity_gap(start: str, end: str, cfg: dict) -> int:
    base_currency = cfg.get("portfolio", {}).get("base_currency", "PLN")
    positions = db.get_portfolio(status="all")
    if not positions:
        return 0

    # Cache historii cen per ticker - wiele dni/pozycji tej samej spółki
    # nie powinno powtarzać tego samego pobrania.
    history_cache: dict[str, pd.DataFrame | None] = {}

    def price_on(ticker: str, day: str) -> float | None:
        if ticker not in history_cache:
            try:
                history_cache[ticker] = fetch_history(ticker, period="2y", interval="1d")
            except Exception:  # noqa: BLE001
                history_cache[ticker] = None
        hist = history_cache[ticker]
        if hist is None or len(hist) == 0:
            return None
        truncated = hist[hist.index.strftime("%Y-%m-%d") <= day]
        return float(truncated["Close"].iloc[-1]) if len(truncated) else None

    # Zakres sesji z historii PIERWSZEGO tickera z cenami - krzywa kapitału
    # rysowana jest per dzień kalendarzowy cyklu, nie per sesja jednej
    # konkretnej spółki, więc bierzemy zwykłe dni robocze (bdate_range).
    days = [d.strftime("%Y-%m-%d") for d in pd.bdate_range(start, end)]

    currencies = {p.get("currency") or "USD" for p in positions}
    existing_by_currency = {cur: db.get_portfolio_equity_dates(cur, start, end) for cur in currencies}
    existing_combined = db.get_portfolio_equity_combined_dates(base_currency, start, end)

    inserted = 0
    for day in days:
        held = [
            p for p in positions
            if p["buy_date"] <= day and (p["status"] == "open" or (p.get("sell_date") and p["sell_date"] > day))
        ]
        if not held:
            continue

        by_currency: dict[str, dict] = {}
        combined_value = 0.0
        combined_cost = 0.0
        any_combined_contribution = False

        for p in held:
            price = price_on(p["ticker"], day)
            if price is None:
                continue  # notowanie niedostępne na ten dzień (np. świeżo zadebiutowana spółka) - pomiń TĘ pozycję

            currency = p.get("currency") or "USD"
            market_value = p["shares"] * price
            cost_basis = p["shares"] * p["buy_price"]
            bucket = by_currency.setdefault(currency, {"value": 0.0, "cost": 0.0})
            bucket["value"] += market_value
            bucket["cost"] += cost_basis

            rate = 1.0 if currency == base_currency else get_historical_fx_rate(currency, base_currency, day)
            if rate is None:
                continue  # ta pozycja nie wchodzi do widoku ŁĄCZNEGO na ten dzień, ale wciąż liczy się per-walutowo wyżej
            cost_rate = p.get("buy_fx_rate") or rate
            combined_value += market_value * rate
            combined_cost += cost_basis * cost_rate
            any_combined_contribution = True

        ts = f"{day} 12:00:00"
        for currency, totals in by_currency.items():
            if day in existing_by_currency.get(currency, set()):
                continue
            db.record_portfolio_equity_snapshot_at(currency, totals["value"], totals["cost"], ts)
            inserted += 1

        # Tak jak w live cyklu (web_app.py) - zapisujemy widok łączny nawet
        # przy częściowych danych, żeby jedna niedostępna para walutowa nie
        # wstrzymywała całej krzywej.
        if any_combined_contribution and day not in existing_combined:
            db.record_portfolio_equity_combined_snapshot_at(base_currency, combined_value, combined_cost, ts)
            inserted += 1

    return inserted


def main():
    parser = argparse.ArgumentParser(
        description="Uzupełnia dziurę w danych (skuteczność narzędzia + krzywa kapitału) po przestoju dashboardu."
    )
    parser.add_argument("--config", default="config.yaml", help="Ścieżka do pliku konfiguracyjnego")
    parser.add_argument("--start", default=None, help="YYYY-MM-DD (domyślnie: 30 dni wstecz od wczoraj)")
    parser.add_argument("--end", default=None, help="YYYY-MM-DD (domyślnie: wczoraj)")
    args = parser.parse_args()

    with open(args.config, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    yesterday = (datetime.now().date() - timedelta(days=1))
    end = args.end or yesterday.strftime("%Y-%m-%d")
    start = args.start or (yesterday - timedelta(days=30)).strftime("%Y-%m-%d")

    companies = cfg.get("watchlist", [])

    console.rule(f"[bold cyan]Uzupełnianie dziury w danych: {start} .. {end}[/bold cyan]")

    console.print("[bold]Panel \"Skuteczność narzędzia\"[/bold]")
    if companies:
        n_score = backfill_score_history_gap(companies, start, end, cfg)
        console.print(f"  -> łącznie wstawiono {n_score} wpisów (source='backfill_gap').")
    else:
        console.print("  [dim]Pusta watchlista - pomijam.[/dim]")

    console.print("[bold]Krzywa kapitału portfela[/bold]")
    n_equity = backfill_equity_gap(start, end, cfg)
    console.print(f"  -> łącznie wstawiono {n_equity} punktów.")

    console.rule("[bold green]Zakończono[/bold green]")
    console.print(
        "\n[bold yellow]Pamiętaj:[/bold yellow] tak jak backfill_history.py, uzupełnienie skuteczności "
        "pomija sentyment newsów, fundamenty i kontekst makro z tamtych dni - to przybliżenie oparte "
        "głównie na analizie technicznej. Krzywa kapitału używa kursów RYNKOWYCH z yfinance (nie "
        "oficjalnego średniego kursu NBP), tak samo jak bieżące przeliczenia na dashboardzie."
    )


if __name__ == "__main__":
    main()

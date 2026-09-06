"""
backfill_history.py
----------------------
Zasila panel skuteczności REALNYMI, historycznymi danymi cenowymi, bez
czekania tygodniami na naturalne zbieranie score_history z cykli na żywo.

Dla każdej spółki z watchlisty "cofa się" po historii cen i w regularnych
odstępach (domyślnie co --step-days sesji giełdowych) odtwarza DOKŁADNIE tę
samą logikę analizy technicznej, której dashboard używa na żywo
(technical_analysis.analyze_multi_timeframe) - wyłącznie na danych
dostępnych "do tamtego dnia", bez zaglądania w przyszłość.

WAŻNE OGRANICZENIA METODOLOGICZNE (przeczytaj przed użyciem wyników):
- Sentyment newsów jest tu POMIJANY (ustawiany jako neutralny) - historyczne
  nagłówki dzień-po-dniu dla całego okresu wymagałyby dziesiątek tysięcy
  wywołań lokalnego LLM. Wynik łączny w backfillu bazuje więc głównie na
  analizie technicznej, nie na pełnym pipeline'ie z dashboardu na żywo.
- Fundamenty są POMIJANE - yfinance nie udostępnia historycznych wskaźników
  fundamentalnych (np. P/E sprzed roku), tylko bieżące.
- Kontekst makro (VIX) jest POMIJANY - próg sugestii nie jest tu podnoszony
  w historycznie niespokojnych okresach.
To PRZYBLIŻENIE, nie identyczna rekonstrukcja tego, co dashboard pokazałby
"na żywo" w danym dniu - ale wystarczające, by szybko sprawdzić, czy sama
logika techniczna+scoring historycznie się broni, i zasilić panel
skuteczności realnymi cenami zamiast czekać tygodniami.

Użycie:
    python -m src.backfill_history --config config.yaml
    python -m src.backfill_history --config config.yaml --years 2 --step-days 5
    python -m src.backfill_history --tickers MSFT,AAPL --years 3 --clear-first
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml
from rich.console import Console

sys.path.append(str(Path(__file__).resolve().parent.parent))

from src import db
from src.market_data import fetch_history
from src.technical_analysis import analyze_multi_timeframe
from src.llm_sentiment import SentimentResult
from src.report import combine_scores

console = Console()


def backfill_ticker(ticker: str, name: str, years: int, step_days: int, cfg: dict) -> int:
    """Zwraca liczbę wstawionych 'zdjęć' historycznych dla danej spółki."""
    try:
        # +1 rok bufora, żeby pierwsze snapshoty miały już pełną historię
        # potrzebną do SMA200 i rollingowego maksimum 52-tygodniowego.
        history = fetch_history(ticker, period=f"{years + 1}y", interval="1d")
    except Exception as exc:  # noqa: BLE001
        console.print(f"[yellow]Pomijam {ticker}: nie udało się pobrać danych ({exc})[/yellow]")
        return 0

    ma_long = cfg["technical"].get("ma_long", 200)
    start_idx = max(ma_long, 252) + 5  # +bufor bezpieczeństwa
    if len(history) <= start_idx:
        console.print(f"[yellow]Pomijam {ticker}: za mało historii do wiarygodnego backfillu.[/yellow]")
        return 0

    inserted = 0
    for idx in range(start_idx, len(history), step_days):
        truncated = history.iloc[: idx + 1]  # CAUSAL - tylko dane "do tego dnia"

        fifty_two_week_high = float(truncated["High"].tail(252).max())
        fifty_two_week_low = float(truncated["Low"].tail(252).min())

        technical = analyze_multi_timeframe(
            ticker=ticker, daily_history=truncated,
            fifty_two_week_high=fifty_two_week_high, fifty_two_week_low=fifty_two_week_low,
            cfg=cfg["technical"],
        )

        # Sentyment celowo neutralny - patrz zastrzeżenia w docstringu modułu.
        neutral_sentiment = SentimentResult(
            ticker=ticker, score=0.0,
            summary="Backfill historyczny: sentyment newsów pominięty (brak danych dzień-po-dniu).",
            method="none",
        )
        combined = combine_scores(
            technical, neutral_sentiment, cfg["scoring"],
            trend=None, fundamentals=None, threshold_adjustment=0.0,
        )

        snapshot_date = truncated.index[-1]
        ts_str = snapshot_date.strftime("%Y-%m-%d") + " 12:00:00"
        market_price = float(truncated["Close"].iloc[-1])

        db.record_score_snapshot_at(
            ticker=ticker, ts=ts_str, score=combined["final_score"],
            category=combined["category"], signal=technical.signal,
            source="backfill", market_price=market_price,
        )
        inserted += 1

    return inserted


def main():
    parser = argparse.ArgumentParser(
        description="Zasila panel skuteczności historycznymi danymi cenowymi (patrz ograniczenia w docstringu pliku)"
    )
    parser.add_argument("--config", default="config.yaml", help="Ścieżka do pliku konfiguracyjnego")
    parser.add_argument("--tickers", default=None,
                         help="Lista tickerów oddzielonych przecinkami (domyślnie: cała watchlist z config.yaml)")
    parser.add_argument("--years", type=int, default=2, help="Ile lat wstecz przeanalizować (domyślnie 2)")
    parser.add_argument("--step-days", type=int, default=5,
                         help="Co ile sesji giełdowych generować 'zdjęcie' werdyktu (domyślnie 5, czyli ~tydzień)")
    parser.add_argument("--clear-first", action="store_true",
                         help="Usuń wcześniejsze wpisy backfillu przed uruchomieniem (unika duplikatów)")
    args = parser.parse_args()

    with open(args.config, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    if args.tickers:
        companies = [{"ticker": t.strip(), "name": t.strip()} for t in args.tickers.split(",") if t.strip()]
    else:
        companies = cfg.get("watchlist", [])

    if not companies:
        console.print("[red]Brak spółek do przetworzenia (pusta watchlista i brak --tickers).[/red]")
        return

    if args.clear_first:
        for c in companies:
            removed = db.clear_backfill_history(c["ticker"])
            if removed:
                console.print(f"[dim]Usunięto {removed} wcześniejszych wpisów backfillu dla {c['ticker']}.[/dim]")

    console.rule(f"[bold cyan]Backfill historyczny - {len(companies)} spółek, {args.years} lat wstecz[/bold cyan]")

    total_inserted = 0
    for c in companies:
        console.print(f"Przetwarzam {c.get('name', c['ticker'])} ({c['ticker']})...")
        count = backfill_ticker(c["ticker"], c.get("name", c["ticker"]), args.years, args.step_days, cfg)
        console.print(f"  -> wstawiono {count} historycznych 'zdjęć' werdyktu.")
        total_inserted += count

    console.rule("[bold green]Zakończono[/bold green]")
    console.print(f"Łącznie wstawiono {total_inserted} wpisów do score_history (source='backfill').")
    console.print(
        "\n[bold yellow]Pamiętaj:[/bold yellow] backfill pomija sentyment newsów, fundamenty i kontekst makro "
        "(patrz nagłówek pliku backfill_history.py) - to przybliżenie oparte głównie na analizie technicznej, "
        "przydatne do szybkiego zasilenia panelu skuteczności, nie identyczna rekonstrukcja pełnej analizy "
        "z dashboardu na żywo. Panel skuteczności w dashboardzie powinien teraz pokazać znacznie więcej danych."
    )


if __name__ == "__main__":
    main()
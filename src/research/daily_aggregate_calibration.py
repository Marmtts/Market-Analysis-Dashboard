"""
daily_aggregate_calibration.py
---------------------------------
ŚCIEŻKA 2 (ostatni test przed odrzuceniem sentymentu newsów jako sygnału):
zamiast korelować POJEDYNCZY nagłówek ze zwrotem ceny (co dało wynik ~0 -
patrz calibration_report.py), agreguje sentyment WSZYSTKICH nagłówków
z tego samego dnia dla tej samej spółki i dopiero taki, uśredniony
"dzienny sentyment" koreluje ze zwrotem.

To bliżej odpowiada temu, co system robi NA ŻYWO (analyze_sentiment ocenia
kilka-kilkanaście nagłówków naraz, nie jeden) - i eliminuje szum pojedynczego,
przypadkowego artykułu.

Nie wymaga zbierania nowych danych - przelicza to, co już jest w
training_examples.

Użycie:
    python -m src.daily_aggregate_calibration
"""

from __future__ import annotations

import sys
from pathlib import Path
from collections import defaultdict

import numpy as np
from rich.console import Console
from rich.table import Table

sys.path.append(str(Path(__file__).resolve().parent.parent))

from src import db

db.init_db()
console = Console()


def aggregate_by_day(examples: list[dict]) -> list[dict]:
    """Grupuje przykłady po (ticker, published_date) i uśrednia sentyment
    oraz zwroty (zwroty forward dla tej samej spółki/daty powinny być
    identyczne albo prawie identyczne - biorą tę samą cenę bazową)."""
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for e in examples:
        groups[(e["ticker"], e["published_date"])].append(e)

    daily = []
    for (ticker, pub_date), rows in groups.items():
        scores = [r["llm_score"] for r in rows if r["llm_score"] is not None]
        if not scores:
            continue
        daily.append({
            "ticker": ticker,
            "published_date": pub_date,
            "headline_count": len(rows),
            "avg_llm_score": sum(scores) / len(scores),
            "return_pct_5d": rows[0].get("return_pct_5d"),
            "return_pct_10d": rows[0].get("return_pct_10d"),
            "return_pct_20d": rows[0].get("return_pct_20d"),
        })
    return daily


def correlation_for_horizon(daily: list[dict], col: str, min_headlines: int = 1) -> dict | None:
    pairs = [(d["avg_llm_score"], d[col]) for d in daily
             if d[col] is not None and d["headline_count"] >= min_headlines]
    if len(pairs) < 10:
        return None
    scores = np.array([p[0] for p in pairs])
    returns = np.array([p[1] for p in pairs])
    correlation = float(np.corrcoef(scores, returns)[0, 1])
    pos = returns[scores > 0.2]
    neg = returns[scores < -0.2]
    return {
        "n": len(pairs),
        "correlation": correlation,
        "avg_positive": float(pos.mean()) if len(pos) else None,
        "avg_negative": float(neg.mean()) if len(neg) else None,
    }


def main():
    examples = db.get_training_examples()
    if len(examples) < 50:
        console.print(f"[yellow]Tylko {len(examples)} przykładów - za mało.[/yellow]")
        return

    daily = aggregate_by_day(examples)
    multi_headline_days = [d for d in daily if d["headline_count"] >= 2]

    console.rule(f"[bold cyan]Kalibracja sentymentu DZIENNEGO ({len(daily)} dni-spółek, "
                 f"{len(multi_headline_days)} z >=2 nagłówkami)[/bold cyan]")

    for horizon, col in [("5 sesji", "return_pct_5d"), ("10 sesji", "return_pct_10d"), ("20 sesji", "return_pct_20d")]:
        for min_h, label in [(1, "wszystkie dni"), (2, "tylko dni z >=2 nagłówkami")]:
            result = correlation_for_horizon(daily, col, min_headlines=min_h)
            if result is None:
                console.print(f"[yellow]Za mało danych: {horizon}, {label}[/yellow]")
                continue
            table = Table(title=f"Horyzont: {horizon} ({label})")
            table.add_column("Metryka")
            table.add_column("Wartość")
            table.add_row("Liczba dni-spółek", str(result["n"]))
            table.add_row("Korelacja sentyment dzienny <-> zwrot", f"{result['correlation']:+.3f}")
            table.add_row("Śr. zwrot gdy sentyment dzienny > +0.2",
                           f"{result['avg_positive']:.2f}%" if result['avg_positive'] is not None else "brak danych")
            table.add_row("Śr. zwrot gdy sentyment dzienny < -0.2",
                           f"{result['avg_negative']:.2f}%" if result['avg_negative'] is not None else "brak danych")
            console.print(table)

    console.print(
        "\n[bold yellow]Interpretacja:[/bold yellow] jeśli korelacja dla 'tylko dni z >=2 nagłówkami' jest "
        "wyraźnie lepsza niż w calibration_report.py (pojedyncze nagłówki), to znaczy, że AGREGACJA newsów "
        "faktycznie usuwa szum i ujawnia realny sygnał - warto przejść do fine-tuningu na tej podstawie. "
        "Jeśli nadal blisko zera - sentyment newsowy (w tej formie) prawdopodobnie nie niesie przydatnej "
        "informacji predykcyjnej dla tego zbioru spółek/okresu, niezależnie od sposobu agregacji."
    )


if __name__ == "__main__":
    main()
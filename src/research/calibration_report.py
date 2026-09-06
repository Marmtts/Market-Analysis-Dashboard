"""
calibration_report.py
------------------------
FAZA 2: zanim zainwestujesz czas w fine-tuning, sprawdź, czy obecny model
LLM w ogóle ma sens - czy jego ocena sentymentu koreluje z tym, co
FAKTYCZNIE stało się z ceną w kolejnych dniach.

Jeśli korelacja jest bliska zeru albo ujemna, fine-tuning tego samego
podejścia (ten sam mały model, ta sama struktura promptu) prawdopodobnie
nic nie poprawi - problem może leżeć gdzie indziej (np. sam sentyment
newsowy słabo przewiduje krótkoterminowy ruch ceny, niezależnie od modelu).

Użycie:
    python -m src.calibration_report
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
from rich.console import Console
from rich.table import Table

sys.path.append(str(Path(__file__).resolve().parent.parent))

from src import db

db.init_db()

console = Console()


def compute_excess_correlation(examples, col: str) -> dict | None:
    """Odejmuje od zwrotu każdego przykładu ŚREDNI zwrot TEJ SPÓŁKI w całym
    zbiorze (dla danego horyzontu) - usuwa efekt ogólnego trendu/beta spółki
    (np. 'MU rosło przez cały okres niezależnie od newsów'), zostawiając
    odchylenie specyficzne dla konkretnego dnia/newsa. To dokładniejszy test
    tego, czy POJEDYNCZY news miał jakikolwiek efekt ponad ogólny trend."""
    by_ticker_returns: dict[str, list[float]] = {}
    for e in examples:
        if e[col] is not None:
            by_ticker_returns.setdefault(e["ticker"], []).append(e[col])
    ticker_mean = {t: sum(r) / len(r) for t, r in by_ticker_returns.items() if len(r) >= 5}

    pairs = []
    for e in examples:
        if e["llm_score"] is None or e[col] is None:
            continue
        mean_for_ticker = ticker_mean.get(e["ticker"])
        if mean_for_ticker is None:
            continue
        excess = e[col] - mean_for_ticker
        pairs.append((e["llm_score"], excess))

    if len(pairs) < 10:
        return None

    scores = np.array([p[0] for p in pairs])
    excess_returns = np.array([p[1] for p in pairs])
    correlation = float(np.corrcoef(scores, excess_returns)[0, 1])
    pos = excess_returns[scores > 0.2]
    neg = excess_returns[scores < -0.2]
    return {
        "n": len(pairs),
        "correlation": correlation,
        "avg_excess_positive": float(pos.mean()) if len(pos) else None,
        "avg_excess_negative": float(neg.mean()) if len(neg) else None,
    }


def main():
    examples = db.get_training_examples()
    if len(examples) < 20:
        console.print(f"[yellow]Tylko {len(examples)} przykładów w bazie - za mało na wiarygodną ocenę. "
                       f"Uruchom najpierw python -m src.collect_training_data z większą liczbą spółek/lat.[/yellow]")
        return

    console.rule(f"[bold cyan]Raport kalibracji sentymentu LLM ({len(examples)} przykładów)[/bold cyan]")

    for horizon, col in [("5 sesji", "return_pct_5d"), ("10 sesji", "return_pct_10d"), ("20 sesji", "return_pct_20d")]:
        pairs = [(e["llm_score"], e[col]) for e in examples if e["llm_score"] is not None and e[col] is not None]
        if len(pairs) < 10:
            console.print(f"[yellow]Za mało danych dla horyzontu {horizon}.[/yellow]")
            continue

        scores = np.array([p[0] for p in pairs])
        returns = np.array([p[1] for p in pairs])
        correlation = float(np.corrcoef(scores, returns)[0, 1])

        positive_sentiment_returns = returns[scores > 0.2]
        negative_sentiment_returns = returns[scores < -0.2]

        table = Table(title=f"Horyzont: {horizon}")
        table.add_column("Metryka")
        table.add_column("Wartość")
        table.add_row("Liczba przykładów", str(len(pairs)))
        table.add_row("Korelacja sentyment <-> zwrot", f"{correlation:+.3f}")
        table.add_row("Śr. zwrot gdy sentyment > +0.2",
                       f"{positive_sentiment_returns.mean():.2f}%" if len(positive_sentiment_returns) else "brak danych")
        table.add_row("Śr. zwrot gdy sentyment < -0.2",
                       f"{negative_sentiment_returns.mean():.2f}%" if len(negative_sentiment_returns) else "brak danych")
        console.print(table)

        excess = compute_excess_correlation(examples, col)
        if excess:
            table2 = Table(title=f"Horyzont: {horizon} (ZWROT NADWYŻKOWY - po odjęciu trendu spółki)")
            table2.add_column("Metryka")
            table2.add_column("Wartość")
            table2.add_row("Liczba przykładów", str(excess["n"]))
            table2.add_row("Korelacja sentyment <-> zwrot nadwyżkowy", f"{excess['correlation']:+.3f}")
            table2.add_row("Śr. zwrot nadwyżkowy gdy sentyment > +0.2",
                            f"{excess['avg_excess_positive']:.2f}%" if excess['avg_excess_positive'] is not None else "brak danych")
            table2.add_row("Śr. zwrot nadwyżkowy gdy sentyment < -0.2",
                            f"{excess['avg_excess_negative']:.2f}%" if excess['avg_excess_negative'] is not None else "brak danych")
            console.print(table2)

    console.print(
        "\n[bold yellow]Jak czytać wynik:[/bold yellow]\n"
        "- Korelacja bliska 0 -> obecny sentyment LLM praktycznie NIE przewiduje ruchu ceny w tym oknie. "
        "Fine-tuning tego samego podejścia prawdopodobnie nie pomoże - warto rozważyć inny sygnał.\n"
        "- Korelacja wyraźnie dodatnia (>0.15-0.2) -> sentyment ma pewną wartość predykcyjną - fine-tuning "
        "większego/lepiej dostrojonego modelu ma sens i może tę korelację wzmocnić.\n"
        "- Jeśli średni zwrot przy dobrym sentymencie jest NIŻSZY niż przy złym - to sygnał, że model jest "
        "źle skalibrowany (np. myli 'ekscytację rynkową' z 'realną szansą wzrostu') - fine-tuning z takimi "
        "przykładami mógłby to naprawić."
    )


if __name__ == "__main__":
    main()
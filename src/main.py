"""
main.py
--------
Interfejs wiersza poleceń (CLI). Uruchom: python -m src.main [--config config.yaml]

Cała logika analizy mieszka teraz w src/analysis_engine.py (reużywana też
przez dashboard webowy, src/web_app.py) - main.py jest już tylko cienką
warstwą CLI: woła silnik, wypisuje wyniki w konsoli (rich) i zapisuje
raport Markdown/JSON.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import yaml
from rich.console import Console
from rich.table import Table

sys.path.append(str(Path(__file__).resolve().parent.parent))

from src.analysis_engine import run_full_analysis
from src.report import save_reports

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("xtb_trend_watch.main")

console = Console()

_LEVEL_STYLE = {"info": "white", "warning": "yellow", "error": "red", "success": "green"}


def load_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _console_progress(message: str, level: str = "info") -> None:
    style = _LEVEL_STYLE.get(level, "white")
    console.print(f"[{style}]{message}[/{style}]")


def print_results_table(results: list[dict], title: str) -> None:
    table = Table(title=title)
    table.add_column("Spółka")
    table.add_column("XTB")
    table.add_column("Sygnał tech.")
    table.add_column("Sentyment")
    table.add_column("Trend 12m")
    table.add_column("Wynik")
    table.add_column("Kategoria")

    for r in results:
        cat_color = {
            "WARTO OBSERWOWAĆ - możliwy dobry punkt wejścia": "green",
            "NEUTRALNIE POZYTYWNIE - brak jednoznacznego sygnału wejścia": "yellow",
            "BRAK SYGNAŁU / POCZEKAJ": "grey58",
            "UNIKAJ - blisko szczytu trendu": "red",
        }.get(r["combined"]["category"], "white")

        trend_label = "-"
        if r.get("sentiment_trend"):
            trend_label = r["sentiment_trend"]["trend_direction"]

        table.add_row(
            r["name"], r["xtb_symbol"], r["technical"]["signal"],
            str(r["sentiment"]["score"]), trend_label, str(r["combined"]["final_score"]),
            f"[{cat_color}]{r['combined']['category']}[/{cat_color}]",
        )

    console.print(table)


def run(cfg: dict) -> None:
    console.rule("[bold cyan]XTB Trend Watch - analiza dzienna[/bold cyan]")

    payload = run_full_analysis(cfg, cfg["watchlist"], on_progress=_console_progress)

    print_results_table(payload["results"], title="Podsumowanie dnia - stała watchlista")
    if payload["discovered_results"]:
        print_results_table(payload["discovered_results"],
                             title="Nowo odkryte spółki (propozycje AI - do weryfikacji!)")

    json_path, md_path = save_reports(
        payload["results"], payload["macro_summary"], cfg,
        discovered_results=payload["discovered_results"],
        macro_context=payload["macro_context"],
    )
    console.print(f"\n[bold green]Zapisano raport:[/bold green]\n- {md_path}\n- {json_path}")
    console.print(
        "\n[bold yellow]Przypomnienie:[/bold yellow] to narzędzie analityczne, nie porada "
        "inwestycyjna. Propozycje nowych spółek są generowane przez LLM i MOGĄ zawierać błędy "
        "lub nieaktualne informacje - zawsze zweryfikuj samodzielnie przed jakąkolwiek decyzją. "
        "Zdecyduj samodzielnie i pamiętaj o zarządzaniu ryzykiem (np. wielkość pozycji, "
        "stop-loss) przed złożeniem zlecenia w XTB."
    )


def main():
    parser = argparse.ArgumentParser(description="XTB Trend Watch - dzienna analiza rynku")
    parser.add_argument("--config", default="config.yaml", help="Ścieżka do pliku konfiguracyjnego")
    args = parser.parse_args()

    cfg = load_config(args.config)
    run(cfg)


if __name__ == "__main__":
    main()

"""
daily_brief.py
----------------
Codzienny, spójny brief napisany przez lokalny LLM - łączy kontekst makro,
najważniejsze sygnały z watchlisty i stan portfela w jeden czytelny akapit,
zamiast zmuszać użytkownika do składania obrazu z osobnych kart dashboardu.

Generowany RAZ na pełny cykl analizy (nie przy każdym odświeżeniu pojedynczej
spółki) - jedno dodatkowe wywołanie LLM, korzysta z tych samych, już policzonych
danych (bez dodatkowych zapytań do yfinance/Finnhub).
"""

from __future__ import annotations

import logging

from .llm_sentiment import _call_ollama
from .report import earnings_days_from_result

logger = logging.getLogger("xtb_trend_watch.daily_brief")

_SORT_ORDER = {
    "WARTO OBSERWOWAĆ - możliwy dobry punkt wejścia": 0,
    "NEUTRALNIE POZYTYWNIE - brak jednoznacznego sygnału wejścia": 1,
    "BRAK SYGNAŁU / POCZEKAJ": 2,
    "UNIKAJ - świeży gwałtowny spadek (sprawdź przyczynę)": 3,
    "UNIKAJ - blisko szczytu trendu": 4,
}


def _build_prompt(payload: dict, portfolio_summary: dict | None) -> str:
    macro = payload.get("macro_context") or {}
    all_results = (payload.get("results") or []) + (payload.get("discovered_results") or [])

    worth_watching = [r for r in all_results if r["combined"]["category"].startswith("WARTO OBSERWOWAĆ")]
    avoid = [r for r in all_results if r["combined"]["category"].startswith("UNIKAJ")]
    sectors = payload.get("sector_concentration") or []

    macro_line = "brak danych makro"
    if macro.get("available"):
        macro_line = f"reżim ryzyka {macro['risk_regime']}, VIX={macro.get('vix_last')}"

    watch_lines = "\n".join(
        f"- {r['name']} ({r['ticker']}): wynik {r['combined']['final_score']}, sygnał {r['technical']['signal']}"
        for r in worth_watching[:6]
    ) or "brak spółek w tej kategorii dzisiaj"

    avoid_lines = "\n".join(
        f"- {r['name']} ({r['ticker']}): {r['combined']['category']}"
        for r in avoid[:6]
    ) or "brak"

    upcoming_earnings = []
    for r in all_results:
        e = earnings_days_from_result(r, max_days=10)
        if e:
            upcoming_earnings.append((e[1], e[0], r))
    upcoming_earnings.sort(key=lambda x: x[0])

    def _when(days: int) -> str:
        return "dzisiaj" if days == 0 else ("jutro" if days == 1 else f"za {days} dni")

    earnings_lines = "\n".join(
        f"- {r['name']} ({r['ticker']}): wyniki kwartalne {date_str} ({_when(days)})"
        for days, date_str, r in upcoming_earnings[:6]
    ) or "brak w ciągu najbliższych 10 dni"

    sector_lines = "\n".join(
        f"- {s['sector']}: {s['count']} spółek ({', '.join(s['tickers'])})"
        for s in sectors[:4]
    ) or "brak istotnej koncentracji"

    portfolio_block = "Użytkownik nie ma jeszcze żadnych pozycji w portfelu."
    if portfolio_summary and portfolio_summary.get("by_currency"):
        lines = []
        for currency, s in portfolio_summary["by_currency"].items():
            lines.append(
                f"- {currency}: wartość {s['value']:.2f}, koszt {s['cost']:.2f}, "
                f"P/L {s['pl_pct']:+.1f}%, ryzyko przy stop-lossach {s.get('risk_pct', 'n/d')}%"
            )
        portfolio_block = "\n".join(lines)

    return f"""Jesteś asystentem analitycznym przygotowującym KRÓTKI, codzienny brief dla inwestora
korzystającego z narzędzia XTB Trend Watch. Masz następujące dane z dzisiejszego cyklu analizy:

KONTEKST MAKRO: {macro_line}

SPÓŁKI "WARTO OBSERWOWAĆ" dzisiaj:
{watch_lines}

SPÓŁKI DO UNIKANIA dzisiaj:
{avoid_lines}

NADCHODZĄCE WYNIKI KWARTALNE (do 10 dni):
{earnings_lines}

KONCENTRACJA SEKTOROWA (watchlista):
{sector_lines}

PORTFEL UŻYTKOWNIKA:
{portfolio_block}

Napisz zwięzły (120-180 słów), rzeczowy brief po polsku, jednym spójnym tekstem (nie listą punktów),
podsumowujący najważniejsze rzeczy z powyższych danych. Jeśli w najbliższych dniach są wyniki kwartalne
spółek, wspomnij o tym jako o czynniku podwyższonej zmienności. Zachowaj profesjonalny, neutralny ton -
bez nadmiernego entuzjazmu ani straszenia. Zakończ jednym zdaniem przypominającym, że to analiza
narzędziowa, nie porada inwestycyjna. Zwróć WYŁĄCZNIE poprawny obiekt JSON (bez markdown):

{{"brief": "<treść briefu>"}}
"""


def generate_daily_brief(payload: dict, portfolio_summary: dict | None, llm_cfg: dict) -> str | None:
    """Zwraca tekst briefu, albo None jeśli LLM wyłączony/niedostępny -
    brak briefu nie powinien nigdy zablokować reszty cyklu analizy."""
    if not llm_cfg.get("enabled", False):
        return None

    prompt = _build_prompt(payload, portfolio_summary)
    parsed = _call_ollama(
        prompt=prompt, base_url=llm_cfg["base_url"], model=llm_cfg["model"],
        timeout=llm_cfg.get("request_timeout_seconds", 60),
    )
    if parsed and "brief" in parsed:
        return str(parsed["brief"]).strip()

    logger.warning("Nie udało się wygenerować codziennego briefu - LLM nie zwrócił poprawnego JSON.")
    return None
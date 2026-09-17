"""
chatbot.py
------------
Interaktywny asystent czatu dashboardu, oparty o lokalny model LLM (Ollama).
Odpowiada na pytania użytkownika WYŁĄCZNIE na podstawie danych już obecnych
w dashboardzie (wyniki analizy, portfel, kontekst makro, brief dnia) - to nie
jest ogólny chatbot ani doradca inwestycyjny, tylko interfejs konwersacyjny
do tego, co narzędzie już policzyło.
"""

from __future__ import annotations

import logging

import requests

logger = logging.getLogger("xtb_trend_watch.chatbot")


def _call_ollama_chat(messages: list[dict], base_url: str, model: str, timeout: int, num_ctx: int = 8192) -> str | None:
    try:
        resp = requests.post(
            f"{base_url}/api/chat",
            json={
                "model": model, "messages": messages, "stream": False,
                "options": {"num_ctx": num_ctx},  # domyślne 2048 tokenów w Ollama jest ZA MAŁE
                                                    # na kontekst z całą watchlistą i portfelem naraz
            },
            timeout=timeout,
        )
        resp.raise_for_status()
        content = resp.json().get("message", {}).get("content", "").strip()
        return content or None
    except Exception as exc:  # noqa: BLE001
        logger.warning("Wywołanie czatu Ollama nie powiodło się: %s", exc)
        return None


def _build_context_block(payload: dict, portfolio_summary: dict | None, extra_context: dict | None) -> str:
    macro = payload.get("macro_context") or {}
    all_results = (payload.get("results") or []) + (payload.get("discovered_results") or [])

    macro_line = "brak danych makro"
    if macro.get("available"):
        macro_line = f"reżim ryzyka {macro['risk_regime']}, VIX={macro.get('vix_last')}"
        for note in (macro.get("notes") or [])[:2]:
            macro_line += f" | {note}"

    lines = [f"KONTEKST MAKRO: {macro_line}", "", "PEŁNA ANALIZA SPÓŁEK (dzisiejszy cykl, posortowane wg wyniku):"]

    for r in sorted(all_results, key=lambda x: -x["combined"]["final_score"]):
        fund = r.get("fundamentals")
        target = fund.get("metrics", {}).get("analyst_target_mean") if fund and fund.get("available") else None
        flags = "; ".join((fund.get("flags") or [])[:2]) if fund and fund.get("available") else "brak danych"
        reasons = "; ".join(r["technical"]["reasons"][:3])
        sentiment = (r["sentiment"]["summary"] or "")[:180]
        discovery = f" [PROPOZYCJA AI: {r.get('discovery_reason', '')[:120]}]" if r.get("discovery_reason") else ""

        lines.append(
            f"- {r['name']} ({r['ticker']}): {r['combined']['category']}, wynik={r['combined']['final_score']}, "
            f"sygnał={r['technical']['signal']}{f', cena docelowa={target}' if target else ''}{discovery}\n"
            f"  Powody techniczne: {reasons}\n"
            f"  Sentyment: {sentiment}\n"
            f"  Fundamenty: {flags}"
        )

    if payload.get("sector_concentration"):
        lines.append("")
        lines.append("KONCENTRACJA SEKTOROWA WATCHLISTY (spółki WARTO OBSERWOWAĆ z tego samego sektora):")
        for s in payload["sector_concentration"]:
            lines.append(f"- {s['sector']}: {s['count']} spółek ({', '.join(s['tickers'])})")

    if payload.get("daily_brief"):
        lines.append("")
        lines.append(f"BRIEF DNIA (wygenerowany wcześniej): {payload['daily_brief']}")

    lines.append("")
    if portfolio_summary and portfolio_summary.get("by_currency"):
        lines.append("PORTFEL UŻYTKOWNIKA (podsumowanie per waluta):")
        for currency, s in portfolio_summary["by_currency"].items():
            lines.append(
                f"- {currency}: wartość {s['value']:.2f}, koszt {s['cost']:.2f}, "
                f"P/L {s['pl_pct']:+.1f}%, ryzyko przy stop-lossach {s.get('risk_pct', 'n/d')}%"
            )
    else:
        lines.append("PORTFEL UŻYTKOWNIKA: brak otwartych pozycji.")

    if extra_context:
        exposure = extra_context.get("sector_exposure")
        if exposure:
            lines.append("")
            lines.append("EKSPOZYCJA SEKTOROWA PORTFELA (% realnego kapitału, nie watchlisty):")
            for s in exposure[:8]:
                lines.append(f"- {s['sector']}: {s['pct_of_portfolio']}% ({', '.join(s['tickers'])})")

        tax = extra_context.get("tax_summary")
        if tax and tax.get("by_year"):
            lines.append("")
            lines.append(f"ORIENTACYJNE PODSUMOWANIE PODATKOWE ({tax['base_currency']}, wg roku sprzedaży):")
            for year, y in sorted(tax["by_year"].items(), reverse=True):
                lines.append(
                    f"- {year}: zyski {y['total_gains']:.2f}, straty {y['total_losses']:.2f}, "
                    f"netto {y['net_result']:.2f}, szac. podatek 19% = {y['estimated_tax_19pct']:.2f}"
                )

        eff = extra_context.get("effectiveness_stats")
        if eff:
            lines.append("")
            lines.append("HISTORYCZNA SKUTECZNOŚĆ NARZĘDZIA (wg kategorii, min. 14 dni od werdyktu):")
            for cat, s in eff.items():
                lines.append(f"- {cat}: {s['count']} sygnałów, win rate {s['win_rate_pct']}%, śr. zwrot {s['avg_return_pct']}%")

    return "\n".join(lines)


SYSTEM_PROMPT_TEMPLATE = """Jesteś asystentem czatu wbudowanym w dashboard XTB Trend Watch - narzędzie
analityczne do obserwacji spółek giełdowych. Odpowiadasz na pytania użytkownika WYŁĄCZNIE na podstawie
danych podanych poniżej - to jest Twoja jedyna wiedza o bieżącej sytuacji, nie zgaduj i nie wymyślaj
danych, których tu nie ma.

{context}

Zasady:
- Odpowiadaj zwięźle, po polsku, rzeczowym tonem.
- Jeśli użytkownik pyta o coś, czego nie ma w powyższych danych (spółkę spoza tej listy, wydarzenie
  sprzed/po dzisiejszym cyklu), powiedz to wprost - nie zmyślaj.
- NIGDY nie udzielaj porady inwestycyjnej w stylu "kup" / "sprzedaj" - opisuj, co pokazują dane i jakie
  sygnały wygenerowało narzędzie, decyzję zostawiając użytkownikowi.
- Jeśli pytanie jest niejasne, zadaj krótkie pytanie doprecyzowujące zamiast zgadywać.
"""


def answer_chat_question(conversation: list[dict], payload: dict,
                          portfolio_summary: dict | None, extra_context: dict | None,
                          llm_cfg: dict) -> str:
    if not llm_cfg.get("enabled", False):
        return "Czat wymaga włączonego lokalnego modelu LLM (llm.enabled: true w config.yaml)."

    system_message = {"role": "system", "content": SYSTEM_PROMPT_TEMPLATE.format(
        context=_build_context_block(payload, portfolio_summary, extra_context)
    )}
    # Ostatnie 8 wiadomości (zamiast 12) - kontekst danych jest teraz znacznie
    # większy niż wcześniej, więc historia rozmowy musi być krótsza, żeby
    # zmieścić się w num_ctx bez ucinania najważniejszych, świeżych danych.
    messages = [system_message] + conversation[-8:]

    reply = _call_ollama_chat(
        messages=messages, base_url=llm_cfg["base_url"], model=llm_cfg["model"],
        timeout=llm_cfg.get("request_timeout_seconds", 90),  # dłuższy timeout - większy prompt = wolniejsza odpowiedź
        num_ctx=llm_cfg.get("chat_num_ctx", 8192),
    )
    return reply or "Nie udało się uzyskać odpowiedzi od lokalnego modelu - spróbuj ponownie."
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


def _call_ollama_chat(messages: list[dict], base_url: str, model: str, timeout: int) -> str | None:
    try:
        resp = requests.post(
            f"{base_url}/api/chat",
            json={"model": model, "messages": messages, "stream": False},
            timeout=timeout,
        )
        resp.raise_for_status()
        content = resp.json().get("message", {}).get("content", "").strip()
        return content or None
    except Exception as exc:  # noqa: BLE001
        logger.warning("Wywołanie czatu Ollama nie powiodło się: %s", exc)
        return None


def _build_context_block(payload: dict, portfolio_summary: dict | None) -> str:
    macro = payload.get("macro_context") or {}
    all_results = (payload.get("results") or []) + (payload.get("discovered_results") or [])

    macro_line = "brak danych makro"
    if macro.get("available"):
        macro_line = f"reżim ryzyka {macro['risk_regime']}, VIX={macro.get('vix_last')}"

    lines = [f"KONTEKST MAKRO: {macro_line}", "", "WYNIKI ANALIZY SPÓŁEK (dzisiejszy cykl):"]
    for r in sorted(all_results, key=lambda x: -x["combined"]["final_score"])[:20]:
        fund = r.get("fundamentals")
        target = fund.get("metrics", {}).get("analyst_target_mean") if fund and fund.get("available") else None
        lines.append(
            f"- {r['name']} ({r['ticker']}): kategoria={r['combined']['category']}, "
            f"wynik={r['combined']['final_score']}, sygnał={r['technical']['signal']}"
            + (f", cena docelowa={target}" if target else "")
        )

    if payload.get("daily_brief"):
        lines.append("")
        lines.append(f"BRIEF DNIA: {payload['daily_brief']}")

    lines.append("")
    if portfolio_summary and portfolio_summary.get("by_currency"):
        lines.append("PORTFEL UŻYTKOWNIKA:")
        for currency, s in portfolio_summary["by_currency"].items():
            lines.append(
                f"- {currency}: wartość {s['value']:.2f}, koszt {s['cost']:.2f}, "
                f"P/L {s['pl_pct']:+.1f}%, ryzyko przy stop-lossach {s.get('risk_pct', 'n/d')}%"
            )
    else:
        lines.append("PORTFEL UŻYTKOWNIKA: brak otwartych pozycji.")

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
                          portfolio_summary: dict | None, llm_cfg: dict) -> str:
    if not llm_cfg.get("enabled", False):
        return "Czat wymaga włączonego lokalnego modelu LLM (llm.enabled: true w config.yaml)."

    system_message = {"role": "system", "content": SYSTEM_PROMPT_TEMPLATE.format(
        context=_build_context_block(payload, portfolio_summary)
    )}
    # Ostatnie 12 wiadomości wystarcza na sensowny kontekst rozmowy bez
    # nadmiernego wydłużania promptu (a więc i czasu odpowiedzi).
    messages = [system_message] + conversation[-12:]

    reply = _call_ollama_chat(
        messages=messages, base_url=llm_cfg["base_url"], model=llm_cfg["model"],
        timeout=llm_cfg.get("request_timeout_seconds", 60),
    )
    return reply or "Nie udało się uzyskać odpowiedzi od lokalnego modelu - spróbuj ponownie."
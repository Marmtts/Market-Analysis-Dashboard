"""
discovery.py
--------------
LLM (lokalny, Ollama) na podstawie bieżących newsów makroekonomicznych
(oraz opcjonalnie tematów, które akurat wracają w nagłówkach) proponuje
spółki, które mogą być warte dodania do obserwacji - poza stałą watchlistą
z config.yaml.

WAŻNE zastrzeżenie: LLM może "zhalucynować" nieistniejący lub nieaktualny
ticker. Dlatego KAŻDY kandydat jest tu traktowany jako niezweryfikowana
propozycja - main.py próbuje pobrać dla niego realne dane rynkowe (yfinance)
i dopiero jeśli się to uda, kandydat trafia do dalszej analizy technicznej
i newsowej (dokładnie tej samej, co spółki ze stałej watchlisty).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .news_sources import Headline
from .llm_sentiment import _call_ollama  # reużywamy tej samej funkcji wywołania Ollama

logger = logging.getLogger("xtb_trend_watch.discovery")


@dataclass
class CandidateCompany:
    ticker: str
    name: str
    reason: str


def _build_discovery_prompt(macro_headlines: list[Headline],
                             existing_tickers: list[str],
                             max_candidates: int) -> str:
    joined = "\n".join(f"- [{h.source}] {h.title}" for h in macro_headlines) or "(brak nagłówków)"
    existing = ", ".join(existing_tickers) if existing_tickers else "(brak)"

    return f"""Jesteś analitykiem rynkowym. Poniżej znajdują się aktualne nagłówki
ogólnorynkowe (makro) z ostatnich dni:

{joined}

Spółki już obserwowane przez użytkownika (NIE proponuj ich ponownie):
{existing}

Zadanie: Na podstawie powyższych nagłówków oraz Twojej ogólnej wiedzy o rynkach,
zaproponuj do {max_candidates} spółek notowanych publicznie na giełdach na całym
świecie, które obecnie wydają się interesujące do obserwacji pod kątem
POTENCJALNEGO zakupu akcji w najbliższych tygodniach/miesiącach - np. dlatego,
że są w centrum jakiegoś trendu/tematu widocznego w newsach, działają w branży,
o której ostatnio dużo się mówi, albo pojawiają się w newsach w pozytywnym
kontekście. Unikaj spółek, które według newsów są akurat na szczycie hype'u/ceny
(to narzędzie z założenia unika kupowania "na górce" - Twoim zadaniem jest
wskazać kandydatów do DALSZEJ analizy, nie gotowe rekomendacje zakupu).

Podaj WYŁĄCZNIE realne, aktualnie istniejące tickery giełdowe (w formacie
rozpoznawanym przez Yahoo Finance, np. "AAPL", "ASML.AS", "7203.T", "ALE.WA").
Jeśli nie masz pewności co do dokładnego tickeru danej spółki, pomiń ją.

Zwróć WYŁĄCZNIE poprawny obiekt JSON (bez dodatkowego tekstu, bez markdown):

{{"candidates": [
  {{"ticker": "<ticker w formacie Yahoo Finance>", "name": "<pełna nazwa spółki>",
    "reason": "<1-2 zdania po polsku, dlaczego ta spółka jest warta obserwacji>"}}
]}}
"""


def discover_candidates(
    macro_headlines: list[Headline],
    existing_tickers: list[str],
    llm_cfg: dict,
    max_candidates: int = 5,
) -> list[CandidateCompany]:
    """Zwraca listę niezweryfikowanych kandydatów zaproponowanych przez LLM.
    Zwraca pustą listę, jeśli LLM jest wyłączony lub wywołanie się nie powiedzie -
    to funkcja czysto "kreatywna", bez sensownego fallbacku słownikowego."""
    if not llm_cfg.get("enabled", False):
        logger.info("Odkrywanie nowych spółek pominięte - LLM wyłączony w konfiguracji.")
        return []

    prompt = _build_discovery_prompt(macro_headlines, existing_tickers, max_candidates)
    parsed = _call_ollama(
        prompt=prompt,
        base_url=llm_cfg["base_url"],
        model=llm_cfg["model"],
        timeout=llm_cfg.get("request_timeout_seconds", 60),
    )

    if not parsed or "candidates" not in parsed:
        logger.warning("LLM nie zwrócił poprawnej listy kandydatów - pomijam odkrywanie.")
        return []

    existing_upper = {t.upper() for t in existing_tickers}
    seen: set[str] = set()
    candidates: list[CandidateCompany] = []

    for item in parsed["candidates"]:
        try:
            ticker = str(item.get("ticker", "")).strip()
            name = str(item.get("name", "")).strip()
            reason = str(item.get("reason", "")).strip()
        except AttributeError:
            continue

        if not ticker or not name:
            continue
        if ticker.upper() in existing_upper or ticker.upper() in seen:
            continue

        seen.add(ticker.upper())
        candidates.append(CandidateCompany(ticker=ticker, name=name, reason=reason))

        if len(candidates) >= max_candidates:
            break

    return candidates

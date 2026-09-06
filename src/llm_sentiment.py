"""
llm_sentiment.py
------------------
Analiza sentymentu nagłówków newsowych dla danej spółki.

Preferowana ścieżka: lokalny LLM przez Ollama (http://localhost:11434).
Jeśli LLM jest niedostępny (nie uruchomiony, błąd sieci) i w configu
`fallback_to_lexicon: true`, używamy prostego analizatora słownikowego
(lista słów finansowo pozytywnych/negatywnych) - mniej dokładny,
ale zawsze działa offline i bez zależności.

Wynik sentymentu: liczba w zakresie -1.0 (bardzo negatywny) do 1.0
(bardzo pozytywny) + krótkie uzasadnienie.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

import requests

from .news_sources import Headline

logger = logging.getLogger("xtb_trend_watch.llm_sentiment")


@dataclass
class SentimentResult:
    ticker: str
    score: float           # -1.0 do 1.0
    summary: str
    method: str             # "llm" | "lexicon" | "none"


_POSITIVE_WORDS = {
    "growth", "beat", "beats", "surge", "record", "upgrade", "upgraded", "strong",
    "profit", "rally", "gain", "gains", "bullish", "outperform", "expansion",
    "wzrost", "rekord", "zysk", "silny", "poprawa", "podwyżka", "optymizm",
}
_NEGATIVE_WORDS = {
    "decline", "miss", "misses", "drop", "plunge", "downgrade", "downgraded",
    "weak", "loss", "losses", "bearish", "underperform", "recession", "lawsuit",
    "investigation", "fraud", "layoffs", "spadek", "strata", "słaby", "obniżka",
    "afera", "pozew", "zwolnienia", "recesja",
}


def _lexicon_sentiment(headlines: list[Headline]) -> SentimentResult:
    pos, neg = 0, 0
    for h in headlines:
        words = h.title.lower().split()
        pos += sum(1 for w in words if any(pw in w for pw in _POSITIVE_WORDS))
        neg += sum(1 for w in words if any(nw in w for nw in _NEGATIVE_WORDS))
    total = pos + neg
    score = 0.0 if total == 0 else (pos - neg) / total
    summary = (f"Analiza słownikowa (fallback, bez LLM): {pos} pozytywnych vs {neg} "
               f"negatywnych sygnałów słownych w {len(headlines)} nagłówkach.")
    return SentimentResult(ticker="", score=round(score, 2), summary=summary, method="lexicon")


def _build_prompt(ticker: str, company_name: str, headlines: list[Headline]) -> str:
    joined = "\n".join(f"- {h.title} (źródło: {h.source})" for h in headlines) or "(brak nagłówków)"
    return f"""Jesteś analitykiem rynkowym. Poniżej znajdują się najnowsze nagłówki wiadomości
dotyczące spółki {company_name} ({ticker}).

Nagłówki:
{joined}

Zadanie: Oceń ogólny sentyment tych wiadomości dla inwestora rozważającego ZAKUP akcji
w perspektywie kilku tygodni/miesięcy. Zwróć WYŁĄCZNIE poprawny obiekt JSON (bez dodatkowego
tekstu, bez markdown) w formacie:

{{"score": <liczba float od -1.0 do 1.0, gdzie -1 = bardzo negatywny, 0 = neutralny, 1 = bardzo pozytywny>,
"summary": "<zwięzłe uzasadnienie po polsku, maks. 3 zdania>",
"key_risks": ["<ryzyko 1>", "<ryzyko 2>"],
"key_positives": ["<pozytyw 1>", "<pozytyw 2>"]}}
"""


def _parse_json_loose(raw_text: str) -> dict | None:
    """Próbuje sparsować JSON zwrócony przez LLM. Modele czasem dorzucają
    dodatkowy tekst, markdown-owe ```json fence'y, albo drobne błędy
    składniowe wokół właściwego obiektu - próbujemy więc kilku strategii,
    zanim się poddamy."""
    text = raw_text.strip()

    # 1) prosta próba - może to już czysty JSON
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # 2) usuń ewentualne markdown code fence'y (```json ... ``` lub ``` ... ```)
    if "```" in text:
        parts = text.split("```")
        for part in parts:
            candidate = part.strip()
            if candidate.startswith("json"):
                candidate = candidate[4:].strip()
            if candidate.startswith("{"):
                try:
                    return json.loads(candidate)
                except json.JSONDecodeError:
                    continue

    # 3) wytnij fragment między pierwszym "{" a ostatnim "}" - najczęstszy
    # przypadek, gdy model dopisał komentarz przed/po właściwym obiekcie
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        candidate = text[start:end + 1]
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass

    return None


def _call_ollama(prompt: str, base_url: str, model: str, timeout: int) -> dict | None:
    try:
        resp = requests.post(
            f"{base_url}/api/generate",
            json={"model": model, "prompt": prompt, "stream": False, "format": "json"},
            timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        raw_text = data.get("response", "").strip()
        parsed = _parse_json_loose(raw_text)
        if parsed is None:
            logger.warning("Nie udało się sparsować odpowiedzi JSON z LLM. Surowa odpowiedź "
                            "(pierwsze 300 znaków): %s", raw_text[:300])
        return parsed
    except Exception as exc:  # noqa: BLE001
        logger.warning("Wywołanie lokalnego LLM (Ollama) nie powiodło się: %s", exc)
        return None


def analyze_sentiment(ticker: str, company_name: str, headlines: list[Headline],
                       llm_cfg: dict) -> SentimentResult:
    if not headlines:
        return SentimentResult(ticker=ticker, score=0.0,
                                summary="Brak dostępnych nagłówków do analizy.", method="none")

    if llm_cfg.get("enabled", False):
        prompt = _build_prompt(ticker, company_name, headlines)
        parsed = _call_ollama(
            prompt=prompt,
            base_url=llm_cfg["base_url"],
            model=llm_cfg["model"],
            timeout=llm_cfg.get("request_timeout_seconds", 60),
        )
        if parsed and "score" in parsed:
            try:
                score = float(parsed["score"])
                score = max(-1.0, min(1.0, score))
                summary = str(parsed.get("summary", "")).strip()
                risks = parsed.get("key_risks", [])
                positives = parsed.get("key_positives", [])
                if risks:
                    summary += " Ryzyka: " + "; ".join(risks) + "."
                if positives:
                    summary += " Pozytywy: " + "; ".join(positives) + "."
                return SentimentResult(ticker=ticker, score=round(score, 2),
                                        summary=summary, method="llm")
            except (TypeError, ValueError):
                logger.warning("LLM zwrócił niepoprawny format dla %s, przechodzę na fallback.", ticker)

    if llm_cfg.get("fallback_to_lexicon", True):
        result = _lexicon_sentiment(headlines)
        result.ticker = ticker
        return result

    return SentimentResult(ticker=ticker, score=0.0, summary="Sentyment niedostępny (LLM wyłączony).",
                            method="none")


# =====================================================================
# ANALIZA TRENDU SENTYMENTU W CZASIE (na bazie newsów historycznych)
# =====================================================================

@dataclass
class MonthlySentiment:
    month: str          # "YYYY-MM"
    score: float         # -1.0 do 1.0 (zawsze liczone słownikowo - szybkie, offline)
    headline_count: int


@dataclass
class SentimentTrendResult:
    ticker: str
    monthly: list[MonthlySentiment]
    trend_direction: str    # "POPRAWIA_SIE" | "POGARSZA_SIE" | "STABILNY" | "BRAK_DANYCH"
    trend_score_delta: float  # różnica: (średnia z ost. 3 mies.) - (średnia z wcześniejszych)
    llm_narrative: str        # opcjonalne podsumowanie roku wygenerowane przez LLM


def _trend_direction_from_delta(delta: float) -> str:
    if delta >= 0.15:
        return "POPRAWIA_SIE"
    if delta <= -0.15:
        return "POGARSZA_SIE"
    return "STABILNY"


def _build_year_summary_prompt(ticker: str, company_name: str,
                                monthly: list[MonthlySentiment],
                                sample_headlines: dict[str, list[Headline]]) -> str:
    lines = []
    for m in monthly:
        sample = sample_headlines.get(m.month, [])[:3]
        sample_txt = "; ".join(h.title for h in sample) if sample else "(brak przykładowych nagłówków)"
        lines.append(f"{m.month}: wynik słownikowy={m.score:+.2f}, "
                      f"liczba nagłówków={m.headline_count}, przykłady: {sample_txt}")
    joined = "\n".join(lines)
    return f"""Jesteś analitykiem rynkowym. Poniżej masz miesięczne podsumowanie newsów o spółce
{company_name} ({ticker}) z ostatnich ~12 miesięcy, wraz z przykładowymi nagłówkami z każdego
miesiąca i prostym wynikiem słownikowym sentymentu (-1 do 1) dla danego miesiąca.

{joined}

Zadanie: Opisz, jak zmieniał się narracyjny kontekst wokół tej spółki na przestrzeni roku
(np. czy sentyment się poprawiał, pogarszał, czy był stabilny; jakie były kluczowe wydarzenia
lub powtarzające się tematy). Zwróć WYŁĄCZNIE poprawny obiekt JSON (bez dodatkowego tekstu):

{{"narrative": "<zwięzłe podsumowanie po polsku, maks. 4-5 zdań, opisujące ewolucję sentymentu i kluczowe tematy>"}}
"""


def analyze_historical_trend(
    ticker: str,
    company_name: str,
    monthly_headlines: dict[str, list[Headline]],
    llm_cfg: dict,
) -> SentimentTrendResult:
    """Liczy sentyment słownikowy dla każdego miesiąca (zawsze, offline) i - jeśli LLM
    włączony - dorzuca jedno zwięzłe podsumowanie narracyjne całego okresu."""
    if not monthly_headlines:
        return SentimentTrendResult(
            ticker=ticker, monthly=[], trend_direction="BRAK_DANYCH",
            trend_score_delta=0.0, llm_narrative="Brak danych historycznych (sprawdź klucz Finnhub API)."
        )

    monthly_results: list[MonthlySentiment] = []
    for month_key in sorted(monthly_headlines.keys()):
        headlines = monthly_headlines[month_key]
        lex = _lexicon_sentiment(headlines) if headlines else SentimentResult(
            ticker=ticker, score=0.0, summary="", method="lexicon")
        monthly_results.append(MonthlySentiment(month=month_key, score=lex.score,
                                                 headline_count=len(headlines)))

    # trend: porównanie średniej z ostatnich 3 miesięcy vs. reszty okresu
    if len(monthly_results) >= 4:
        recent = monthly_results[-3:]
        older = monthly_results[:-3]
        recent_avg = sum(m.score for m in recent) / len(recent)
        older_avg = sum(m.score for m in older) / len(older) if older else recent_avg
        delta = round(recent_avg - older_avg, 2)
    else:
        delta = 0.0

    direction = _trend_direction_from_delta(delta)

    narrative = ""
    if llm_cfg.get("enabled", False):
        prompt = _build_year_summary_prompt(ticker, company_name, monthly_results, monthly_headlines)
        parsed = _call_ollama(
            prompt=prompt,
            base_url=llm_cfg["base_url"],
            model=llm_cfg["model"],
            timeout=llm_cfg.get("request_timeout_seconds", 60),
        )
        if parsed and "narrative" in parsed:
            narrative = str(parsed["narrative"]).strip()

    if not narrative:
        narrative = (f"Podsumowanie automatyczne (bez LLM): sentyment słownikowy w ostatnich "
                     f"3 miesiącach ({recent_avg:+.2f} śr.) względem wcześniejszego okresu "
                     f"({older_avg:+.2f} śr.) - trend: {direction}."
                     if len(monthly_results) >= 4 else "Zbyt mało danych miesięcznych na ocenę trendu.")

    return SentimentTrendResult(
        ticker=ticker, monthly=monthly_results, trend_direction=direction,
        trend_score_delta=delta, llm_narrative=narrative,
    )

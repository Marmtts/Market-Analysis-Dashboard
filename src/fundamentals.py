"""
fundamentals.py
------------------
Podstawowa analiza fundamentalna spółki na bazie yfinance `Ticker.info`
(P/E, P/B, wzrost przychodów, marże, zadłużenie).

Uwaga: `Ticker.info` bywa niekompletne lub chwilowo niedostępne dla
niektórych spółek (szczególnie spoza USA) - stąd wszystkie odczyty są
"defensywne" (brakująca wartość = None, nie wyjątek).

To NIE jest pełna wycena spółki - to zestaw prostych, ogólnodostępnych
wskaźników z krótką, regułową interpretacją, traktowaną jako DODATKOWY
kontekst obok analizy technicznej i newsowej. Waga w łącznym wyniku jest
celowo niewielka (patrz report.py / scoring.fundamentals_weight).
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from datetime import date, datetime

import yfinance as yf

logger = logging.getLogger("xtb_trend_watch.fundamentals")


@dataclass
class FundamentalResult:
    ticker: str
    available: bool
    metrics: dict = field(default_factory=dict)
    flags: list[str] = field(default_factory=list)   # np. "wysoka wycena bez wzrostu"
    score: float = 0.5   # 0-1, 0.5 = neutralnie / brak danych


def _safe_get(info: dict, *keys, default=None):
    """Pobiera pierwszą dostępną wartość spośród `keys`. Kluczowe: `yfinance`
    czasem zwraca `float('nan')` zamiast `None` dla brakujących pól (np.
    debtToEquity dla spółki, która go nie raportuje) - traktujemy NaN
    dokładnie tak samo jak brak wartości, żeby nie przeciekał dalej do
    obliczeń i serializacji JSON."""
    for k in keys:
        v = info.get(k)
        if v is None:
            continue
        if isinstance(v, float) and math.isnan(v):
            continue
        return v
    return default


# Cache terminów wyników w pamięci procesu - termin publikacji zmienia się
# rzadko (raz na kwartał), a ręczne odświeżanie pojedynczej spółki nie powinno
# za każdym razem dokładać kolejnego zapytania do Yahoo Finance.
_EARNINGS_CACHE: dict[str, tuple[str | None, float]] = {}
_EARNINGS_CACHE_TTL_SECONDS = 12 * 3600


def pick_next_earnings_date(candidates, today: date | None = None) -> date | None:
    """Z listy kandydatów (date/datetime/pd.Timestamp - Yahoo zwraca różne
    typy, a często kilka dat: przedział szacunkowy) wybiera NAJBLIŻSZĄ datę
    nie wcześniejszą niż dziś. Daty z przeszłości (ostatnie, już opublikowane
    wyniki) są ignorowane."""
    today = today or date.today()
    normalized: list[date] = []
    for c in candidates:
        if isinstance(c, datetime):  # datetime jest podklasą date - sprawdzamy pierwszy
            normalized.append(c.date())
        elif isinstance(c, date):
            normalized.append(c)
    future = sorted(d for d in normalized if d >= today)
    return future[0] if future else None


def fetch_next_earnings_date(ticker_obj, ticker: str, info: dict) -> str | None:
    """Zwraca datę najbliższej publikacji wyników kwartalnych ("YYYY-MM-DD")
    albo None. Źródła: `Ticker.calendar` (dokładniejsze), a w razie braku
    pola `earningsTimestamp*` z `info` (już pobranego - zero dodatkowych
    zapytań). Uwaga: Yahoo bywa niedokładne - to często data SZACUNKOWA."""
    cached = _EARNINGS_CACHE.get(ticker)
    if cached and (time.time() - cached[1]) < _EARNINGS_CACHE_TTL_SECONDS:
        return cached[0]

    candidates: list = []
    calendar_ok = False
    try:
        calendar = ticker_obj.calendar
        calendar_ok = True
        if isinstance(calendar, dict):
            raw = calendar.get("Earnings Date")
            if raw is not None:
                candidates.extend(raw if isinstance(raw, (list, tuple)) else [raw])
    except Exception as exc:  # noqa: BLE001
        logger.debug("Brak kalendarza wyników dla %s: %s", ticker, exc)

    for key in ("earningsTimestamp", "earningsTimestampStart"):
        ts = info.get(key)
        if isinstance(ts, (int, float)) and not isinstance(ts, bool) and ts > 0 and not math.isnan(ts):
            try:
                candidates.append(datetime.fromtimestamp(ts))
            except (OverflowError, OSError, ValueError):
                pass

    picked = pick_next_earnings_date(candidates)
    result = picked.isoformat() if picked else None
    # Nie cache'ujemy "brak daty", jeśli zapytanie o kalendarz się nie udało
    # (błąd przejściowy) - przy następnym odświeżeniu spróbujemy ponownie.
    if result is not None or calendar_ok:
        _EARNINGS_CACHE[ticker] = (result, time.time())
    return result


def fetch_fundamentals(ticker: str, cfg: dict, current_price: float | None = None) -> FundamentalResult:
    """Pobiera i ocenia podstawowe wskaźniki fundamentalne dla spółki."""
    try:
        ticker_obj = yf.Ticker(ticker)
        info = ticker_obj.get_info()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Nie udało się pobrać danych fundamentalnych dla %s: %s", ticker, exc)
        return FundamentalResult(ticker=ticker, available=False)

    if not info:
        return FundamentalResult(ticker=ticker, available=False)

    trailing_pe = _safe_get(info, "trailingPE")
    forward_pe = _safe_get(info, "forwardPE")
    price_to_book = _safe_get(info, "priceToBook")
    revenue_growth = _safe_get(info, "revenueGrowth")          # ułamek, np. 0.15 = 15%
    earnings_growth = _safe_get(info, "earningsGrowth")
    profit_margins = _safe_get(info, "profitMargins")
    debt_to_equity = _safe_get(info, "debtToEquity")
    return_on_equity = _safe_get(info, "returnOnEquity")
    dividend_yield = _safe_get(info, "dividendYield")
    market_cap = _safe_get(info, "marketCap")
    sector = info.get("sector")  # string, nie liczba - brak potrzeby _safe_get/NaN-check
    quote_type = info.get("quoteType")  # "EQUITY" | "ETF" | "MUTUALFUND" | "INDEX" itd.

    # Termin najbliższych wyników kwartalnych - tylko dla akcji (ETF-y i fundusze
    # nie publikują wyników w tym sensie).
    next_earnings_date = None
    if quote_type in (None, "EQUITY"):
        next_earnings_date = fetch_next_earnings_date(ticker_obj, ticker, info)

    # --- Konsensus analityków Wall Street (cena docelowa, rekomendacja) ---
    # To NIE jest nasza własna analiza - to zagregowana opinia analityków
    # śledzona przez Yahoo Finance. Traktujemy to jako dodatkowy, zewnętrzny
    # punkt odniesienia, nie jako wyrocznię - analitycy też się mylą i bywają
    # opóźnieni względem newsów.
    target_mean = _safe_get(info, "targetMeanPrice")
    target_high = _safe_get(info, "targetHighPrice")
    target_low = _safe_get(info, "targetLowPrice")
    num_analysts = _safe_get(info, "numberOfAnalystOpinions")
    recommendation_key = _safe_get(info, "recommendationKey")  # np. "buy", "hold", "sell"

    analyst_upside_pct = None
    if target_mean is not None and current_price:
        analyst_upside_pct = round((target_mean - current_price) / current_price * 100, 1)

    metrics = {
        "trailing_pe": round(trailing_pe, 2) if trailing_pe is not None else None,
        "forward_pe": round(forward_pe, 2) if forward_pe is not None else None,
        "price_to_book": round(price_to_book, 2) if price_to_book is not None else None,
        "revenue_growth_pct": round(revenue_growth * 100, 1) if revenue_growth is not None else None,
        "earnings_growth_pct": round(earnings_growth * 100, 1) if earnings_growth is not None else None,
        "profit_margins_pct": round(profit_margins * 100, 1) if profit_margins is not None else None,
        "debt_to_equity": round(debt_to_equity, 1) if debt_to_equity is not None else None,
        "return_on_equity_pct": round(return_on_equity * 100, 1) if return_on_equity is not None else None,
        "dividend_yield_pct": round(dividend_yield * 100, 2) if dividend_yield is not None else None,
        "market_cap": market_cap,
        "analyst_target_mean": round(target_mean, 2) if target_mean is not None else None,
        "analyst_target_high": round(target_high, 2) if target_high is not None else None,
        "analyst_target_low": round(target_low, 2) if target_low is not None else None,
        "analyst_num_opinions": int(num_analysts) if num_analysts is not None else None,
        "analyst_recommendation": recommendation_key,
        "analyst_upside_pct": analyst_upside_pct,
        "sector": sector,
        "quote_type": quote_type,
        "next_earnings_date": next_earnings_date,
    }

    flags: list[str] = []
    score = 0.5  # start neutralny

    pe_high_threshold = cfg.get("pe_high_threshold", 40)
    pe_low_threshold = cfg.get("pe_low_threshold", 12)
    revenue_growth_good_threshold = cfg.get("revenue_growth_good_pct", 10)
    debt_to_equity_high_threshold = cfg.get("debt_to_equity_high", 150)

    # --- P/E ---
    if trailing_pe is not None:
        if trailing_pe <= 0:
            flags.append("Ujemny P/E (spółka nierentowna wg zysku netto TTM) - podwyższone ryzyko.")
            score -= 0.10
        elif trailing_pe >= pe_high_threshold:
            if revenue_growth is not None and revenue_growth * 100 < revenue_growth_good_threshold:
                flags.append(
                    f"Wysoka wycena (P/E={trailing_pe:.1f}) przy stosunkowo niskim wzroście "
                    f"przychodów ({revenue_growth*100:.1f}%) - możliwa przewartościowana spółka."
                )
                score -= 0.15
            else:
                flags.append(f"Wysoki P/E ({trailing_pe:.1f}), ale uzasadniony silnym wzrostem "
                              f"przychodów - typowe dla spółek wzrostowych.")
        elif trailing_pe <= pe_low_threshold:
            flags.append(f"Niski P/E ({trailing_pe:.1f}) względem historycznych norm rynkowych - "
                          f"może oznaczać niedowartościowanie LUB fundamentalne problemy, sprawdź kontekst.")
            score += 0.05

    # --- Wzrost przychodów ---
    if revenue_growth is not None:
        if revenue_growth < 0:
            flags.append(f"Malejące przychody rok do roku ({revenue_growth*100:.1f}%).")
            score -= 0.15
        elif revenue_growth * 100 >= revenue_growth_good_threshold:
            flags.append(f"Solidny wzrost przychodów rok do roku ({revenue_growth*100:.1f}%).")
            score += 0.15

    # --- Marże / rentowność ---
    if profit_margins is not None:
        if profit_margins < 0:
            flags.append(f"Ujemna marża netto ({profit_margins*100:.1f}%) - spółka obecnie nierentowna.")
            score -= 0.10
        elif profit_margins > 0.15:
            flags.append(f"Wysoka marża netto ({profit_margins*100:.1f}%) - dobra rentowność operacyjna.")
            score += 0.05

    # --- Zadłużenie ---
    if debt_to_equity is not None and debt_to_equity >= debt_to_equity_high_threshold:
        flags.append(f"Wysoki wskaźnik zadłużenia do kapitału własnego (D/E={debt_to_equity:.0f}%) - "
                      f"większa wrażliwość na wzrost stóp procentowych.")
        score -= 0.10

        # --- Konsensus analityków - informacyjnie, celowo NIE wpływa na score ---
    # (żeby nie liczyć tego samego sentymentu rynkowego dwa razy - już mamy
    # osobny komponent sentymentu newsów). To dodatkowy kontekst do przeczytania,
    # nie kolejny czynnik ważony w wyniku.
    if target_mean is not None and num_analysts:
        if analyst_upside_pct is not None and analyst_upside_pct >= 15:
            flags.append(
                f"Analitycy (śr. z {num_analysts}) widzą cenę docelową {target_mean:.2f} "
                f"({analyst_upside_pct:+.1f}% względem obecnej ceny) - istotny potencjał wzrostu wg Wall Street."
            )
        elif analyst_upside_pct is not None and analyst_upside_pct <= -10:
            flags.append(
                f"Cena docelowa analityków ({target_mean:.2f}, {num_analysts} opinii) jest "
                f"{analyst_upside_pct:.1f}% PONIŻEJ obecnej ceny - możliwe przewartościowanie wg Wall Street."
            )
        else:
            flags.append(
                f"Cena docelowa analityków: {target_mean:.2f} (śr. z {num_analysts} opinii, "
                f"rekomendacja: {recommendation_key or 'brak'})."
            )
            
    # --- Zbliżające się wyniki kwartalne - celowo TYLKO informacyjnie (nie
    # zmienia score): to nie jest sygnał "kupuj/sprzedaj", tylko ostrzeżenie o
    # podwyższonej zmienności (luka cenowa po publikacji, często niezależna od
    # tego, czy liczby są dobre). Decyzję zostawiamy użytkownikowi.
    earnings_warning_days = cfg.get("earnings_warning_days", 7)
    if next_earnings_date:
        days_left = (date.fromisoformat(next_earnings_date) - date.today()).days
        if 0 <= days_left <= earnings_warning_days:
            when = "DZISIAJ" if days_left == 0 else ("jutro" if days_left == 1 else f"za {days_left} dni")
            flags.append(
                f"Wyniki kwartalne {when} ({next_earnings_date}) - podwyższona zmienność: cena może "
                f"gwałtownie się zmienić w obie strony, także przy dobrych liczbach. Zastanów się nad "
                f"wielkością pozycji i stop-lossem przed tą datą (data z Yahoo Finance bywa szacunkowa)."
            )

    score = max(0.0, min(1.0, score))

    if not flags:
        flags.append("Brak istotnych sygnałów fundamentalnych (dane niepełne lub neutralne).")

    return FundamentalResult(ticker=ticker, available=True, metrics=metrics, flags=flags, score=round(score, 3))

"""
fx_rates.py
-------------
Pobieranie kursów wymiany walut (yfinance, pary FX np. "USDPLN=X") z prostym
cache'owaniem w pamięci (TTL 15 min) - żeby nie odpytywać Yahoo Finance przy
każdym cyklu i przy każdym sprawdzeniu alertu cenowego osobno.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta

import requests
import yfinance as yf

NBP_BASE_URL = "https://api.nbp.pl/api/exchangerates/rates/A"

logger = logging.getLogger("xtb_trend_watch.fx_rates")

_CACHE: dict[tuple[str, str], tuple[float, float]] = {}  # (from,to) -> (rate, timestamp)
_TTL_SECONDS = 900  # 15 minut

# Pamięć PORAŻKI dla kursów HISTORYCZNYCH (get_historical_fx_rate) - w
# odróżnieniu od udanych zapytań, które trwale cache'ują się w SQLite
# (fx_rate_cache), niepowodzenie nigdzie się nie zapisywało: para walutowa
# bez notowań na yfinance (np. rzadziej płynne SEK) była odpytywana od nowa
# przy KAŻDYM wywołaniu (np. compute_twr_curve dla krzywej łącznej liczy to
# per przepływ gotówki - bez tej pamięci to dziesiątki zbędnych, powolnych
# zapytań sieciowych przy każdym odświeżeniu widoku). W pamięci procesu
# (nie SQLite) - restart serwera daje parze walutowej kolejną szansę.
_HISTORICAL_FX_FAILURES: dict[str, float] = {}
_HISTORICAL_FX_FAILURE_TTL_SECONDS = 6 * 3600  # 6h - to dane HISTORYCZNE, nie zmienią się w kolejnych minutach


def get_fx_rate(from_currency: str, to_currency: str) -> float | None:
    """Zwraca ile jednostek to_currency odpowiada 1 jednostce from_currency.
    Dwie warstwy cache: w pamięci (najszybsza, znika przy restarcie) i na
    dysku w SQLite (przetrwa restart serwera - oszczędza jedno zapytanie
    sieciowe zaraz po starcie, zamiast czekać na pierwsze świeże pobranie).
    Zwraca None, jeśli nie udało się pobrać kursu - wywołujący musi to
    obsłużyć (pominąć tę walutę, nie zgadywać kursu 1:1)."""
    from . import db  # import lokalny - unika cyklicznego importu na starcie modułu

    from_currency, to_currency = from_currency.upper(), to_currency.upper()
    if from_currency == to_currency:
        return 1.0

    cache_key = (from_currency, to_currency)
    now = time.time()

    cached = _CACHE.get(cache_key)
    if cached and (now - cached[1]) < _TTL_SECONDS:
        return cached[0]

    persisted = db.get_cached_fx_rate(f"{from_currency}_{to_currency}", ttl_seconds=_TTL_SECONDS)
    if persisted is not None:
        _CACHE[cache_key] = (persisted, now)
        return persisted

    rate = _fetch_pair(f"{from_currency}{to_currency}=X")
    if rate is None:
        inverse = _fetch_pair(f"{to_currency}{from_currency}=X")
        rate = (1.0 / inverse) if inverse else None

    if rate is not None:
        _CACHE[cache_key] = (rate, now)
        db.save_fx_rate_to_cache(f"{from_currency}_{to_currency}", rate)
    else:
        logger.warning("Nie udało się pobrać kursu %s -> %s.", from_currency, to_currency)
    return rate


def _fetch_pair(pair_symbol: str) -> float | None:
    try:
        hist = yf.Ticker(pair_symbol).history(period="10d", interval="1d")
        closes = hist["Close"].dropna()
        if closes.empty:
            logger.warning("Brak notowań dla pary walutowej %s (pusty wynik yfinance).", pair_symbol)
            return None
        return float(closes.iloc[-1])
    except Exception as exc:  # noqa: BLE001
        logger.warning("Błąd pobierania pary walutowej %s: %s", pair_symbol, exc)
        return None


def get_historical_fx_rate(from_currency: str, to_currency: str, date_str: str) -> float | None:
    """Zwraca PRZYBLIŻONY kurs z konkretnego dnia ("YYYY-MM-DD") - kurs
    RYNKOWY z yfinance (najbliższa dostępna sesja), NIE oficjalny średni
    kurs NBP wymagany polskimi przepisami podatkowymi. Cache'owany WIECZNIE
    na dysku (ttl_seconds=None) - kurs z przeszłej, zamkniętej daty nigdy
    się nie zmieni, więc po pierwszym pobraniu już nigdy nie pytamy ponownie
    (przydatne szczególnie dla podsumowania podatkowego, liczonego wielokrotnie).
    Używane wyłącznie do orientacyjnego podsumowania podatkowego (patrz
    report.compute_tax_summary) - przed rozliczeniem PIT zweryfikuj dokładne
    kwoty w tabelach NBP."""
    from . import db

    from_currency, to_currency = from_currency.upper(), to_currency.upper()
    if from_currency == to_currency:
        return 1.0

    cache_key = f"{from_currency}_{to_currency}_{date_str}"
    persisted = db.get_cached_fx_rate(cache_key, ttl_seconds=None)
    if persisted is not None:
        return persisted

    failed_at = _HISTORICAL_FX_FAILURES.get(cache_key)
    if failed_at is not None and (time.time() - failed_at) < _HISTORICAL_FX_FAILURE_TTL_SECONDS:
        return None

    rate = _fetch_pair_historical(f"{from_currency}{to_currency}=X", date_str)
    if rate is None:
        inverse = _fetch_pair_historical(f"{to_currency}{from_currency}=X", date_str)
        rate = (1.0 / inverse) if inverse else None

    if rate is not None:
        db.save_fx_rate_to_cache(cache_key, rate)
        _HISTORICAL_FX_FAILURES.pop(cache_key, None)
    else:
        _HISTORICAL_FX_FAILURES[cache_key] = time.time()
    return rate


def _fetch_pair_historical(pair_symbol: str, date_str: str) -> float | None:
    try:
        target = datetime.strptime(date_str, "%Y-%m-%d")
        start = (target - timedelta(days=7)).strftime("%Y-%m-%d")
        end = (target + timedelta(days=1)).strftime("%Y-%m-%d")
        hist = yf.Ticker(pair_symbol).history(start=start, end=end, interval="1d")
        closes = hist["Close"].dropna()
        if closes.empty:
            return None
        valid = closes[closes.index.strftime("%Y-%m-%d") <= date_str]
        return float(valid.iloc[-1]) if not valid.empty else float(closes.iloc[0])
    except Exception:  # noqa: BLE001
        return None

def _fetch_nbp_rate_for_date(currency: str, date_str: str) -> float | None:
    """Pyta NBP o ŚREDNI kurs danej waluty względem PLN z KONKRETNEGO dnia.
    NBP nie publikuje kursów w weekendy/święta (zwraca 404) - wywołujący
    (get_nbp_rate) próbuje wtedy dni wcześniejszych."""
    try:
        resp = requests.get(f"{NBP_BASE_URL}/{currency}/{date_str}/?format=json", timeout=10)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return float(resp.json()["rates"][0]["mid"])
    except Exception:  # noqa: BLE001
        return None


def get_nbp_rate(currency: str, date_str: str | None = None, for_tax_purposes: bool = False) -> float | None:
    """Zwraca OFICJALNY średni kurs NBP waluty obcej względem PLN - to jest
    WŁAŚCIWY, ZGODNY Z PRZEPISAMI kurs do rozliczenia podatku PIT-38
    (w przeciwieństwie do przybliżonego kursu rynkowego z yfinance używanego
    gdzie indziej w narzędziu).

    - date_str=None -> najświeższy dostępny kurs (do bieżących przeliczeń).
    - for_tax_purposes=True -> kurs z OSTATNIEGO DNIA ROBOCZEGO POPRZEDZAJĄCEGO
      date_str, zgodnie z art. 11a ustawy o PIT (kurs z dnia transakcji NIE
      jest tu poprawny prawnie - liczy się dzień wcześniejszy).
    - for_tax_purposes=False -> kurs z date_str (lub najbliższego wcześniejszego
      dnia roboczego, jeśli to weekend/święto).

    Wyniki cache'owane TRWALE w SQLite (kursy z przeszłości się nie zmieniają).
    Zwraca None, jeśli NBP nie publikuje danej waluty lub API jest niedostępne -
    wywołujący powinien wtedy skorzystać z fallbacku (patrz report.compute_tax_summary)."""
    from . import db

    currency = currency.upper()
    if currency == "PLN":
        return 1.0

    if date_str is None:
        try:
            resp = requests.get(f"{NBP_BASE_URL}/{currency}/?format=json", timeout=10)
            resp.raise_for_status()
            return float(resp.json()["rates"][0]["mid"])
        except Exception:  # noqa: BLE001
            return None

    cache_key = f"NBP_{currency}_{date_str}_{'prev' if for_tax_purposes else 'same'}"
    persisted = db.get_cached_fx_rate(cache_key, ttl_seconds=None)
    if persisted is not None:
        return persisted

    target = datetime.strptime(date_str, "%Y-%m-%d")
    if for_tax_purposes:
        target -= timedelta(days=1)  # dzień POPRZEDZAJĄCY transakcję - wymóg ustawowy

    rate = None
    for _ in range(10):  # max 10 dni wstecz - wystarczy na długie weekendy/święta
        rate = _fetch_nbp_rate_for_date(currency, target.strftime("%Y-%m-%d"))
        if rate is not None:
            break
        target -= timedelta(days=1)

    if rate is not None:
        db.save_fx_rate_to_cache(cache_key, rate)
    return rate
"""
macro_context.py
------------------
Pobiera szeroki kontekst makroekonomiczny, niezależny od konkretnej spółki,
i klasyfikuje bieżący "reżim ryzyka" na rynku. Używane jako DODATKOWY filtr
bezpieczeństwa - w dniach podwyższonego ryzyka rynkowego podnosimy poprzeczkę
dla sugestii kupna (zamiast całkowicie blokować - decyzję i tak podejmuje
użytkownik).

Źródła (przez yfinance, publicznie dostępne indeksy/tickery):
- ^VIX  - indeks zmienności S&P 500 ("indeks strachu")
- ^TNX  - rentowność 10-letnich obligacji skarbowych USA (w %, *10 w yfinance)

Uwaga: pełna inflacja (CPI) nie jest łatwo dostępna przez yfinance bez
dodatkowego, płatnego źródła danych (np. FRED) - pomijamy ją tutaj celowo,
zamiast pokazywać nieaktualne/przybliżone dane. VIX i rentowność obligacji
są sensownym, w pełni darmowym substytutem "nastroju makro".
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import yfinance as yf

logger = logging.getLogger("xtb_trend_watch.macro_context")


@dataclass
class MacroContext:
    available: bool
    vix_last: float | None = None
    vix_20d_avg: float | None = None
    treasury_10y_yield_pct: float | None = None
    risk_regime: str = "NIEZNANY"      # "SPOKOJNY" | "PODWYZSZONY" | "WYSOKI" | "NIEZNANY"
    notes: list[str] | None = None


def fetch_macro_context(cfg: dict) -> MacroContext:
    notes: list[str] = []
    vix_last = None
    vix_20d_avg = None
    tnx_last = None

    try:
        vix_hist = yf.Ticker("^VIX").history(period="3mo", interval="1d")
        if not vix_hist.empty:
            vix_last = float(vix_hist["Close"].iloc[-1])
            vix_20d_avg = float(vix_hist["Close"].tail(20).mean())
    except Exception as exc:  # noqa: BLE001
        logger.warning("Nie udało się pobrać danych VIX: %s", exc)

    try:
        tnx_hist = yf.Ticker("^TNX").history(period="5d", interval="1d")
        if not tnx_hist.empty:
            tnx_last = float(tnx_hist["Close"].iloc[-1])  # ^TNX podaje rentowność *10 w yfinance -> to już %
    except Exception as exc:  # noqa: BLE001
        logger.warning("Nie udało się pobrać rentowności obligacji (^TNX): %s", exc)

    if vix_last is None:
        return MacroContext(available=False, notes=["Brak dostępnych danych makro (VIX)."])

    vix_elevated = cfg.get("vix_elevated_threshold", 20)
    vix_high = cfg.get("vix_high_threshold", 30)

    if vix_last >= vix_high:
        risk_regime = "WYSOKI"
        notes.append(f"VIX na poziomie {vix_last:.1f} - podwyższona zmienność/niepewność rynkowa. "
                      f"Historycznie takie okresy bywają zarówno okazjami, jak i zapowiedzią dalszych "
                      f"spadków - zachowaj szczególną ostrożność z wielkością pozycji.")
    elif vix_last >= vix_elevated:
        risk_regime = "PODWYZSZONY"
        notes.append(f"VIX na poziomie {vix_last:.1f} - lekko podwyższone ryzyko rynkowe względem "
                      f"typowego spokojnego otoczenia (<{vix_elevated}).")
    else:
        risk_regime = "SPOKOJNY"
        notes.append(f"VIX na poziomie {vix_last:.1f} - relatywnie spokojne otoczenie rynkowe.")

    if tnx_last is not None:
        notes.append(f"Rentowność 10-letnich obligacji USA: {tnx_last:.2f}%. "
                      f"Wyższe rentowności zwykle oznaczają większą konkurencję dla akcji ze strony "
                      f"obligacji oraz wyższy koszt kapitału dla spółek zadłużonych.")

    return MacroContext(
        available=True,
        vix_last=round(vix_last, 2),
        vix_20d_avg=round(vix_20d_avg, 2) if vix_20d_avg is not None else None,
        treasury_10y_yield_pct=round(tnx_last, 2) if tnx_last is not None else None,
        risk_regime=risk_regime,
        notes=notes,
    )


def risk_regime_score_adjustment(macro: MacroContext, cfg: dict) -> float:
    """Zwraca korektę progu sugestii (dodawaną do scoring.suggestion_threshold) -
    w dniach podwyższonego/wysokiego ryzyka podnosimy poprzeczkę, żeby sugestie
    kupna pojawiały się rzadziej i tylko przy naprawdę mocnych sygnałach."""
    if not macro.available:
        return 0.0
    if macro.risk_regime == "WYSOKI":
        return cfg.get("threshold_increase_on_high_risk", 0.10)
    if macro.risk_regime == "PODWYZSZONY":
        return cfg.get("threshold_increase_on_elevated_risk", 0.05)
    return 0.0

"""
report.py
----------
Łączy wynik analizy technicznej oraz sentymentu newsów w jedną,
finalną ocenę na spółkę i generuje raport dzienny (Markdown + JSON).

Kluczowa zasada bezpieczeństwa (zgodnie z wymogiem użytkownika):
Jeżeli sygnał techniczny to "AT_TOP" (cena blisko szczytu / wykupienie),
spółka NIGDY nie trafia do sekcji rekomendacji zakupu - niezależnie
od tego, jak pozytywne są newsy. Chronimy w ten sposób przed
kupowaniem "na górce".
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from .technical_analysis import TechnicalResult
from .llm_sentiment import SentimentResult, SentimentTrendResult
from .fundamentals import FundamentalResult

# Kategorie, dla których koncentracja sektorowa jest istotna - głównie "kupuj",
# bo kilka jednoczesnych sygnałów kupna w tym samym sektorze to jeden
# skoncentrowany zakład, a nie kilka niezależnych okazji.
_CONCENTRATION_TARGET_CATEGORIES = {"WARTO OBSERWOWAĆ - możliwy dobry punkt wejścia"}


def earnings_days_from_result(result: dict | None, max_days: int | None = None) -> tuple[str, int] | None:
    """Zwraca (data_wyników "YYYY-MM-DD", liczba_dni_do_wyników) dla wyniku
    analizy spółki albo None (brak danych, ETF, termin już minął, albo dalej
    niż max_days). Liczba dni jest liczona OD DZIŚ, a nie od momentu analizy -
    dzięki temu nie starzeje się w cache'u między cyklami."""
    fund = (result or {}).get("fundamentals")
    if not fund or not fund.get("available"):
        return None
    date_str = (fund.get("metrics") or {}).get("next_earnings_date")
    if not date_str:
        return None
    try:
        days = (datetime.strptime(date_str, "%Y-%m-%d").date() - datetime.now().date()).days
    except (ValueError, TypeError):
        return None
    if days < 0 or (max_days is not None and days > max_days):
        return None
    return date_str, days


def compute_sector_concentration(all_results: list[dict], min_count: int = 2) -> list[dict]:
    """Grupuje spółki z kategorii 'WARTO OBSERWOWAĆ' po sektorze (z danych
    fundamentalnych yfinance) i zwraca sektory, w których jest >= min_count
    takich spółek naraz - sygnał, że warto sprawdzić, czy to nie jest
    przypadkiem jeden skoncentrowany zakład sektorowy zamiast kilku
    niezależnych okazji."""
    by_sector: dict[str, list[str]] = {}
    for r in all_results:
        if r["combined"]["category"] not in _CONCENTRATION_TARGET_CATEGORIES:
            continue
        fund = r.get("fundamentals")
        sector = fund.get("metrics", {}).get("sector") if fund and fund.get("available") else None
        if not sector:
            continue
        by_sector.setdefault(sector, []).append(r["ticker"])

    return [
        {"sector": sector, "tickers": tickers, "count": len(tickers)}
        for sector, tickers in sorted(by_sector.items(), key=lambda kv: -len(kv[1]))
        if len(tickers) >= min_count
    ]


def combine_scores(technical: TechnicalResult, sentiment: SentimentResult,
                    scoring_cfg: dict,
                    trend: SentimentTrendResult | None = None,
                    fundamentals: FundamentalResult | None = None,
                    threshold_adjustment: float = 0.0) -> dict:
    tech_w = scoring_cfg["technical_weight"]
    sent_w = scoring_cfg["sentiment_weight"]
    fund_w = scoring_cfg.get("fundamentals_weight", 0.0)

    # Jeśli fundamenty niedostępne, przenosimy ich wagę z powrotem na
    # techniczną+sentyment (proporcjonalnie), żeby suma wag nadal wynosiła 1
    # i żeby brak danych fundamentalnych nie "sztucznie" zaniżał wyniku.
    fundamentals_available = fundamentals is not None and fundamentals.available and fund_w > 0
    if not fundamentals_available and fund_w > 0:
        remaining = tech_w + sent_w
        if remaining > 0:
            tech_w = tech_w / remaining
            sent_w = sent_w / remaining
        fund_w = 0.0

    # normalizacja sentymentu z [-1,1] do [0,1]
    sentiment_norm = (sentiment.score + 1) / 2

    final_score = technical.score * tech_w + sentiment_norm * sent_w
    if fundamentals_available:
        final_score += fundamentals.score * fund_w

    # Drobna korekta na podstawie DŁUGOTERMINOWEGO trendu sentymentu (jeśli dostępny):
    # poprawiający się od miesięcy sentyment -> lekka premia; pogarszający się -> lekka kara.
    # Celowo niewielki wpływ (+/- 0.05), żeby nie zdominować analizy technicznej.
    trend_adjustment = 0.0
    if trend is not None and trend.trend_direction != "BRAK_DANYCH":
        if trend.trend_direction == "POPRAWIA_SIE":
            trend_adjustment = 0.05
        elif trend.trend_direction == "POGARSZA_SIE":
            trend_adjustment = -0.05
    final_score = max(0.0, min(1.0, final_score + trend_adjustment))

    hard_blocked_top = scoring_cfg.get("hard_block_on_top", True) and technical.signal == "AT_TOP"
    hard_blocked_crash = scoring_cfg.get("hard_block_on_sharp_decline", True) and technical.signal == "SHARP_DECLINE"
    hard_blocked = hard_blocked_top or hard_blocked_crash

    # Efektywny próg sugestii - podnoszony w dniach podwyższonego ryzyka makro
    # (patrz macro_context.risk_regime_score_adjustment), żeby w niepewnym
    # otoczeniu rynkowym sugestie kupna pojawiały się rzadziej i tylko przy
    # naprawdę mocnych sygnałach.
    effective_threshold = scoring_cfg["suggestion_threshold"] + threshold_adjustment

    if hard_blocked_top:
        category = "UNIKAJ - blisko szczytu trendu"
    elif hard_blocked_crash:
        category = "UNIKAJ - świeży gwałtowny spadek (sprawdź przyczynę)"
    elif final_score >= effective_threshold and technical.signal == "GOOD_ENTRY":
        category = "WARTO OBSERWOWAĆ - możliwy dobry punkt wejścia"
    elif final_score >= effective_threshold:
        category = "NEUTRALNIE POZYTYWNIE - brak jednoznacznego sygnału wejścia"
    else:
        category = "BRAK SYGNAŁU / POCZEKAJ"

    return {
        "final_score": round(final_score, 3),
        "category": category,
        "hard_blocked": hard_blocked,
        "trend_adjustment": trend_adjustment,
        "effective_threshold": round(effective_threshold, 3),
        "fundamentals_used": fundamentals_available,
    }


def build_company_report(company_cfg: dict, technical: TechnicalResult,
                          sentiment: SentimentResult, combined: dict,
                          trend: SentimentTrendResult | None = None,
                          fundamentals: FundamentalResult | None = None,
                          headlines: list | None = None) -> dict:
    return {
        "ticker": company_cfg["ticker"],
        "xtb_symbol": company_cfg.get("xtb_symbol", company_cfg["ticker"]),
        "name": company_cfg.get("name", company_cfg["ticker"]),
        "technical": asdict(technical),
        "sentiment": asdict(sentiment),
        "sentiment_trend": asdict(trend) if trend is not None else None,
        "fundamentals": asdict(fundamentals) if fundamentals is not None else None,
        # Surowe nagłówki (nie tylko podsumowanie LLM) - do panelu szczegółów
        # na dashboardzie, żeby było widać źródła sentymentu.
        "news_headlines": [asdict(h) for h in (headlines or [])][:8],
        "combined": combined,
        # Znacznik czasu TEJ KONKRETNEJ analizy (nie globalnego cyklu) -
        # różne spółki mogą mieć różny wiek danych, np. po odświeżeniu na
        # żywo pojedynczej karty na dashboardzie (patrz web_app.py:
        # POST /api/refresh/{ticker}).
        "analyzed_at": datetime.now().isoformat(timespec="seconds"),
    }


_SORT_ORDER = {
    "WARTO OBSERWOWAĆ - możliwy dobry punkt wejścia": 0,
    "NEUTRALNIE POZYTYWNIE - brak jednoznacznego sygnału wejścia": 1,
    "BRAK SYGNAŁU / POCZEKAJ": 2,
    "UNIKAJ - świeży gwałtowny spadek (sprawdź przyczynę)": 3,
    "UNIKAJ - blisko szczytu trendu": 4,
}


def _render_company_section(r: dict) -> list[str]:
    """Renderuje blok Markdown dla jednej spółki - używane zarówno dla stałej
    watchlisty, jak i dla kandydatów odkrytych przez LLM."""
    t = r["technical"]
    s = r["sentiment"]
    c = r["combined"]
    lines = [f"### {r['name']} ({r['ticker']} / XTB: {r['xtb_symbol']})"]

    if r.get("discovery_reason"):
        lines.append(f"**Dlaczego zaproponowane przez AI:** {r['discovery_reason']}  ")

    lines.append(f"**Kategoria:** {c['category']}  ")
    lines.append(f"**Wynik łączny:** {c['final_score']} (0-1)  ")
    lines.append(f"**Sygnał techniczny:** {t['signal']} (score={round(t['score'], 2)})  ")
    lines.append(f"**Sentyment newsów (bieżący):** {s['score']} (metoda: {s['method']})  ")

    trend = r.get("sentiment_trend")
    if trend and trend.get("monthly"):
        lines.append(f"**Trend sentymentu (12 mies.):** {trend['trend_direction']} "
                      f"(delta={trend['trend_score_delta']:+.2f}, "
                      f"korekta wyniku: {c['trend_adjustment']:+.2f})  ")

    fund = r.get("fundamentals")
    if fund and fund.get("available"):
        lines.append(f"**Wynik fundamentalny:** {fund['score']} (0-1, uwzględniony w wyniku łącznym: "
                      f"{'tak' if c.get('fundamentals_used') else 'nie'})  ")
    lines.append("")
    lines.append("**Metryki techniczne:**")
    for k, v in t["metrics"].items():
        lines.append(f"- {k}: {v}")
    lines.append("")
    lines.append("**Powody (analiza techniczna):**")
    for reason in t["reasons"]:
        lines.append(f"- {reason}")
    lines.append("")
    lines.append(f"**Podsumowanie newsów (bieżące):** {s['summary']}")
    lines.append("")

    if trend and trend.get("monthly"):
        lines.append("**Sentyment miesięczny (ostatnie ~12 miesięcy, analiza słownikowa):**")
        for m in trend["monthly"]:
            lines.append(f"- {m['month']}: {m['score']:+.2f} ({m['headline_count']} nagłówków)")
        lines.append("")
        lines.append(f"**Podsumowanie roczne (LLM/automatyczne):** {trend['llm_narrative']}")
        lines.append("")

    if fund and fund.get("available"):
        lines.append("**Metryki fundamentalne:**")
        for k, v in fund["metrics"].items():
            if v is not None:
                lines.append(f"- {k}: {v}")
        lines.append("")
        lines.append("**Obserwacje fundamentalne:**")
        for flag in fund["flags"]:
            lines.append(f"- {flag}")
        lines.append("")
    elif fund is not None:
        lines.append("**Metryki fundamentalne:** niedostępne dla tej spółki.")
        lines.append("")

    lines.append("---")
    lines.append("")
    return lines


def save_reports(all_results: list[dict], macro_summary: str, cfg: dict,
                  discovered_results: list[dict] | None = None,
                  macro_context: dict | None = None) -> tuple[Path, Path]:
    discovered_results = discovered_results or []
    out_dir = Path(cfg["output"]["reports_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    date_str = datetime.now().strftime("%Y-%m-%d_%H%M")

    json_path = out_dir / f"report_{date_str}.json"
    md_path = out_dir / f"report_{date_str}.md"

    if cfg["output"].get("save_json", True):
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump({"generated_at": date_str, "macro_summary": macro_summary,
                       "macro_context": macro_context,
                       "results": all_results,
                       "discovered_results": discovered_results},
                      f, ensure_ascii=False, indent=2)

    if cfg["output"].get("save_markdown", True):
        md_lines = [
            f"# Raport XTB Trend Watch - {date_str}",
            "",
            "> **Zastrzeżenie:** To narzędzie analityczne, nie porada inwestycyjna. "
            "Wszystkie decyzje inwestycyjne i ryzyko z nimi związane leżą po stronie użytkownika.",
            "",
        ]

        if macro_context and macro_context.get("available"):
            md_lines.append(f"## Kontekst makro - reżim ryzyka: {macro_context['risk_regime']}")
            if macro_context.get("vix_last") is not None:
                md_lines.append(f"- VIX: {macro_context['vix_last']} "
                                 f"(śr. 20-dniowa: {macro_context.get('vix_20d_avg')})")
            if macro_context.get("treasury_10y_yield_pct") is not None:
                md_lines.append(f"- Rentowność obligacji 10Y (USA): {macro_context['treasury_10y_yield_pct']}%")
            for note in macro_context.get("notes") or []:
                md_lines.append(f"- {note}")
            md_lines.append("")

        md_lines.append("## Nastrój rynku (makro - newsy)")
        md_lines.append(macro_summary or "(brak danych makro)")
        md_lines.append("")
        md_lines.append("## Wyniki per spółka (stała watchlista)")
        md_lines.append("")

        all_results_sorted = sorted(
            all_results, key=lambda r: _SORT_ORDER.get(r["combined"]["category"], 99)
        )
        for r in all_results_sorted:
            md_lines.extend(_render_company_section(r))

        if discovered_results:
            md_lines.append("## Nowo odkryte spółki (propozycje AI)")
            md_lines.append("")
            md_lines.append("> **Uwaga:** poniższe spółki zostały zaproponowane automatycznie przez "
                             "lokalny LLM na podstawie bieżących newsów makro. Tickery zostały "
                             "zweryfikowane pod kątem istnienia (udało się pobrać dane rynkowe), "
                             "ale **uzasadnienie i trafność propozycji mogą być niedoskonałe** - "
                             "zawsze zweryfikuj samodzielnie przed podjęciem decyzji.")
            md_lines.append("")
            discovered_sorted = sorted(
                discovered_results, key=lambda r: _SORT_ORDER.get(r["combined"]["category"], 99)
            )
            for r in discovered_sorted:
                md_lines.extend(_render_company_section(r))

        with open(md_path, "w", encoding="utf-8") as f:
            f.write("\n".join(md_lines))

    return json_path, md_path

def evaluate_portfolio_position(position: dict, result: dict | None,
                                 earnings_warning_days: int = 7) -> dict:
    """Ocenia POSIADANĄ pozycję w świetle najnowszego wyniku analizy tej
    spółki. To NIE jest porada inwestycyjna ani podatkowa - to zestaw
    prostych, przejrzystych reguł łączących: cenę zakupu vs bieżącą,
    sugerowany stop-loss (ATR) i aktualny sygnał/kategorię narzędzia."""
    base = {
        "current_price": None, "unrealized_pct": None, "unrealized_value": None,
        "market_value": None, "cost_basis": None, "holding_days": None,
        "horizon": "brak danych", "category": None, "signal": None,
        "action": "BRAK DANYCH", "reasons": ["Spółka nie jest jeszcze przeanalizowana - poczekaj na cykl."],
        "currency": None, "next_earnings_date": None,
    }
    if result is None:
        return base

    currency = result["technical"]["metrics"].get("currency", "USD")
    current_price = result["technical"]["metrics"].get("last_price")
    if current_price is None:
        return base

    shares = position["shares"]
    buy_price = position["buy_price"]
    cost_basis = shares * buy_price
    market_value = shares * current_price
    unrealized_pct = (current_price - buy_price) / buy_price * 100 if buy_price else None

    holding_days = None
    horizon = "brak danych"
    try:
        buy_date = datetime.strptime(position["buy_date"], "%Y-%m-%d").date()
        holding_days = (datetime.now().date() - buy_date).days
        if holding_days < 30:
            horizon = "krótkoterminowa (<1 mies.)"
        elif holding_days < 365:
            horizon = "średnioterminowa (<1 rok)"
        else:
            horizon = "długoterminowa (>1 rok)"
    except (ValueError, TypeError):
        pass

    category = result["combined"]["category"]
    signal = result["technical"]["signal"]
    # Własny stop-loss użytkownika (jeśli ustawiony) ma pierwszeństwo przed
    # sugestią z ATR - to on trafia też do panelu ryzyka portfela.
    custom_stop = position.get("custom_stop")
    custom_target = position.get("custom_target")
    atr_stop = result["technical"]["metrics"].get("suggested_stop_loss")
    stop_loss = custom_stop if custom_stop else atr_stop
    stop_source = "own" if custom_stop else "atr"
    stop_label = "własnego" if custom_stop else "sugerowanego"

    action = "TRZYMAJ"
    reasons: list[str] = []

    if stop_loss is not None and current_price < stop_loss:
        action = "ROZWAŻ SPRZEDAŻ"
        reasons.append(f"Cena ({current_price:.2f}) spadła poniżej {stop_label} stop-loss ({stop_loss:.2f}).")

    if custom_target and current_price >= custom_target and action == "TRZYMAJ":
        action = "ROZWAŻ REALIZACJĘ ZYSKU"
        reasons.append(f"Cena ({current_price:.2f}) osiągnęła Twój własny cel ({custom_target:.2f}).")

    if signal == "AT_TOP":
        if unrealized_pct is not None and unrealized_pct > 0:
            action = "ROZWAŻ REALIZACJĘ ZYSKU"
            reasons.append("Narzędzie wskazuje 'blisko szczytu trendu' - część zysku można rozważyć zabezpieczyć.")
        else:
            reasons.append("Narzędzie wskazuje 'blisko szczytu trendu' mimo pozycji na stracie - zachowaj ostrożność z dokupowaniem.")
    elif signal == "SHARP_DECLINE":
        action = "SPRAWDŹ PRZYCZYNĘ - NIE DOKUPUJ AUTOMATYCZNIE"
        reasons.append("Świeży, gwałtowny spadek ceny - sprawdź newsy przed jakąkolwiek decyzją.")
    elif signal == "GOOD_ENTRY" and category.startswith("WARTO"):
        reasons.append("Trend wzrostowy nadal aktywny wg narzędzia - brak sygnału do sprzedaży.")

    if not reasons:
        reasons.append(f"Brak jednoznacznego sygnału zmiany - bieżąca kategoria: {category}.")

    # Zbliżające się wyniki kwartalne - tylko dodatkowa uwaga, nie zmienia
    # rekomendacji (patrz komentarz w fundamentals.py).
    earnings = earnings_days_from_result(result)
    if earnings and earnings[1] <= earnings_warning_days:
        when = "DZISIAJ" if earnings[1] == 0 else ("jutro" if earnings[1] == 1 else f"za {earnings[1]} dni")
        reasons.append(
            f"Wyniki kwartalne {when} ({earnings[0]}) - możliwy gwałtowny ruch ceny w obie strony; "
            f"sprawdź, czy wielkość pozycji i stop-loss są dla Ciebie odpowiednie przed publikacją."
        )

    return {
        "current_price": round(current_price, 2),
        "unrealized_pct": round(unrealized_pct, 2) if unrealized_pct is not None else None,
        "unrealized_value": round(market_value - cost_basis, 2),
        "market_value": round(market_value, 2),
        "cost_basis": round(cost_basis, 2),
        "holding_days": holding_days,
        "horizon": horizon,
        "category": category,
        "signal": signal,
        "action": action,
        "reasons": reasons,
        "currency": currency,
        "analyzed_at": result.get("analyzed_at"),
        "suggested_stop_loss": round(stop_loss, 2) if stop_loss is not None else None,
        "stop_source": stop_source if stop_loss is not None else None,
        "next_earnings_date": earnings[0] if earnings else None,
    }


def compute_max_drawdown(equity_points: list[dict]) -> dict:
    """Liczy maksymalne historyczne obsunięcie kapitału (drawdown) z krzywej
    wartości portfela w czasie - kluczowa miara ryzyka: o ile portfel
    potrafił spaść od swojego dotychczasowego szczytu."""
    if len(equity_points) < 2:
        return {"available": False}

    values = [p["total_value"] for p in equity_points]
    peak = values[0]
    peak_ts = equity_points[0]["ts"]
    max_dd_pct = 0.0
    max_dd_peak_ts = peak_ts
    max_dd_trough_ts = peak_ts

    for point in equity_points:
        v = point["total_value"]
        if v > peak:
            peak = v
            peak_ts = point["ts"]
        dd_pct = (peak - v) / peak * 100 if peak > 0 else 0.0
        if dd_pct > max_dd_pct:
            max_dd_pct = dd_pct
            max_dd_peak_ts = peak_ts
            max_dd_trough_ts = point["ts"]

    current_value = values[-1]
    current_peak = max(values)
    current_dd_pct = (current_peak - current_value) / current_peak * 100 if current_peak > 0 else 0.0

    return {
        "available": True,
        "max_drawdown_pct": round(max_dd_pct, 2),
        "max_drawdown_peak_date": max_dd_peak_ts,
        "max_drawdown_trough_date": max_dd_trough_ts,
        "current_drawdown_pct": round(current_dd_pct, 2),
    }


def compute_portfolio_risk_summary(open_positions_evaluated: list[dict]) -> dict:
    """Sumuje, ile REALNIE stracisz (per waluta), jeśli KAŻDA otwarta pozycja
    spadnie dokładnie do swojego sugerowanego stop-lossu - 'najgorszy
    rozsądny scenariusz' przy obecnym ustawieniu stopów, nie prognoza."""
    by_currency: dict[str, dict] = {}
    skipped = 0

    for p in open_positions_evaluated:
        currency = p.get("currency") or "USD"
        current_price = p.get("current_price")
        stop_loss = p.get("suggested_stop_loss")
        shares = p.get("shares")
        market_value = p.get("market_value")

        bucket = by_currency.setdefault(currency, {"portfolio_value": 0.0, "risk_amount": 0.0})
        if market_value:
            bucket["portfolio_value"] += market_value

        if current_price is None or stop_loss is None or shares is None:
            skipped += 1
            continue

        risk_per_share = max(0.0, current_price - stop_loss)
        bucket["risk_amount"] += risk_per_share * shares

    result = {}
    for currency, b in by_currency.items():
        risk_pct = (b["risk_amount"] / b["portfolio_value"] * 100) if b["portfolio_value"] > 0 else None
        result[currency] = {
            "portfolio_value": round(b["portfolio_value"], 2),
            "risk_amount": round(b["risk_amount"], 2),
            "risk_pct": round(risk_pct, 2) if risk_pct is not None else None,
        }
    return {"by_currency": result, "positions_without_stop_loss": skipped}


def compute_portfolio_sector_exposure(open_positions_evaluated: list[dict],
                                       fundamentals_by_ticker: dict) -> list[dict]:
    """Jak compute_sector_concentration, ale dla FAKTYCZNIE POSIADANYCH
    pozycji, ważone WARTOŚCIĄ RYNKOWĄ - pokazuje % kapitału realnie
    siedzącego w danym sektorze, nie tylko liczbę obserwowanych spółek."""
    by_sector: dict[str, dict] = {}
    total_value = 0.0

    for p in open_positions_evaluated:
        value = p.get("market_value") or 0.0
        total_value += value
        fund = fundamentals_by_ticker.get(p["ticker"])
        sector = fund.get("metrics", {}).get("sector") if fund and fund.get("available") else None
        sector = sector or "Nieznany sektor"

        bucket = by_sector.setdefault(sector, {"value": 0.0, "tickers": set()})
        bucket["value"] += value
        bucket["tickers"].add(p["ticker"])

    if total_value <= 0:
        return []

    exposure = [
        {"sector": sector, "value": round(b["value"], 2),
         "pct_of_portfolio": round(b["value"] / total_value * 100, 1),
         "tickers": sorted(b["tickers"])}
        for sector, b in by_sector.items()
    ]
    exposure.sort(key=lambda x: -x["pct_of_portfolio"])
    return exposure

def compute_tax_summary(closed_positions: list[dict], base_currency: str = "PLN") -> dict:
    """Grupuje ZREALIZOWANE transakcje wg roku sprzedaży i liczy orientacyjny
    wynik podatkowy (podatek od zysków kapitałowych, 19% w Polsce - tzw.
    'podatek Belki'). Dla base_currency='PLN' używa OFICJALNEGO średniego
    kursu NBP z dnia poprzedzającego transakcję (zgodnie z art. 11a ustawy
    o PIT) - to JEST poprawny prawnie kurs. Jeśli NBP jest niedostępny dla
    danej waluty/daty, spada na przybliżony kurs rynkowy (yfinance) i
    WYRAŹNIE to odnotowuje w conversion_notes oraz w polu 'nbp_compliant'."""
    from .fx_rates import get_historical_fx_rate, get_nbp_rate

    by_year: dict[str, dict] = {}
    conversion_notes: list[str] = []
    any_fallback_used = False

    for p in closed_positions:
        sell_date = p.get("sell_date")
        if not sell_date:
            continue
        sell_year = sell_date[:4]
        currency = p.get("currency") or "USD"

        if currency == base_currency:
            buy_rate = sell_rate = 1.0
        elif base_currency == "PLN":
            buy_rate = get_nbp_rate(currency, p["buy_date"], for_tax_purposes=True)
            sell_rate = get_nbp_rate(currency, sell_date, for_tax_purposes=True)
            if buy_rate is None or sell_rate is None:
                any_fallback_used = True
                buy_rate = buy_rate or get_historical_fx_rate(currency, base_currency, p["buy_date"])
                sell_rate = sell_rate or get_historical_fx_rate(currency, base_currency, sell_date)
                conversion_notes.append(
                    f"{p['ticker']}: kurs NBP niedostępny dla {currency} - użyto przybliżonego kursu rynkowego."
                )
        else:
            any_fallback_used = True
            buy_rate = get_historical_fx_rate(currency, base_currency, p["buy_date"])
            sell_rate = get_historical_fx_rate(currency, base_currency, sell_date)

        if buy_rate is None or sell_rate is None:
            conversion_notes.append(
                f"{p['ticker']} ({p['buy_date']} -> {sell_date}): brak kursu {currency}->{base_currency}, pominięto."
            )
            continue

        cost = p["buy_price"] * p["shares"] * buy_rate
        proceeds = p["sell_price"] * p["shares"] * sell_rate
        gain = proceeds - cost

        bucket = by_year.setdefault(sell_year, {"gains": 0.0, "losses": 0.0, "count": 0, "trades": []})
        bucket["count"] += 1
        if gain >= 0:
            bucket["gains"] += gain
        else:
            bucket["losses"] += -gain
        bucket["trades"].append({"ticker": p["ticker"], "sell_date": sell_date,
                                  "gain": round(gain, 2), "currency": currency})

    result = {}
    for year, b in sorted(by_year.items()):
        net = b["gains"] - b["losses"]
        result[year] = {
            "trade_count": b["count"],
            "total_gains": round(b["gains"], 2),
            "total_losses": round(b["losses"], 2),
            "net_result": round(net, 2),
            "estimated_tax_19pct": round(max(0.0, net) * 0.19, 2),
            "trades": b["trades"],
        }

    return {
        "base_currency": base_currency,
        "by_year": result,
        "conversion_notes": conversion_notes,
        "nbp_compliant": base_currency == "PLN" and not any_fallback_used,
    }

def summarize_portfolio_by_currency(open_positions_evaluated: list[dict]) -> dict:
    """Grupuje otwarte pozycje po walucie (wartość/koszt/P&L%/ryzyko) -
    reużywane przez daily_brief i chatbota, żeby nie duplikować tej samej
    logiki agregującej w dwóch miejscach."""
    by_currency: dict[str, dict] = {}
    for p in open_positions_evaluated:
        currency = p.get("currency") or "USD"
        bucket = by_currency.setdefault(currency, {"value": 0.0, "cost": 0.0})
        bucket["value"] += p.get("market_value") or 0.0
        bucket["cost"] += p.get("cost_basis") or 0.0

    risk = compute_portfolio_risk_summary(open_positions_evaluated)
    result = {}
    for currency, totals in by_currency.items():
        pl_pct = ((totals["value"] - totals["cost"]) / totals["cost"] * 100) if totals["cost"] > 0 else 0.0
        risk_bucket = risk.get("by_currency", {}).get(currency, {})
        result[currency] = {"value": totals["value"], "cost": totals["cost"],
                             "pl_pct": pl_pct, "risk_pct": risk_bucket.get("risk_pct")}
    return {"by_currency": result}


def _series_to_daily_naive(series: "pd.Series") -> "pd.Series":
    """Sprowadza serię cen do indeksu 'sama data, bez strefy czasowej'.
    Konieczne przy łączeniu giełd z różnych stref (np. NYSE i GPW) - bez tego
    znaczniki czasu z różnych stref nie pokrywają się i wspólna część
    historii wychodzi pusta."""
    s = series.dropna()
    idx = pd.DatetimeIndex(s.index)
    if idx.tz is not None:
        idx = idx.tz_localize(None)
    s = pd.Series(s.values, index=idx.normalize())
    return s[~s.index.duplicated(keep="last")].sort_index()


def compute_portfolio_statistics(closes_by_ticker: dict, weights: dict,
                                  risk_free_rate_pct: float = 0.0,
                                  min_observations: int = 60) -> dict:
    """Statystyki ryzyka portfela liczone z HISTORII cen (zwykle 1 rok) przy
    OBECNYCH wagach pozycji: zmienność, Sharpe, maks. obsunięcie, korelacje
    między pozycjami i współczynnik dywersyfikacji.

    Ograniczenia (celowo jawne): to hipotetyczny portfel o stałych wagach z
    dzisiejszego stanu posiadania, a nie Twoja faktyczna historia; zwroty
    liczone w walutach notowania, więc bez efektu zmian kursów walut.
    Współczynnik dywersyfikacji = (średnia ważona zmienności pozycji) /
    (zmienność portfela): 1.0 = brak korzyści z dywersyfikacji (pozycje
    poruszają się jak jedna), im wyżej, tym lepiej."""
    series = {}
    for ticker, s in closes_by_ticker.items():
        if s is None or len(s) == 0:
            continue
        series[ticker] = _series_to_daily_naive(pd.Series(s))
    if not series:
        return {"available": False, "reason": "Brak danych cenowych dla pozycji w portfelu."}

    prices = pd.concat(series, axis=1, join="inner").dropna()
    returns = prices.pct_change().dropna()
    if len(returns) < min_observations:
        return {"available": False,
                "reason": f"Za mało wspólnej historii notowań ({len(returns)} sesji, potrzeba co najmniej "
                          f"{min_observations}) - np. bardzo świeża spółka w portfelu."}

    tickers = list(returns.columns)
    n = len(tickers)
    w = np.array([max(float(weights.get(t, 0.0)), 0.0) for t in tickers])
    if w.sum() <= 0:
        return {"available": False, "reason": "Brak dodatniej wartości pozycji do policzenia wag."}
    w = w / w.sum()

    trading_days = 252
    cov = returns.cov().values * trading_days
    vol_each = np.sqrt(np.clip(np.diag(cov), 0, None))
    port_vol = math.sqrt(max(float(w @ cov @ w), 0.0))

    port_returns = returns.values @ w
    ann_return = float(np.mean(port_returns) * trading_days)
    rf = risk_free_rate_pct / 100.0
    sharpe = (ann_return - rf) / port_vol if port_vol > 0 else None

    equity = np.cumprod(1 + port_returns)
    peak = np.maximum.accumulate(equity)
    max_drawdown = float(((peak - equity) / peak).max())

    diversification_ratio = float(np.dot(w, vol_each) / port_vol) if port_vol > 0 else None

    corr = returns.corr()
    pairs = []
    for i in range(n):
        for j in range(i + 1, n):
            c = corr.iloc[i, j]
            if not (isinstance(c, float) and math.isnan(c)):
                pairs.append({"a": tickers[i], "b": tickers[j], "correlation": round(float(c), 2)})
    pairs.sort(key=lambda p: -p["correlation"])
    avg_corr = round(sum(p["correlation"] for p in pairs) / len(pairs), 2) if pairs else None

    warnings: list[str] = []
    if avg_corr is not None and avg_corr >= 0.7:
        warnings.append(
            f"Średnia korelacja między pozycjami wynosi {avg_corr} - portfel zachowuje się w dużej mierze "
            f"jak jeden zakład, mimo wielu spółek."
        )
    strong = [p for p in pairs if p["correlation"] >= 0.85]
    if strong:
        warnings.append("Bardzo silnie skorelowane pary (>= 0.85): " +
                        ", ".join(f"{p['a']}/{p['b']} ({p['correlation']})" for p in strong[:4]) + ".")
    if port_vol >= 0.40:
        warnings.append(f"Roczna zmienność portfela to {port_vol * 100:.0f}% - to bardzo wysoki poziom, "
                        f"spadki o kilkadziesiąt procent są przy takiej zmienności realnym scenariuszem.")

    return {
        "available": True,
        "observations": int(len(returns)),
        "period_start": returns.index[0].strftime("%Y-%m-%d"),
        "period_end": returns.index[-1].strftime("%Y-%m-%d"),
        "tickers": tickers,
        "weights_pct": {t: round(float(x) * 100, 1) for t, x in zip(tickers, w)},
        "annual_volatility_pct": round(port_vol * 100, 1),
        "annual_return_pct": round(ann_return * 100, 1),
        "sharpe": round(float(sharpe), 2) if sharpe is not None else None,
        "max_drawdown_pct": round(max_drawdown * 100, 1),
        "diversification_ratio": round(diversification_ratio, 2) if diversification_ratio is not None else None,
        "avg_correlation": avg_corr,
        "top_pairs": pairs[:5],
        "matrix": {"tickers": tickers, "values": [[round(float(v), 2) for v in row] for row in corr.values]},
        "risk_free_rate_pct": risk_free_rate_pct,
        "warnings": warnings,
    }


def compute_benchmark_comparison(closes_by_ticker: dict, weights: dict, benchmark_ticker: str,
                                  benchmark_closes, risk_free_rate_pct: float = 0.0,
                                  min_observations: int = 60) -> dict:
    """Porównuje HIPOTETYCZNY portfel (obecne wagi pozycji, stałe w czasie) z
    benchmarkiem na wspólnej historii cen (zwykle 1 rok).

    Dlaczego hipotetyczny, a nie krzywa kapitału z bazy: snapshoty kapitału
    zawierają wpłaty i sprzedaże (kupno kolejnej akcji podnosi wartość portfela
    bez żadnego zysku), a nie mamy zapisanych przepływów gotówki, więc nie da
    się z nich uczciwie policzyć stopy zwrotu do porównania z indeksem.

    Zwraca m.in.: zwrot portfela i benchmarku, betę (jak mocno portfel reaguje
    na ruchy rynku), alfę Jensena w skali roku (część wyniku NIEWYJAŚNIONA samą
    ekspozycją na rynek), korelację oraz wychwyt wzrostów/spadków (jaką część
    ruchu rynku portfel łapał w dni wzrostowe i spadkowe rynku), a także serie
    do wykresu (wzrost 100 jednostek) i krótkie, opisowe wnioski."""
    if benchmark_closes is None or len(benchmark_closes) == 0:
        return {"available": False, "reason": f"Brak notowań benchmarku {benchmark_ticker}."}

    bench_key = "__benchmark__"
    series = {t: _series_to_daily_naive(pd.Series(s))
              for t, s in closes_by_ticker.items() if s is not None and len(s) > 0}
    if not series:
        return {"available": False, "reason": "Brak danych cenowych dla pozycji w portfelu."}
    series[bench_key] = _series_to_daily_naive(pd.Series(benchmark_closes))

    prices = pd.concat(series, axis=1, join="inner").dropna()
    returns = prices.pct_change().dropna()
    if len(returns) < min_observations:
        return {"available": False,
                "reason": f"Za mało wspólnej historii z benchmarkiem {benchmark_ticker} "
                          f"({len(returns)} sesji, potrzeba co najmniej {min_observations})."}

    tickers = [c for c in returns.columns if c != bench_key]
    w = np.array([max(float(weights.get(t, 0.0)), 0.0) for t in tickers])
    if w.sum() <= 0:
        return {"available": False, "reason": "Brak dodatniej wartości pozycji do policzenia wag."}
    w = w / w.sum()

    p = returns[tickers].values @ w
    b = returns[bench_key].values
    var_b = float(np.var(b, ddof=1))
    if var_b <= 0:
        return {"available": False, "reason": f"Benchmark {benchmark_ticker} nie zmieniał ceny w badanym okresie."}

    trading_days = 252
    beta = float(np.cov(p, b, ddof=1)[0, 1] / var_b)
    corr = float(np.corrcoef(p, b)[0, 1])
    rf_daily = risk_free_rate_pct / 100.0 / trading_days
    alpha_annual = float(((p.mean() - rf_daily) - beta * (b.mean() - rf_daily)) * trading_days)

    p_curve = np.cumprod(1 + p)
    b_curve = np.cumprod(1 + b)
    port_total = float(p_curve[-1] - 1)
    bench_total = float(b_curve[-1] - 1)
    excess = port_total - bench_total

    def capture(mask):
        if int(mask.sum()) < 5:
            return None
        denom = float(b[mask].mean())
        return float(p[mask].mean() / denom) if denom != 0 else None

    up_capture = capture(b > 0)
    down_capture = capture(b < 0)

    # --- Opisowe wnioski (bez rekomendacji - tylko co wynika z liczb) ---
    insights: list[str] = []
    if excess >= 0:
        insights.append(f"W badanym okresie portfel (przy obecnych wagach) wyprzedził {benchmark_ticker} "
                        f"o {excess * 100:.1f} pkt proc.")
    else:
        insights.append(f"W badanym okresie portfel (przy obecnych wagach) przegrał z {benchmark_ticker} "
                        f"o {abs(excess) * 100:.1f} pkt proc.")

    if beta >= 1.15:
        insights.append(f"Beta {beta:.2f}: portfel reaguje na ruchy rynku mocniej niż benchmark, więc część "
                        f"wyniku (w górę i w dół) wynika po prostu z wyższej ekspozycji na rynek.")
    elif beta <= 0.85:
        insights.append(f"Beta {beta:.2f}: portfel jest mniej wrażliwy na ruchy rynku niż benchmark.")
    else:
        insights.append(f"Beta {beta:.2f}: wrażliwość na rynek zbliżona do benchmarku.")

    if alpha_annual >= 0.02:
        insights.append(f"Alfa {alpha_annual * 100:+.1f}% rocznie: po uwzględnieniu bety wynik wykracza poza samą "
                        f"ekspozycję na rynek (na tej próbie — niekoniecznie trwale).")
    elif alpha_annual <= -0.02:
        insights.append(f"Alfa {alpha_annual * 100:+.1f}% rocznie: po uwzględnieniu bety portfel wypadał gorzej, "
                        f"niż wynikałoby z samej ekspozycji na rynek.")
    else:
        insights.append("Alfa bliska zera: wynik w zasadzie tłumaczy sama ekspozycja na rynek (beta).")

    if excess > 0 and beta >= 1.15 and alpha_annual < 0.02:
        insights.append("Przewaga nad benchmarkiem wynika głównie z wyższej bety, a nie z trafnej selekcji "
                        "spółek — w słabszym rynku ta sama beta działałaby w drugą stronę.")

    if up_capture is not None and down_capture is not None:
        insights.append(f"W dni wzrostowe rynku portfel łapał średnio {up_capture * 100:.0f}% jego ruchu, "
                        f"w dni spadkowe — {down_capture * 100:.0f}%.")

    if len(returns) < 120:
        insights.append(f"Próba jest krótka ({len(returns)} sesji) — alfa i beta mają duży błąd statystyczny.")

    dates = returns.index.strftime("%Y-%m-%d").tolist()
    return {
        "available": True,
        "benchmark": benchmark_ticker,
        "observations": int(len(returns)),
        "period_start": dates[0],
        "period_end": dates[-1],
        "tickers": tickers,
        "portfolio_return_pct": round(port_total * 100, 1),
        "benchmark_return_pct": round(bench_total * 100, 1),
        "excess_return_pct": round(excess * 100, 1),
        "beta": round(beta, 2),
        "correlation": round(corr, 2),
        "alpha_annual_pct": round(alpha_annual * 100, 1),
        "up_capture": round(up_capture, 2) if up_capture is not None else None,
        "down_capture": round(down_capture, 2) if down_capture is not None else None,
        "curve": {
            "dates": dates,
            "portfolio": [round(float(100 * x), 2) for x in p_curve],
            "benchmark": [round(float(100 * x), 2) for x in b_curve],
        },
        "insights": insights,
    }

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
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from .technical_analysis import TechnicalResult
from .llm_sentiment import SentimentResult, SentimentTrendResult
from .fundamentals import FundamentalResult

# Kategorie, dla których koncentracja sektorowa jest istotna - głównie "kupuj",
# bo kilka jednoczesnych sygnałów kupna w tym samym sektorze to jeden
# skoncentrowany zakład, a nie kilka niezależnych okazji.
_CONCENTRATION_TARGET_CATEGORIES = {"WARTO OBSERWOWAĆ - możliwy dobry punkt wejścia"}


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

def evaluate_portfolio_position(position: dict, result: dict | None) -> dict:
    """Ocenia POSIADANĄ pozycję w świetle najnowszego wyniku analizy tej
    spółki. To NIE jest porada inwestycyjna ani podatkowa - to zestaw
    prostych, przejrzystych reguł łączących: cenę zakupu vs bieżącą,
    sugerowany stop-loss (ATR) i aktualny sygnał/kategorię narzędzia."""
    base = {
        "current_price": None, "unrealized_pct": None, "unrealized_value": None,
        "market_value": None, "cost_basis": None, "holding_days": None,
        "horizon": "brak danych", "category": None, "signal": None,
        "action": "BRAK DANYCH", "reasons": ["Spółka nie jest jeszcze przeanalizowana - poczekaj na cykl."],
        "currency": None,
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
    stop_loss = result["technical"]["metrics"].get("suggested_stop_loss")

    action = "TRZYMAJ"
    reasons: list[str] = []

    if stop_loss is not None and current_price < stop_loss:
        action = "ROZWAŻ SPRZEDAŻ"
        reasons.append(f"Cena ({current_price:.2f}) spadła poniżej sugerowanego stop-loss ({stop_loss:.2f}).")

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
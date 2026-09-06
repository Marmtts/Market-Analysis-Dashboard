"""
analysis_engine.py
---------------------
Wspólna logika pełnego cyklu analizy (kontekst makro -> watchlista -> newsy
makro -> odkrywanie kandydatów), WYDZIELONA z main.py, żeby mogła być
reużywana zarówno przez CLI (`src/main.py`), jak i przez serwer webowy
(`src/web_app.py`) bez duplikacji kodu.

Zamiast pisać bezpośrednio do konsoli (`rich`), funkcje przyjmują opcjonalny
callback `on_progress(message, level)`, który wywołujący (CLI albo web
dashboard) może podłączyć do dowolnego "ujścia" - konsoli, WebSocketu, logu
w bazie itd.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from typing import Callable

from src.market_data import get_ticker_data
from src.technical_analysis import analyze_multi_timeframe
from src.news_sources import get_ticker_news, get_macro_headlines
from src.news_history import fetch_historical_news_by_month
from src.llm_sentiment import analyze_sentiment, analyze_historical_trend
from src.discovery import discover_candidates
from src.fundamentals import fetch_fundamentals
from src.macro_context import fetch_macro_context, risk_regime_score_adjustment
from src.report import combine_scores, build_company_report, compute_sector_concentration
from src.json_utils import sanitize_for_json

ProgressCallback = Callable[[str, str], None]  # (message, level) -> None


def _noop_progress(message: str, level: str = "info") -> None:
    pass


def summarize_macro(headlines) -> str:
    if not headlines:
        return "Brak nagłówków makro do wyświetlenia."
    lines = [f"- [{h.source}] {h.title}" for h in headlines[:10]]
    return "\n".join(lines)


def analyze_company(company: dict, cfg: dict, threshold_adjustment: float = 0.0,
                     on_progress: ProgressCallback | None = None) -> dict | None:
    """Pełny pipeline analizy jednej spółki (techniczna wielointerwałowa +
    sentyment + trend historyczny + fundamenty). Zwraca None, jeśli nie
    udało się pobrać danych (np. nieprawidłowy/nieistniejący ticker)."""
    progress = on_progress or _noop_progress
    ticker = company["ticker"]
    name = company.get("name", ticker)
    progress(f"Analizuję: {name} ({ticker})...", "info")

    try:
        data = get_ticker_data(
            ticker,
            period=cfg["technical"]["history_period"],
            interval=cfg["technical"]["interval"],
        )
    except Exception as exc:  # noqa: BLE001
        progress(f"Pominięto {ticker}: nie udało się pobrać danych ({exc})", "error")
        return None

    technical = analyze_multi_timeframe(
        ticker=ticker,
        daily_history=data.history,
        fifty_two_week_high=data.fifty_two_week_high,
        fifty_two_week_low=data.fifty_two_week_low,
        cfg=cfg["technical"],
    )
    # Waluta notowania - dołączona tu (nie w technical_analysis.py), żeby
    # nie mieszać odpowiedzialności modułu liczącego wskaźniki z danymi
    # rynkowymi niezwiązanymi z ceną jako taką. Zakładamy, że cena docelowa
    # analityków (fundamentals.py) jest w tej samej walucie co notowanie.
    technical.metrics["currency"] = data.currency

    headlines = []
    if cfg["news"].get("use_ticker_news_from_yfinance", True):
        headlines = get_ticker_news(ticker, max_headlines=cfg["news"]["max_headlines_per_ticker"])

    sentiment = analyze_sentiment(ticker, name, headlines, cfg["llm"])

    trend = None
    if cfg["news"].get("use_historical_news", False):
        api_key = cfg["news"].get("finnhub_api_key", "")
        if api_key and api_key != "d8ls8jhr01qnkjl8jfb0d8ls8jhr01qnkjl8jfbg":
            progress(f"  Pobieram newsy historyczne dla {ticker} (Finnhub, "
                      f"{cfg['news'].get('historical_lookback_days', 365)} dni wstecz)...", "info")
            monthly_headlines = fetch_historical_news_by_month(
                ticker,
                api_key=api_key,
                days_back=cfg["news"].get("historical_lookback_days", 365),
                max_per_month=cfg["news"].get("max_headlines_per_month", 4),
            )
            trend = analyze_historical_trend(ticker, name, monthly_headlines, cfg["llm"])

    fundamentals = None
    if cfg.get("fundamentals", {}).get("enabled", False):
        fundamentals = fetch_fundamentals(ticker, cfg["fundamentals"], current_price=data.last_price)

    combined = combine_scores(
        technical, sentiment, cfg["scoring"],
        trend=trend, fundamentals=fundamentals,
        threshold_adjustment=threshold_adjustment,
    )
    result = build_company_report(company, technical, sentiment, combined,
                                   trend=trend, fundamentals=fundamentals,
                                   headlines=headlines)
    progress(f"Zakończono: {name} ({ticker}) -> {combined['category']}", "success")
    return result


def run_full_analysis(cfg: dict, watchlist: list[dict],
                       on_progress: ProgressCallback | None = None,
                       extra_excluded_tickers: list[str] | None = None) -> dict:
    """Wykonuje pełen cykl: kontekst makro -> watchlista -> discovery.
    Zwraca słownik gotowy do zserializowania (JSON) i wypchnięcia do UI."""
    progress = on_progress or _noop_progress

    threshold_adjustment = 0.0
    macro_ctx_dict = None
    if cfg.get("macro", {}).get("enabled", False):
        progress("Pobieram szeroki kontekst makro (VIX, rentowność obligacji)...", "info")
        macro_ctx = fetch_macro_context(cfg["macro"])
        macro_ctx_dict = asdict(macro_ctx)
        if macro_ctx.available:
            progress(f"Reżim ryzyka: {macro_ctx.risk_regime} (VIX={macro_ctx.vix_last})", "info")
            threshold_adjustment = risk_regime_score_adjustment(macro_ctx, cfg["macro"])
            if threshold_adjustment > 0:
                progress(f"Podnoszę próg sugestii kupna o {threshold_adjustment:+.2f} "
                          f"ze względu na podwyższone ryzyko rynkowe.", "warning")
        else:
            progress("Kontekst makro niedostępny - kontynuuję bez korekty progu.", "warning")

    progress("Pobieram ogólnorynkowe newsy (makro)...", "info")
    macro_headlines = get_macro_headlines(
        cfg["news"]["macro_rss_feeds"], max_total=cfg["news"]["max_macro_headlines"]
    )
    macro_summary = summarize_macro(macro_headlines)

    all_results = []
    for company in watchlist:
        result = analyze_company(company, cfg, threshold_adjustment=threshold_adjustment,
                                  on_progress=on_progress)
        if result:
            all_results.append(result)

    # --- Odkrywanie nowych spółek (LLM proponuje kandydatów) ---
    discovered_results = []
    discovery_cfg = cfg.get("discovery", {})
    if discovery_cfg.get("enabled", False):
        progress("Odkrywanie nowych spółek (LLM analizuje newsy makro)...", "info")
        # extra_excluded_tickers - spółki niedawno już zaproponowane przez AI
        # (patrz db.get_recently_proposed_tickers) - unikamy powtarzania tej
        # samej propozycji w kółko, dopóki nie minie okres "cooldownu".
        existing_tickers = [c["ticker"] for c in watchlist] + list(extra_excluded_tickers or [])
        candidates = discover_candidates(
            macro_headlines=macro_headlines,
            existing_tickers=existing_tickers,
            llm_cfg=cfg["llm"],
            max_candidates=discovery_cfg.get("max_candidates", 5),
        )

        if not candidates:
            progress("LLM nie zaproponował dziś żadnych nowych kandydatów.", "info")
        else:
            progress(f"LLM zaproponował {len(candidates)} kandydatów - weryfikuję tickery...", "info")
            for cand in candidates:
                progress(f"Propozycja: {cand.name} ({cand.ticker}) - {cand.reason}", "info")
                if not discovery_cfg.get("run_full_analysis_on_candidates", True):
                    continue
                candidate_cfg = {
                    "ticker": cand.ticker,
                    "xtb_symbol": f"{cand.ticker} (sprawdź dokładną nazwę w XTB)",
                    "name": cand.name,
                }
                result = analyze_company(candidate_cfg, cfg, threshold_adjustment=threshold_adjustment,
                                          on_progress=on_progress)
                if result:
                    result["discovery_reason"] = cand.reason
                    discovered_results.append(result)
                else:
                    progress(f"Kandydat {cand.ticker} odrzucony - błędny/nieistniejący ticker "
                              f"(możliwa halucynacja LLM).", "warning")

    progress("Analiza zakończona.", "success")

    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "macro_context": macro_ctx_dict,
        "macro_summary": macro_summary,
        "results": all_results,
        "discovered_results": discovered_results,
        "sector_concentration": compute_sector_concentration(all_results + discovered_results),
    }
    # Siatka bezpieczeństwa: niezależnie od tego, skąd wzięłaby się wartość
    # NaN/Infinity (np. nietypowe dane z yfinance), nigdy nie chcemy wysłać
    # jej dalej - psuje serializację JSON zarówno w REST API, jak i WebSocket.
    return sanitize_for_json(payload)

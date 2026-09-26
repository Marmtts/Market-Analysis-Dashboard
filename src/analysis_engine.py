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

from src.market_data import (
    get_ticker_data, fetch_benchmark_history, get_benchmark_fallbacks, BenchmarkUnavailable,
)
from src.technical_analysis import analyze_multi_timeframe, AT_TOP_DISTANCE_PENALTY, compute_relative_strength
from src.news_sources import (
    get_ticker_news, get_macro_headlines, get_gpw_market_headlines, filter_headlines_for_company, Headline,
)
from src.news_history import fetch_historical_news_by_month
from src.llm_sentiment import analyze_sentiment, analyze_historical_trend
from src.discovery import discover_candidates
from src.fundamentals import fetch_fundamentals
from src.macro_context import fetch_macro_context, risk_regime_score_adjustment
from src.macro_calendar import get_upcoming_events
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


# Które zastępcze źródła benchmarku już zgłosiliśmy w tym uruchomieniu procesu -
# unika powtarzania tej samej informacji w logu na żywo dla każdej spółki.
_reported_benchmark_fallbacks: set[str] = set()


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

    # Uzupełnienie dla GPW (.WA) - Finnhub (niżej) zwraca 403 dla większości
    # takich spółek (poza darmowym planem), a yfinance wyżej ma bardzo skąpe
    # pokrycie polskich tickerów. Zamiast scrapować podstrony pojedynczych
    # spółek (kruche, już wcześniej rozważone i odrzucone), filtrujemy jeden,
    # ogólny kanał RSS o GPW po nazwie spółki - patrz news_sources.py.
    # gpw_matches jest też reużywane niżej do budowania lokalnego archiwum
    # historycznego (patrz komentarz przy `trend`).
    gpw_matches: list = []
    gpw_rss_url = cfg["news"].get("gpw_rss_url")
    if ticker.upper().endswith(".WA") and gpw_rss_url:
        gpw_feed = get_gpw_market_headlines(gpw_rss_url)
        gpw_matches = filter_headlines_for_company(gpw_feed, name, ticker.split(".")[0])
        seen_titles = {h.title for h in headlines}
        for h in gpw_matches:
            if h.title not in seen_titles:
                headlines.append(h)
                seen_titles.add(h.title)

    sentiment = analyze_sentiment(ticker, name, headlines, cfg["llm"])

    trend = None
    if cfg["news"].get("use_historical_news", False):
        if ticker.upper().endswith(".WA"):
            # Finnhub jest tu POMIJANY CELOWO, nie tylko "nieudany" - wiadomo
            # z góry, że zwróci 403 (poza darmowym planem dla spółek spoza
            # głównych giełd US), więc odpytywanie go marnowałoby zapytanie
            # co cykl bez szans powodzenia. Zamiast tego budujemy WŁASNY,
            # lokalny odpowiednik historii: co cykl dopisujemy dzisiejsze
            # trafienia z ogólnego kanału RSS o GPW (gpw_matches, wyżej) do
            # cache'u BIEŻĄCEGO miesiąca (ten sam news_cache co Finnhub, patrz
            # news_history.py), a trend liczymy z tego, co się w cache'u
            # uzbierało - nawet z jednego miesiąca. WAŻNE ograniczenie: nie da
            # się tak odtworzyć PRZESZŁOŚCI (RSS nie ma archiwum, tylko
            # "teraz") - ale po kilku miesiącach działania dashboardu "trend
            # sentymentu" dla spółek z GPW zacznie mieć realne dane zamiast
            # być zawsze niedostępny.
            from . import db
            if gpw_matches:
                month_key = datetime.now().strftime("%Y-%m")
                existing = db.get_cached_news_months(ticker).get(month_key, [])
                existing_titles = {h["title"] for h in existing}
                new_entries = [h.__dict__ for h in gpw_matches if h.title not in existing_titles]
                if new_entries:
                    db.save_news_month_to_cache(ticker, month_key, existing + new_entries)
            cached_months = db.get_cached_news_months(ticker)
            if cached_months:
                monthly_headlines = {mk: [Headline(**h) for h in hs] for mk, hs in cached_months.items()}
                trend = analyze_historical_trend(ticker, name, monthly_headlines, cfg["llm"])
        else:
            api_key = cfg["news"].get("finnhub_api_key", "")
            if api_key:
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

    # --- Siła względna wobec benchmarku (np. S&P500 dla USD, WIG20 dla PLN) ---
    # Niewielki, celowo ograniczony wpływ na wynik (+/-0.05, analogicznie do
    # trend_adjustment w report.py) - to dodatkowy kontekst, nie dominujący
    # czynnik, bo już mamy wagę techniczną 0.55 opartą o absolutną cenę.
    benchmark_ticker = cfg["technical"].get("benchmark_by_currency", {}).get(data.currency, "SPY")
    rel_strength = None
    try:
        fallbacks = get_benchmark_fallbacks(benchmark_ticker, cfg["technical"])
        benchmark_hist = fetch_benchmark_history(
            benchmark_ticker, period=cfg["technical"]["history_period"], fallbacks=fallbacks,
        )
        lookback = cfg["technical"].get("relative_strength_lookback_days", 60)
        rel_strength = compute_relative_strength(data.history, benchmark_hist, lookback)
        benchmark_label = benchmark_hist.attrs.get("label", benchmark_ticker)
        used_source = benchmark_hist.attrs.get("source", benchmark_ticker)
        if used_source != benchmark_ticker and used_source not in _reported_benchmark_fallbacks:
            # Widoczne w logu na żywo (nie tylko w logu serwera) - ale tylko RAZ na benchmark
            # dzięki pamięci fetch_benchmark_history, żeby nie zalewać loga tą samą informacją
            # dla każdej kolejnej spółki tej samej waluty w tym cyklu.
            progress(f"  Benchmark {benchmark_ticker} niedostępny w Yahoo Finance - używam zastępczego "
                     f"źródła: {used_source}.", "warning")
            _reported_benchmark_fallbacks.add(used_source)
    except BenchmarkUnavailable as exc:
        # Zapamiętane na kilka minut w market_data.py - nie zalewamy logu tym samym
        # ostrzeżeniem dla każdej kolejnej spółki tej samej waluty w tym cyklu.
        progress(f"  Benchmark {benchmark_ticker} niedostępny - pomijam siłę względną dla {ticker} "
                 f"(kolejna próba za kilka minut). Szczegóły: {exc}", "warning")
    except Exception as exc:  # noqa: BLE001
        progress(f"  Nie udało się policzyć siły względnej dla {ticker}: {exc}", "warning")

    if rel_strength:
        rel_strength["benchmark"] = benchmark_ticker
        rel_strength["benchmark_label"] = benchmark_label
        technical.metrics["relative_strength"] = rel_strength
        rs = rel_strength["relative_strength_pct"]
        if rs >= 10:
            technical.score = float(min(1.0, technical.score + 0.05))
            technical.reasons.append(
                f"Spółka bije benchmark ({benchmark_label}) o {rs:+.1f} pkt proc. w ostatnich "
                f"{lookback} sesjach - silna siła względna, nie tylko 'płynie z rynkiem'."
            )
        elif rs <= -10:
            technical.score = float(max(0.0, technical.score - 0.05))
            technical.reasons.append(
                f"Spółka wyraźnie przegrywa z benchmarkiem ({benchmark_label}): {rs:+.1f} pkt proc. "
                f"w ostatnich {lookback} sesjach - wzrost ceny może być głównie efektem rynku, nie siły spółki."
            )

    # --- Korekta dla ETF-ów: bliskość 52-tyg. maksimum to co innego dla
    # zdywersyfikowanego funduszu indeksowego (normalne w długoterminowej
    # hossie) niż dla pojedynczej spółki (ryzyko kupna "na szczycie" hype'u).
    # Cofamy karę TYLKO jeśli blokada wynikała WYŁĄCZNIE z bliskości maksimum,
    # a nie z wykupienia wg RSI - ekstremalne RSI wciąż jest sygnałem
    # ostrożności nawet dla szerokiego ETF-u.
    if (fundamentals and fundamentals.available
            and fundamentals.metrics.get("quote_type") == "ETF"
            and technical.signal == "AT_TOP"):
        rsi_val = technical.metrics.get("rsi")
        rsi_overbought = cfg["technical"]["rsi_overbought"]
        if rsi_val is None or rsi_val < rsi_overbought:
            technical.score = float(min(1.0, technical.score + AT_TOP_DISTANCE_PENALTY))
            technical.signal = (
                "GOOD_ENTRY" if (technical.score >= 0.65 and technical.metrics.get("uptrend"))
                else "NEUTRAL"
            )
            technical.reasons.append(
                "To ETF (fundusz indeksowy) - bliskość historycznych maksimów w silnym, "
                "długoterminowym trendzie wzrostowym jest tu normalna i NIE jest traktowana "
                "jako sygnał 'kupna na szczycie', w przeciwieństwie do pojedynczej spółki."
            )

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
        "macro_calendar": get_upcoming_events(cfg.get("macro_calendar", {})),
        "results": all_results,
        "discovered_results": discovered_results,
        "sector_concentration": compute_sector_concentration(all_results + discovered_results),
        # Próg (w dniach) dla ostrzeżeń o wynikach kwartalnych - frontend
        # używa go do wyróżniania kart i banerów.
        "earnings_warning_days": cfg.get("fundamentals", {}).get("earnings_warning_days", 7),
    }
    # Siatka bezpieczeństwa: niezależnie od tego, skąd wzięłaby się wartość
    # NaN/Infinity (np. nietypowe dane z yfinance), nigdy nie chcemy wysłać
    # jej dalej - psuje serializację JSON zarówno w REST API, jak i WebSocket.
    return sanitize_for_json(payload)

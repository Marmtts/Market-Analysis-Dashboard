"""
web_app.py
------------
Dashboard webowy XTB Trend Watch. Uruchom:

    python -m src.web_app --config config.yaml

Potem otwórz w przeglądarce: http://localhost:8000

Co robi w tle:
- Co `web.refresh_interval_minutes` minut (domyślnie 60) automatycznie
  uruchamia pełen cykl analizy (src/analysis_engine.run_full_analysis) dla
  aktualnej watchlisty z bazy danych.
- Postęp analizy w czasie rzeczywistym oraz finalne wyniki są wypychane do
  WSZYSTKICH podłączonych przeglądarek przez WebSocket (/ws).
- Wyniki są też cache'owane w SQLite, więc świeżo otwarta karta przeglądarki
  od razu widzi ostatnie dostępne dane, zanim skończy się kolejny cykl.

Endpointy REST:
- GET  /api/watchlist                - lista obserwowanych spółek
- POST /api/watchlist                - dodaj spółkę {ticker, name, xtb_symbol?}
- DELETE /api/watchlist/{ticker}     - usuń spółkę
- GET  /api/results                  - ostatnie wyniki analizy (cache)
- GET  /api/chart/{ticker}           - dane OHLC do wykresu świecowego
- GET  /api/logs                     - ostatnie logi
- POST /api/run-now                  - wymuś natychmiastową analizę (w tle)
- WS   /ws                            - kanał live-update
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

from contextlib import asynccontextmanager

import yaml
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

sys.path.append(str(Path(__file__).resolve().parent.parent))

import yfinance as yf

from src import db
from src.analysis_engine import run_full_analysis, analyze_company
from src.market_data import fetch_history
from src.json_utils import sanitize_for_json
from src.report import (
    evaluate_portfolio_position, compute_max_drawdown,
    compute_portfolio_risk_summary, compute_portfolio_sector_exposure,
    compute_tax_summary,
)
from src.daily_brief import generate_daily_brief
from src.chatbot import answer_chat_question
from src.report import summarize_portfolio_by_currency
from src.fx_rates import get_fx_rate

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("xtb_trend_watch.web_app")

STATIC_DIR = Path(__file__).resolve().parent.parent / "web" / "static"

_cfg: dict = {}
_is_running: bool = False
_next_run_at: datetime | None = None
# Ostatnie ręczne odświeżenie per ticker (timestamp) - chroni yfinance/Finnhub
# przed zbyt częstymi zapytaniami o tę samą spółkę (patrz manual_refresh_cooldown_seconds).
_last_manual_refresh: dict[str, float] = {}
# Zapamiętuje ostatni WYSŁANY typ alertu cenowego per pozycja portfela -
# zapobiega spamowaniu tym samym powiadomieniem co kilka minut. Klucz:
# (position_id, alert_type) -> True. Czyszczone, gdy sytuacja przestaje
# spełniać warunek alertu (patrz _check_price_alerts).
_sent_price_alerts: set[tuple[int, str]] = set()


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _broadcast_loop
    _broadcast_loop = asyncio.get_event_loop()
    db.init_db(seed_watchlist=_cfg.get("watchlist", []))
    task = asyncio.create_task(_background_scheduler())
    price_alert_task = asyncio.create_task(_price_alert_scheduler())
    logger.info("XTB Trend Watch Dashboard wystartował.")
    yield
    task.cancel()
    price_alert_task.cancel()


app = FastAPI(title="XTB Trend Watch Dashboard", lifespan=lifespan)


# =====================================================================
# WebSocket - broadcast do wszystkich podłączonych przeglądarek
# =====================================================================

class ConnectionManager:
    def __init__(self) -> None:
        self.active: list[WebSocket] = []

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self.active.append(ws)

    def disconnect(self, ws: WebSocket) -> None:
        if ws in self.active:
            self.active.remove(ws)

    async def broadcast(self, payload: dict) -> None:
        dead = []
        for ws in self.active:
            try:
                await ws.send_json(payload)
            except Exception:  # noqa: BLE001
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)


manager = ConnectionManager()
_broadcast_loop: asyncio.AbstractEventLoop | None = None


def _sync_progress_to_broadcast(message: str, level: str = "info") -> None:
    """Callback wywoływany SYNCHRONICZNIE z wątku roboczego (analiza jest
    blokująca - yfinance/requests) - bezpiecznie planuje broadcast na pętli
    asyncio serwera."""
    db.log_event(level, message)
    if _broadcast_loop is not None:
        asyncio.run_coroutine_threadsafe(
            manager.broadcast({"type": "progress", "level": level, "message": message}),
            _broadcast_loop,
        )


# =====================================================================
# Logika uruchamiania analizy w tle (w osobnym wątku, żeby nie blokować
# pętli asyncio - yfinance/requests/Ollama to wywołania synchroniczne)
# =====================================================================

def _find_company_cfg(ticker: str) -> dict:
    """Znajduje konfigurację spółki (nazwa, symbol XTB) po tickerze - najpierw
    w watchliście, potem wśród ostatnio odkrytych przez AI kandydatów, a jako
    fallback buduje minimalną konfigurację z samego tickera."""
    ticker_upper = ticker.upper()
    for c in db.get_watchlist():
        if c["ticker"].upper() == ticker_upper:
            return c
    cached = db.load_results_cache()
    if cached:
        for r in (cached.get("discovered_results") or []):
            if r["ticker"].upper() == ticker_upper:
                return {"ticker": r["ticker"], "xtb_symbol": r.get("xtb_symbol", r["ticker"]),
                        "name": r.get("name", r["ticker"])}
    return {"ticker": ticker_upper, "xtb_symbol": f"{ticker_upper} (sprawdź w XTB)", "name": ticker_upper}


def _refresh_single_ticker_blocking(ticker: str) -> dict:
    """Pełna analiza JEDNEJ spółki na żądanie (ceny, RSI/ATR, sentyment LLM,
    fundamenty) - bez czekania na cały cykl watchlisty. Wynik podmienia
    odpowiedni wpis w cache'u i jest rozgłaszany przez WebSocket, żeby
    wszystkie otwarte karty przeglądarki od razu zobaczyły świeże dane."""
    company_cfg = _find_company_cfg(ticker)
    result = analyze_company(company_cfg, _cfg, threshold_adjustment=0.0,
                              on_progress=_sync_progress_to_broadcast)
    if result is None:
        raise ValueError(f"Nie udało się pobrać danych dla {ticker} (nieprawidłowy ticker lub błąd API).")
    result = sanitize_for_json(result)

    db.record_score_snapshot(result["ticker"], result["combined"]["final_score"],
                              result["combined"]["category"], result["technical"]["signal"],
                              source="manual_refresh",
                              market_price=result["technical"]["metrics"].get("last_price"))

    cached = db.load_results_cache() or {"results": [], "discovered_results": []}
    updated = False
    for key in ("results", "discovered_results"):
        items = cached.get(key) or []
        for i, r in enumerate(items):
            if r["ticker"].upper() == result["ticker"].upper():
                items[i] = result
                updated = True
                break
        cached[key] = items
    if not updated:
        cached.setdefault("results", []).append(result)

    cached["generated_at"] = datetime.now().isoformat(timespec="seconds")
    db.save_results_cache(cached["generated_at"], sanitize_for_json(cached))
    return result

def _get_enriched_open_positions(cached: dict | None = None) -> list[dict]:
    """Łączy otwarte pozycje portfela z najnowszym wynikiem analizy tej
    spółki - reużywane przez /api/portfolio, panel ryzyka i zapis krzywej
    kapitału, żeby nie duplikować tej samej logiki w trzech miejscach."""
    positions = db.get_portfolio(status="open")
    cached = cached if cached is not None else (db.load_results_cache() or {})
    by_ticker = {r["ticker"]: r for r in (cached.get("results") or []) + (cached.get("discovered_results") or [])}
    enriched = []
    for p in positions:
        result = by_ticker.get(p["ticker"])
        evaluation = evaluate_portfolio_position(p, result)
        enriched.append({**p, **evaluation})
    return enriched

def _build_combined_portfolio_view(enriched_positions: list[dict], base_currency: str) -> tuple[list[dict], set]:
    """Przelicza kwoty pieniężne KAŻDEJ pozycji na base_currency, żeby móc
    je bezpiecznie zsumować (bez tego mieszalibyśmy np. USD i PLN jak tę
    samą jednostkę - patrz naprawiony bug w compute_portfolio_sector_exposure).
    Zwraca (przeliczone_pozycje, zbiór_walut_ktorych_nie_udalo_sie_przeliczyc)."""
    converted = []
    skipped = set()
    for p in enriched_positions:
        currency = p.get("currency") or "USD"
        rate = get_fx_rate(currency, base_currency)
        if rate is None:
            skipped.add(currency)
            continue
        p2 = dict(p)
        for key in ("market_value", "cost_basis", "unrealized_value", "current_price", "suggested_stop_loss"):
            if p2.get(key) is not None:
                p2[key] = p2[key] * rate
        p2["currency"] = base_currency  # dla compute_portfolio_risk_summary - jeden wspólny "koszyk"
        converted.append(p2)
    return converted, skipped

def _run_analysis_blocking() -> dict:
    watchlist = db.get_watchlist()
    cooldown_days = _cfg.get("discovery", {}).get("cooldown_days", 14)
    recently_proposed = db.get_recently_proposed_tickers(cooldown_days=cooldown_days)
    payload = run_full_analysis(_cfg, watchlist, on_progress=_sync_progress_to_broadcast,
                                 extra_excluded_tickers=recently_proposed)
    db.save_results_cache(payload["generated_at"], payload)

    for r in payload["discovered_results"]:
        db.record_discovery_proposal(r["ticker"])

    # Zapisujemy "zdjęcie" werdyktu każdej przeanalizowanej spółki - to
    # buduje historię, którą potem rysujemy jako znaczniki na wykresie ceny.
    open_position_tickers = {p["ticker"] for p in db.get_portfolio(status="open")}
    for r in payload["results"]:
        db.record_score_snapshot(r["ticker"], r["combined"]["final_score"],
                                  r["combined"]["category"], r["technical"]["signal"],
                                  source="watchlist",
                                  market_price=r["technical"]["metrics"].get("last_price"))
        if r["ticker"] in open_position_tickers:
            currency = r["technical"]["metrics"].get("currency")
            if currency:
                for p in db.get_portfolio(status="open"):
                    if p["ticker"] == r["ticker"]:
                        db.set_position_currency(p["id"], currency)
    for r in payload["discovered_results"]:
        db.record_score_snapshot(r["ticker"], r["combined"]["final_score"],
                                  r["combined"]["category"], r["technical"]["signal"],
                                  source="discovery",
                                  market_price=r["technical"]["metrics"].get("last_price"))

    # Krzywa kapitału portfela - "zdjęcie" łącznej wartości/kosztu per
    # waluta przy KAŻDYM pełnym cyklu (nie przy odświeżeniach pojedynczych
    # spółek), żeby punkty na krzywej były regularne w czasie.
    enriched_positions = _get_enriched_open_positions(payload)
    equity_by_currency: dict[str, dict] = {}
    for p in enriched_positions:
        currency = p.get("currency") or "USD"
        bucket = equity_by_currency.setdefault(currency, {"value": 0.0, "cost": 0.0})
        bucket["value"] += p.get("market_value") or 0.0
        bucket["cost"] += p.get("cost_basis") or 0.0
    for currency, totals in equity_by_currency.items():
        db.record_portfolio_equity_snapshot(currency, totals["value"], totals["cost"])

    # Widok ŁĄCZNY (wszystkie waluty przeliczone na jedną, base_currency) -
    # kursem AKTUALNYM W TYM CYKLU, więc historia w czasie jest dokładna.
    # ZAWSZE zapisujemy snapshot, nawet jeśli część walut nie dała się
    # przeliczyć - inaczej jedna chwilowo niedostępna para walutowa
    # (np. przejściowy problem Yahoo Finance) wstrzymywałaby całą krzywą
    # kapitału w nieskończoność, mimo że mamy częściowe, sensowne dane.
    base_currency = _cfg.get("portfolio", {}).get("base_currency", "PLN")
    combined_positions, skipped_currencies = _build_combined_portfolio_view(enriched_positions, base_currency)
    combined_value = sum(p.get("market_value") or 0.0 for p in combined_positions)
    combined_cost = sum(p.get("cost_basis") or 0.0 for p in combined_positions)

    _sync_progress_to_broadcast(
        f"Widok łączny portfela: {len(combined_positions)}/{len(enriched_positions)} pozycji przeliczonych "
        f"na {base_currency}, wartość={combined_value:.2f}, koszt={combined_cost:.2f}.", "info"
    )
    db.record_portfolio_equity_combined_snapshot(base_currency, combined_value, combined_cost)

    if skipped_currencies:
        msg = (f"Nie udało się pobrać kursu dla walut {sorted(skipped_currencies)} -> {base_currency} - "
               f"pominięte w widoku łącznym portfela w tym cyklu (spróbuję ponownie w kolejnym).")
        logger.warning(msg)
        _sync_progress_to_broadcast(msg, "warning")

    # Codzienny brief AI - generowany na końcu, gdy mamy już PEŁEN obraz
    # (wyniki + portfel + ryzyko) do podsumowania w jednym spójnym tekście.
    portfolio_summary_for_brief = None
    if equity_by_currency:
        risk_data = compute_portfolio_risk_summary(enriched_positions)
        portfolio_summary_for_brief = {"by_currency": {}}
        for currency, totals in equity_by_currency.items():
            pl_pct = ((totals["value"] - totals["cost"]) / totals["cost"] * 100) if totals["cost"] > 0 else 0.0
            risk_for_currency = risk_data.get("by_currency", {}).get(currency, {})
            portfolio_summary_for_brief["by_currency"][currency] = {
                "value": totals["value"], "cost": totals["cost"], "pl_pct": pl_pct,
                "risk_pct": risk_for_currency.get("risk_pct"),
            }
        combined_pl_pct = ((combined_value - combined_cost) / combined_cost * 100) if combined_cost > 0 else 0.0
        portfolio_summary_for_brief["combined"] = {
            "base_currency": base_currency, "value": combined_value,
            "cost": combined_cost, "pl_pct": combined_pl_pct,
        }

    try:
        payload["daily_brief"] = generate_daily_brief(payload, portfolio_summary_for_brief, _cfg["llm"])
    except Exception:  # noqa: BLE001
        logger.exception("Błąd podczas generowania codziennego briefu - pomijam, reszta cyklu bez zmian.")
        payload["daily_brief"] = None

    db.save_results_cache(payload["generated_at"], payload)  # nadpisujemy z dołączonym briefem

    return payload


async def trigger_analysis_now() -> None:
    global _is_running, _next_run_at
    if _is_running:
        await manager.broadcast({"type": "progress", "level": "warning",
                                  "message": "Analiza już trwa - poczekaj na zakończenie bieżącego cyklu."})
        return
    _is_running = True
    await manager.broadcast({"type": "run_started", "message": "Rozpoczynam analizę..."})
    try:
        payload = await asyncio.to_thread(_run_analysis_blocking)
        await manager.broadcast({"type": "results", "payload": payload})
    except Exception as exc:  # noqa: BLE001
        logger.exception("Błąd podczas analizy")
        await manager.broadcast({"type": "progress", "level": "error",
                                  "message": f"Błąd krytyczny podczas analizy: {exc}"})
    finally:
        _is_running = False
        interval_min = _cfg.get("web", {}).get("refresh_interval_minutes", 60)
        _next_run_at = datetime.now().timestamp() + interval_min * 60
        await manager.broadcast({"type": "run_finished", "next_run_at": _next_run_at})


async def _background_scheduler() -> None:
    """Pętla działająca przez cały czas życia serwera - uruchamia analizę
    natychmiast po starcie, a potem co `refresh_interval_minutes`."""
    global _next_run_at
    interval_min = _cfg.get("web", {}).get("refresh_interval_minutes", 60)
    run_on_startup = _cfg.get("web", {}).get("run_analysis_on_startup", True)

    if run_on_startup:
        await trigger_analysis_now()
    else:
        _next_run_at = datetime.now().timestamp() + interval_min * 60

    while True:
        await asyncio.sleep(30)  # sprawdzaj co 30s, czy pora na kolejny cykl
        if _next_run_at is not None and datetime.now().timestamp() >= _next_run_at and not _is_running:
            await trigger_analysis_now()


def _check_price_alerts_blocking() -> list[dict]:
    """Szybki, LEKKI sprawdzian - TYLKO cena (jedno zapytanie yfinance na
    spółkę w portfelu), bez LLM/fundamentów/Finnhuba. Osobny od pełnego
    cyklu analizy, żeby wykrywać przebicie stop-lossu/ceny docelowej dużo
    częściej niż raz na godzinę, bez obciążania kosztownych źródeł danych.
    Zwraca listę nowych alertów do wysłania (i aktualizuje _sent_price_alerts,
    żeby nie powtarzać tego samego alertu w kółko)."""
    positions = db.get_portfolio()
    if not positions:
        return []

    by_ticker: dict[str, list[dict]] = {}
    for p in positions:
        by_ticker.setdefault(p["ticker"], []).append(p)

    cached = db.load_results_cache() or {}
    fundamentals_by_ticker = {
        r["ticker"]: r.get("fundamentals")
        for r in (cached.get("results") or []) + (cached.get("discovered_results") or [])
    }

    new_alerts = []
    active_alert_keys = set()

    for ticker, lots in by_ticker.items():
        try:
            history = fetch_history(ticker, period="5d", interval="1d", use_cache=False)
            current_price = float(history["Close"].iloc[-1])
            currency = "USD"
            try:
                fast_info = yf.Ticker(ticker).fast_info
                currency = fast_info.get("currency") or "USD"
            except Exception:  # noqa: BLE001
                pass
        except Exception as exc:  # noqa: BLE001
            logger.warning("Alert cenowy: nie udało się pobrać ceny dla %s: %s", ticker, exc)
            continue

        fund = fundamentals_by_ticker.get(ticker)
        target_mean = None
        if fund and fund.get("available"):
            target_mean = fund.get("metrics", {}).get("analyst_target_mean")

        for p in lots:
            # Stop-loss przybliżony na podstawie ceny zakupu (ATR nie jest
            # tu liczony - to celowo LEKKI check; pełen ATR pochodzi z
            # ostatniego pełnego cyklu analizy, cache'owanego w wynikach).
            cached_result = next(
                (r for r in (cached.get("results") or []) + (cached.get("discovered_results") or [])
                 if r["ticker"] == ticker), None
            )
            stop_loss = (cached_result["technical"]["metrics"].get("suggested_stop_loss")
                         if cached_result else None)

            if stop_loss is not None and current_price < stop_loss:
                key = (p["id"], "stop_loss")
                active_alert_keys.add(key)
                if key not in _sent_price_alerts:
                    new_alerts.append({
                        "type": "stop_loss", "position_id": p["id"], "ticker": ticker,
                        "currency": currency, "current_price": round(current_price, 2),
                        "stop_loss": round(stop_loss, 2),
                        "title": f"{ticker}: cena poniżej stop-loss",
                        "body": f"Cena {current_price:.2f} {currency} spadła poniżej sugerowanego "
                                f"stop-loss ({stop_loss:.2f} {currency}). Pozycja: {p['shares']} szt. @ {p['buy_price']}.",
                    })

            if target_mean is not None and current_price >= target_mean:
                key = (p["id"], "target_reached")
                active_alert_keys.add(key)
                if key not in _sent_price_alerts:
                    new_alerts.append({
                        "type": "target_reached", "position_id": p["id"], "ticker": ticker,
                        "currency": currency, "current_price": round(current_price, 2),
                        "target": round(target_mean, 2),
                        "title": f"{ticker}: osiągnięto cenę docelową analityków",
                        "body": f"Cena {current_price:.2f} {currency} osiągnęła średnią cenę docelową "
                                f"analityków ({target_mean:.2f} {currency}).",
                    })

    # Czyścimy alerty dla sytuacji, które przestały być aktualne (np. cena
    # wróciła nad stop-loss) - żeby ewentualny PONOWNY spadek znów wysłał alert.
    stale = _sent_price_alerts - active_alert_keys
    _sent_price_alerts.difference_update(stale)
    _sent_price_alerts.update(active_alert_keys)

    return new_alerts


async def _price_alert_scheduler() -> None:
    """Osobna, znacznie częstsza pętla niż pełny cykl analizy - sprawdza
    TYLKO ceny pozycji portfela, żeby wykryć przebicie stop-lossu/celu
    dużo szybciej niż raz na godzinę."""
    interval_sec = _cfg.get("web", {}).get("price_alert_interval_seconds", 180)
    if interval_sec <= 0:
        logger.info("Alerty cenowe wyłączone (price_alert_interval_seconds <= 0).")
        return

    while True:
        await asyncio.sleep(interval_sec)
        try:
            alerts = await asyncio.to_thread(_check_price_alerts_blocking)
            for alert in alerts:
                await manager.broadcast({"type": "price_alert", **alert})
        except Exception:  # noqa: BLE001
            logger.exception("Błąd podczas sprawdzania alertów cenowych")


class AddCompanyRequest(BaseModel):
    ticker: str
    name: str = ""
    xtb_symbol: str = ""


class AddPositionRequest(BaseModel):
    ticker: str
    shares: float
    buy_price: float
    buy_date: str   # "YYYY-MM-DD"
    notes: str = ""


class ClosePositionRequest(BaseModel):
    sell_price: float
    sell_date: str  # "YYYY-MM-DD"


class ChatRequest(BaseModel):
    messages: list[dict]  # [{"role": "user"|"assistant", "content": "..."}]


@app.get("/api/watchlist")
async def api_get_watchlist():
    return db.get_watchlist()


@app.post("/api/watchlist")
async def api_add_company(req: AddCompanyRequest):
    ok = db.add_company(req.ticker, req.name or req.ticker.upper(),
                         xtb_symbol=req.xtb_symbol or None, source="manual")
    if not ok:
        raise HTTPException(status_code=409, detail=f"Spółka {req.ticker.upper()} już jest na watchliście.")
    await manager.broadcast({"type": "watchlist_changed"})
    return {"status": "ok", "ticker": req.ticker.upper()}


@app.delete("/api/watchlist/{ticker}")
async def api_remove_company(ticker: str):
    ok = db.remove_company(ticker)
    if not ok:
        raise HTTPException(status_code=404, detail=f"Nie znaleziono spółki {ticker.upper()} na watchliście.")
    await manager.broadcast({"type": "watchlist_changed"})
    return {"status": "ok"}


@app.get("/api/results")
async def api_get_results():
    cached = db.load_results_cache()
    if not cached:
        return JSONResponse({"generated_at": None, "results": [], "discovered_results": [],
                              "macro_context": None, "macro_summary": ""})
    return cached


@app.get("/api/portfolio")
async def api_get_portfolio():
    return sanitize_for_json(_get_enriched_open_positions())


@app.get("/api/portfolio/risk")
async def api_get_portfolio_risk():
    enriched = _get_enriched_open_positions()
    base_currency = _cfg.get("portfolio", {}).get("base_currency", "PLN")
    cached = db.load_results_cache() or {}
    fundamentals_by_ticker = {
        r["ticker"]: r.get("fundamentals")
        for r in (cached.get("results") or []) + (cached.get("discovered_results") or [])
    }

    risk = compute_portfolio_risk_summary(enriched)  # per-natywna-waluta, bez zmian

    # Widok łączny - konwersja na base_currency NAPRAWIA też stary bug:
    # ekspozycja sektorowa wcześniej sumowała market_value z różnych walut
    # BEZ przeliczenia (np. 100 USD + 100 PLN liczone jako "200" tej samej
    # jednostki) - teraz liczona na przeliczonych, spójnych kwotach.
    combined_positions, skipped = _build_combined_portfolio_view(enriched, base_currency)
    combined_value = sum(p.get("market_value") or 0.0 for p in combined_positions)
    combined_cost = sum(p.get("cost_basis") or 0.0 for p in combined_positions)
    combined_pl_pct = ((combined_value - combined_cost) / combined_cost * 100) if combined_cost > 0 else None
    combined_risk = compute_portfolio_risk_summary(combined_positions)
    combined_risk_bucket = combined_risk.get("by_currency", {}).get(base_currency, {})
    sector_exposure = compute_portfolio_sector_exposure(combined_positions, fundamentals_by_ticker)

    return sanitize_for_json({
        "risk": risk,
        "sector_exposure": sector_exposure,
        "combined": {
            "base_currency": base_currency,
            "value": round(combined_value, 2),
            "cost": round(combined_cost, 2),
            "pl_pct": round(combined_pl_pct, 2) if combined_pl_pct is not None else None,
            "risk_amount": combined_risk_bucket.get("risk_amount"),
            "risk_pct": combined_risk_bucket.get("risk_pct"),
            "skipped_currencies": sorted(skipped),
        },
    })


@app.get("/api/portfolio/equity-currencies")
async def api_get_equity_currencies():
    return db.get_equity_currencies()


@app.get("/api/portfolio/equity/combined")
async def api_get_portfolio_equity_combined():
    base_currency = _cfg.get("portfolio", {}).get("base_currency", "PLN")
    points = db.get_portfolio_equity_combined_curve(base_currency)
    drawdown = compute_max_drawdown(points)
    return sanitize_for_json({"points": points, "drawdown": drawdown, "base_currency": base_currency})


@app.get("/api/portfolio/equity/{currency}")
async def api_get_portfolio_equity(currency: str):
    points = db.get_portfolio_equity_curve(currency)
    drawdown = compute_max_drawdown(points)
    return sanitize_for_json({"points": points, "drawdown": drawdown})


@app.get("/api/portfolio/closed")
async def api_get_closed_portfolio():
    positions = db.get_portfolio(status="closed")
    for p in positions:
        p["realized_pl"] = round((p["sell_price"] - p["buy_price"]) * p["shares"], 2)
        p["realized_pl_pct"] = round((p["sell_price"] - p["buy_price"]) / p["buy_price"] * 100, 2)
        try:
            buy_d = datetime.strptime(p["buy_date"], "%Y-%m-%d")
            sell_d = datetime.strptime(p["sell_date"], "%Y-%m-%d")
            p["holding_days"] = (sell_d - buy_d).days
        except (ValueError, TypeError):
            p["holding_days"] = None
    return sanitize_for_json({"positions": positions, "summary": db.get_closed_summary()})

@app.get("/api/portfolio/tax-summary")
async def api_get_tax_summary():
    closed = db.get_portfolio(status="closed")
    base_currency = _cfg.get("portfolio", {}).get("base_currency", "PLN")
    # Wywołania kursów historycznych (yfinance) mogą chwilę potrwać przy
    # wielu transakcjach - w osobnym wątku, żeby nie blokować pętli asyncio.
    summary = await asyncio.to_thread(compute_tax_summary, closed, base_currency)
    return sanitize_for_json(summary)

@app.post("/api/portfolio/{position_id}/close")
async def api_close_position(position_id: int, req: ClosePositionRequest):
    if req.sell_price <= 0:
        raise HTTPException(status_code=400, detail="Nieprawidłowa cena sprzedaży.")
    ok = db.close_position(position_id, req.sell_price, req.sell_date)
    if not ok:
        raise HTTPException(status_code=404, detail="Nie znaleziono pozycji.")
    return {"status": "ok"}


@app.post("/api/portfolio/{position_id}/reopen")
async def api_reopen_position(position_id: int):
    ok = db.reopen_position(position_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Nie znaleziono pozycji.")
    return {"status": "ok"}


@app.post("/api/portfolio")
async def api_add_position(req: AddPositionRequest):
    ticker = req.ticker.strip().upper()
    if not ticker or req.shares <= 0 or req.buy_price <= 0:
        raise HTTPException(status_code=400, detail="Nieprawidłowe dane pozycji.")
    # Jeśli spółki nie ma jeszcze na watchliście, dodajemy ją automatycznie,
    # żeby zaczęła być analizowana w kolejnych cyklach (inaczej nigdy nie
    # dostaniemy aktualnej ceny/sygnału do oceny tej pozycji).
    db.add_company(ticker, name=ticker, source="portfolio")
    position_id = db.add_position(ticker, req.shares, req.buy_price, req.buy_date, req.notes)

    # Ustalamy walutę od razu, jeśli mamy ją w cache'u z ostatniej analizy -
    # inaczej pozycja domyślnie pokazuje USD do najbliższego cyklu.
    cached = db.load_results_cache() or {}
    match = next((r for r in (cached.get("results") or []) + (cached.get("discovered_results") or [])
                  if r["ticker"] == ticker), None)
    if match:
        currency = match["technical"]["metrics"].get("currency")
        if currency:
            db.set_position_currency(position_id, currency)

    return {"status": "ok", "id": position_id}


@app.delete("/api/portfolio/{position_id}")
async def api_remove_position(position_id: int):
    ok = db.remove_position(position_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Nie znaleziono pozycji.")
    return {"status": "ok"}


@app.put("/api/portfolio/{position_id}")
async def api_update_position(position_id: int, req: AddPositionRequest):
    ticker = req.ticker.strip().upper()
    if not ticker or req.shares <= 0 or req.buy_price <= 0:
        raise HTTPException(status_code=400, detail="Nieprawidłowe dane pozycji.")
    db.add_company(ticker, name=ticker, source="portfolio")
    ok = db.update_position(position_id, ticker, req.shares, req.buy_price, req.buy_date, req.notes)
    if not ok:
        raise HTTPException(status_code=404, detail="Nie znaleziono pozycji.")
    return {"status": "ok"}


@app.get("/api/logs")
async def api_get_logs(limit: int = 100):
    return db.get_recent_logs(limit=limit)

@app.post("/api/chat")
async def api_chat(req: ChatRequest):
    cached = db.load_results_cache() or {}
    enriched_positions = _get_enriched_open_positions(cached)
    portfolio_summary = summarize_portfolio_by_currency(enriched_positions)
    reply = await asyncio.to_thread(
        answer_chat_question, req.messages, cached, portfolio_summary, _cfg["llm"]
    )
    return {"reply": reply}

@app.get("/api/score-history/{ticker}")
async def api_get_score_history(ticker: str, limit: int = 400):
    """Historia werdyktów (kategoria/wynik/sygnał) dla spółki w czasie -
    używana do rysowania znaczników na wykresie ceny."""
    return db.get_score_history(ticker, limit=limit)

@app.get("/api/effectiveness")
async def api_get_effectiveness():
    """Statystyki skuteczności narzędzia - porównanie przeszłych werdyktów
    z późniejszą ceną tej samej spółki (patrz db.get_effectiveness_stats)."""
    min_age_days = _cfg.get("effectiveness", {}).get("min_signal_age_days", 14)
    return db.get_effectiveness_stats(min_age_days=min_age_days)


@app.get("/api/status")
async def api_get_status():
    return {"is_running": _is_running, "next_run_at": _next_run_at,
             "refresh_interval_minutes": _cfg.get("web", {}).get("refresh_interval_minutes", 60)}


@app.post("/api/run-now")
async def api_run_now():
    asyncio.create_task(trigger_analysis_now())
    return {"status": "started"}

@app.post("/api/refresh/{ticker}")
async def api_refresh_ticker(ticker: str):
    """Odświeża dane JEDNEJ spółki na żądanie (np. przy otwarciu jej karty
    na dashboardzie), zamiast czekać na cały cykl analizy watchlisty.
    Chronione cooldownem, żeby nie bombardować yfinance/Finnhub przy
    wielokrotnym otwieraniu tej samej spółki w krótkim czasie."""
    ticker_upper = ticker.upper()
    now = datetime.now().timestamp()
    cooldown = _cfg.get("web", {}).get("manual_refresh_cooldown_seconds", 45)
    last = _last_manual_refresh.get(ticker_upper)
    if last is not None and (now - last) < cooldown:
        wait = round(cooldown - (now - last))
        raise HTTPException(status_code=429,
                             detail=f"Poczekaj jeszcze {wait}s przed kolejnym odświeżeniem {ticker_upper}.")
    _last_manual_refresh[ticker_upper] = now

    try:
        result = await asyncio.to_thread(_refresh_single_ticker_blocking, ticker_upper)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Błąd odświeżania {ticker_upper}: {exc}")

    await manager.broadcast({"type": "ticker_updated", "result": result})
    return result


@app.get("/api/chart/{ticker}")
async def api_get_chart(ticker: str, period: str = "1y"):
    """Dane OHLC do wykresu świecowego (TradingView Lightweight Charts)."""
    try:
        history = await asyncio.to_thread(fetch_history, ticker, period, "1d")
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=404, detail=f"Nie udało się pobrać danych dla {ticker}: {exc}")

    candles = [
        {
            "time": idx.strftime("%Y-%m-%d"),
            "open": round(float(row["Open"]), 4),
            "high": round(float(row["High"]), 4),
            "low": round(float(row["Low"]), 4),
            "close": round(float(row["Close"]), 4),
        }
        for idx, row in history.iterrows()
    ]
    ma50 = history["Close"].rolling(50).mean()
    ma200 = history["Close"].rolling(200).mean()
    ma50_series = [
        {"time": idx.strftime("%Y-%m-%d"), "value": round(float(v), 4)}
        for idx, v in ma50.items() if v == v  # odfiltruj NaN
    ]
    ma200_series = [
        {"time": idx.strftime("%Y-%m-%d"), "value": round(float(v), 4)}
        for idx, v in ma200.items() if v == v
    ]

    return sanitize_for_json(
    {"ticker": ticker.upper(), "candles": candles, "ma50": ma50_series, "ma200": ma200_series}
)


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await manager.connect(ws)
    try:
        while True:
            # nie oczekujemy nic konkretnego od klienta - trzymamy połączenie żywe
            await ws.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(ws)


# Statyczny frontend (musi być zamontowany PO endpointach /api i /ws)
app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")


def main():
    global _cfg
    parser = argparse.ArgumentParser(description="XTB Trend Watch - dashboard webowy")
    parser.add_argument("--config", default="config.yaml", help="Ścieżka do pliku konfiguracyjnego")
    parser.add_argument("--host", default=None, help="Nadpisuje web.host z configu")
    parser.add_argument("--port", type=int, default=None, help="Nadpisuje web.port z configu")
    args = parser.parse_args()

    with open(args.config, "r", encoding="utf-8") as f:
        _cfg = yaml.safe_load(f)

    web_cfg = _cfg.get("web", {})
    host = args.host or web_cfg.get("host", "127.0.0.1")
    port = args.port or web_cfg.get("port", 8000)

    import uvicorn
    print(f"\n>>> Dashboard dostępny pod adresem: http://{host}:{port}\n")
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()

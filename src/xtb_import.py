"""
xtb_import.py
---------------
Import raportu XTB (.xlsx, arkusze "Open Positions" / "Closed Positions")
do portfela dashboardu. Bezpieczny i idempotentny: każda zaimportowana
pozycja dostaje znacznik [XTB:<id_pozycji>] w notatce - ponowny import
tego samego (lub nowszego) pliku NIGDY nie tworzy duplikatów, a istniejące
dane wpisane ręcznie w dashboardzie NIE są w żaden sposób ruszane.
"""

from __future__ import annotations

import io
import logging
from datetime import datetime

import openpyxl

logger = logging.getLogger("xtb_trend_watch.xtb_import")

# Sufiksy XTB -> sufiksy Yahoo Finance. Nie jest to lista wyczerpująca dla
# wszystkich giełd świata - dla nierozpoznanego sufiksu ticker zostaje BEZ
# ZMIAN i trafia do listy "do ręcznej weryfikacji" w wyniku importu (można
# go potem poprawić przyciskiem ✎ w portfelu, dokładnie jak każdą inną pozycję).
XTB_SUFFIX_TO_YAHOO = {
    "US": "", "PL": "WA", "DE": "DE", "UK": "L", "FR": "PA", "NL": "AS",
    "ES": "MC", "IT": "MI", "PT": "LS", "BE": "BR", "AT": "VI", "CH": "SW",
    "SE": "ST", "NO": "OL", "DK": "CO", "FI": "HE", "CA": "TO", "JP": "T",
    "HK": "HK", "AU": "AX",
}

# Zgrubne domyślne przypisanie waluty wg sufiksu XTB - tylko jako punkt
# startowy (dokładna waluta i tak zostanie ustalona/skorygowana przez
# yfinance przy pierwszym pełnym cyklu analizy tej spółki).
XTB_SUFFIX_TO_CURRENCY = {
    "US": "USD", "PL": "PLN", "DE": "EUR", "FR": "EUR", "NL": "EUR",
    "ES": "EUR", "IT": "EUR", "PT": "EUR", "BE": "EUR", "AT": "EUR",
    "FI": "EUR", "UK": "GBP", "CH": "CHF", "SE": "SEK", "NO": "NOK",
    "DK": "DKK", "CA": "CAD", "JP": "JPY", "HK": "HKD", "AU": "AUD",
}


def map_xtb_ticker(xtb_ticker: str) -> tuple[str, bool]:
    """Zwraca (ticker_yahoo, czy_pewne_mapowanie). Przy nierozpoznanym
    sufiksie zwraca oryginalny ticker bez zmian i False - do oznaczenia
    jako 'wymaga weryfikacji' w wyniku importu."""
    if "." not in xtb_ticker:
        return xtb_ticker, False
    base, suffix = xtb_ticker.rsplit(".", 1)
    if suffix not in XTB_SUFFIX_TO_YAHOO:
        return xtb_ticker, False
    mapped_suffix = XTB_SUFFIX_TO_YAHOO[suffix]
    return (base if not mapped_suffix else f"{base}.{mapped_suffix}"), True


def guess_currency(xtb_ticker: str) -> str:
    if "." in xtb_ticker:
        suffix = xtb_ticker.rsplit(".", 1)[1]
        return XTB_SUFFIX_TO_CURRENCY.get(suffix, "USD")
    return "USD"


def _find_header(ws, required_columns: list[str]) -> tuple[int, dict[str, int]] | tuple[None, None]:
    """Szuka wiersza nagłówka po nazwach kolumn (nie po numerze wiersza -
    odporne na zmiany liczby wierszy podsumowania nad tabelą w różnych
    wariantach raportu XTB)."""
    for row in ws.iter_rows(min_row=1, max_row=25):
        values = [str(c.value).strip() if c.value is not None else "" for c in row]
        if all(col in values for col in required_columns):
            col_index = {name: i for i, name in enumerate(values)}
            return row[0].row, col_index
    return None, None


def _to_date_str(value) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d")
    return None


def parse_xtb_report(file_bytes: bytes) -> dict:
    """Zwraca {"open": [...], "closed": [...], "warnings": [...]}. Każdy
    element open/closed to dict gotowy do wstawienia do bazy portfela."""
    wb = openpyxl.load_workbook(io.BytesIO(file_bytes), data_only=True)
    warnings: list[str] = []
    open_rows: list[dict] = []
    closed_rows: list[dict] = []

    # --- Open Positions: tylko wiersze POJEDYNCZYCH transakcji (Type=BUY,
    # z wypełnioną datą otwarcia) - wiersze zagregowane (bez daty) pomijamy.
    if "Open Positions" in wb.sheetnames:
        ws = wb["Open Positions"]
        header_row, cols = _find_header(ws, ["Ticker", "Volume", "Open price", "Open time (UTC)"])
        if header_row:
            for row in ws.iter_rows(min_row=header_row + 1, values_only=True):
                if row[cols["Ticker"]] is None:
                    continue
                position_id = row[cols.get("Instrument/Position", -1)] if "Instrument/Position" in cols else None
                open_time = row[cols["Open time (UTC)"]]
                type_ = row[cols.get("Type", -1)] if "Type" in cols else None
                if type_ != "BUY" or not open_time:
                    continue  # wiersz zagregowany albo krótka pozycja - pomijamy

                xtb_ticker = str(row[cols["Ticker"]]).strip()
                volume = row[cols["Volume"]]
                open_price = row[cols["Open price"]]
                if not volume or not open_price:
                    continue

                yahoo_ticker, confident = map_xtb_ticker(xtb_ticker)
                if not confident:
                    warnings.append(f"Nierozpoznany sufiks giełdy dla {xtb_ticker} - "
                                     f"zaimportowano jako '{yahoo_ticker}', ZWERYFIKUJ ręcznie.")

                open_rows.append({
                    "xtb_id": str(int(position_id)) if isinstance(position_id, float) else str(position_id),
                    "ticker": yahoo_ticker,
                    "original_ticker": xtb_ticker,
                    "shares": round(float(volume), 6),
                    "buy_price": round(float(open_price), 6),
                    "buy_date": _to_date_str(open_time) or datetime.now().strftime("%Y-%m-%d"),
                    "currency": guess_currency(xtb_ticker),
                })

    # --- Closed Positions: każdy wiersz to już pojedyncza, zamknięta transakcja.
    if "Closed Positions" in wb.sheetnames:
        ws = wb["Closed Positions"]
        header_row, cols = _find_header(ws, ["Ticker", "Open Price", "Close Price", "Position ID"])
        if header_row:
            for row in ws.iter_rows(min_row=header_row + 1, values_only=True):
                if row[cols["Ticker"]] is None:
                    continue
                type_ = row[cols.get("Type", -1)] if "Type" in cols else "BUY"
                if type_ != "BUY":
                    warnings.append(f"Pominięto pozycję krótką (SELL) dla {row[cols['Ticker']]} - "
                                     f"nieobsługiwane w tym portfelu (tylko długie pozycje).")
                    continue

                xtb_ticker = str(row[cols["Ticker"]]).strip()
                volume = row[cols.get("Volume")]
                open_price = row[cols["Open Price"]]
                close_price = row[cols["Close Price"]]
                if not volume or not open_price or not close_price:
                    continue

                yahoo_ticker, confident = map_xtb_ticker(xtb_ticker)
                if not confident:
                    warnings.append(f"Nierozpoznany sufiks giełdy dla {xtb_ticker} - "
                                     f"zaimportowano jako '{yahoo_ticker}', ZWERYFIKUJ ręcznie.")

                position_id = row[cols["Position ID"]]
                closed_rows.append({
                    "xtb_id": str(int(position_id)) if isinstance(position_id, float) else str(position_id),
                    "ticker": yahoo_ticker,
                    "original_ticker": xtb_ticker,
                    "shares": round(float(volume), 6),
                    "buy_price": round(float(open_price), 6),
                    "buy_date": _to_date_str(row[cols["Open Time (UTC)"]]) or "",
                    "sell_price": round(float(close_price), 6),
                    "sell_date": _to_date_str(row[cols["Close Time (UTC)"]]) or "",
                    "currency": guess_currency(xtb_ticker),
                })

    return {"open": open_rows, "closed": closed_rows, "warnings": warnings}
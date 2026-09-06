"""
json_utils.py
----------------
Pomocnicza sanityzacja danych przed serializacją JSON.

Problem, który to rozwiązuje: `yfinance` (szczególnie `Ticker.info` używane
w fundamentals.py) czasem zwraca `float('nan')` zamiast `None` dla pustych
pól (np. brak danych o zadłużeniu dla danej spółki). Python's `json.dumps`
domyślnie akceptuje NaN/Infinity (zapisując je jako niestandardowe literały
`NaN`/`Infinity`), ale to NIE jest poprawny JSON wg specyfikacji - FastAPI
(przez Starlette) świadomie to blokuje (`allow_nan=False`), więc każda
odpowiedź API zawierająca taką wartość kończy się błędem 500. Przeglądarka
(JSON.parse) też odrzuci taki literał jako błąd składni.

Rozwiązanie: rekurencyjnie przechodzimy przez strukturę danych i zamieniamy
NaN/Infinity na `None` (czyli `null` w JSON) - tracimy tę pojedynczą liczbę,
ale reszta wyniku pozostaje poprawna i w ogóle dociera do przeglądarki.
"""

from __future__ import annotations

import math


def sanitize_for_json(obj):
    """Rekurencyjnie zamienia float('nan')/float('inf')/-inf na None, żeby
    struktura była bezpieczna do zserializowania przez standardowy JSON."""
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
        return obj
    if isinstance(obj, dict):
        return {k: sanitize_for_json(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [sanitize_for_json(v) for v in obj]
    return obj

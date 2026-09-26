"""
notifications.py
-----------------
Zewnętrzne powiadomienia o alertach cenowych (na razie: Discord webhook) -
uzupełnienie tego, co już jest w dashboardzie (log na żywo, odznaka na
szufladzie), o coś, co dotrze do Ciebie nawet gdy karta przeglądarki jest
zamknięta. Świadomie prosty kanał: webhook Discorda to jeden URL, bez bota
i bez logowania się z poziomu tej aplikacji na żadne konto.
"""

from __future__ import annotations

import logging

import requests

logger = logging.getLogger("xtb_trend_watch.notifications")

# Te same kolory co reszta UI (patrz web/static/app.js - linie stop/cel na
# wykresie), żeby powiadomienie na Discordzie "wyglądało" tak samo jak alert
# w aplikacji.
_ALERT_COLORS = {
    "stop_loss": 0xDA6A52,
    "custom_target": 0x56E1E3,
    "target_reached": 0x6DBE85,
}
_DEFAULT_COLOR = 0xC9A227

_TIMEOUT_SECONDS = 5


def send_discord_alert(title: str, body: str, alert_type: str, webhook_url: str) -> bool:
    """Wysyła pojedynczy alert jako embed na Discorda. Zwraca True/False
    (sukces) i NIGDY nie rzuca wyjątku dalej - zła konfiguracja webhooka albo
    chwilowa niedostępność Discorda nie może przerywać właściwej pętli
    alertów cenowych (patrz web_app._check_price_alerts_blocking)."""
    if not webhook_url:
        return False

    payload = {
        "embeds": [{
            "title": title,
            "description": body,
            "color": _ALERT_COLORS.get(alert_type, _DEFAULT_COLOR),
        }],
    }
    try:
        resp = requests.post(webhook_url, json=payload, timeout=_TIMEOUT_SECONDS)
        resp.raise_for_status()
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("Nie udało się wysłać powiadomienia Discord: %s", exc)
        return False


def notify_price_alerts(alerts: list[dict], notifications_cfg: dict) -> None:
    """Wysyła KAŻDY z podanych alertów (patrz web_app._check_price_alerts_blocking
    - to już przefiltrowana lista NOWYCH alertów, z tą samą deduplikacją co
    log/WebSocket) na skonfigurowane kanały zewnętrzne. Cicho nic nie robi,
    jeśli powiadomienia są wyłączone albo brak skonfigurowanego kanału."""
    if not notifications_cfg.get("enabled") or not alerts:
        return

    webhook_url = notifications_cfg.get("discord_webhook_url")
    if webhook_url:
        for alert in alerts:
            send_discord_alert(alert["title"], alert["body"], alert["type"], webhook_url)

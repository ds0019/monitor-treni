"""Invio delle notifiche sul telefono: ntfy e/o Telegram.

I dati di accesso arrivano da variabili d'ambiente (su GitHub: i secret),
mai da config.yaml, perche' la repo e' pubblica:
  NTFY_TOPIC                       argomento ntfy a cui sei iscritto
  NTFY_SERVER                      facoltativo, predefinito https://ntfy.sh
  TELEGRAM_TOKEN, TELEGRAM_CHAT_ID facoltativi, per ricevere gli avvisi su Telegram
"""
from __future__ import annotations

import logging
import os

import requests

log = logging.getLogger("notifiche")


class Notificatore:
    def __init__(self, link: str, priorita: int = 5):
        self.link = link
        self.priorita = priorita
        self.ntfy_server = os.environ.get("NTFY_SERVER") or "https://ntfy.sh"
        self.ntfy_topic = os.environ.get("NTFY_TOPIC", "")
        self.telegram_token = os.environ.get("TELEGRAM_TOKEN", "")
        self.telegram_chat = os.environ.get("TELEGRAM_CHAT_ID", "")

    @property
    def configurato(self) -> bool:
        return bool(self.ntfy_topic or (self.telegram_token and self.telegram_chat))

    def invia(self, titolo: str, testo: str, urgente: bool = True) -> bool:
        """Manda la notifica su tutti i canali configurati. True se almeno uno ha funzionato."""
        log.info("NOTIFICA >> %s | %s", titolo, testo.replace("\n", " | "))
        inviata = False
        if self.ntfy_topic:
            inviata |= self._ntfy(titolo, testo, urgente)
        if self.telegram_token and self.telegram_chat:
            inviata |= self._telegram(titolo, testo)
        return inviata

    def _ntfy(self, titolo: str, testo: str, urgente: bool) -> bool:
        try:
            r = requests.post(self.ntfy_server.rstrip("/") + "/", timeout=15, json={
                "topic": self.ntfy_topic,
                "title": titolo,
                "message": testo,
                "priority": self.priorita if urgente else 3,
                "tags": ["train"],
                "click": self.link,
            })
            r.raise_for_status()
            return True
        except Exception as e:
            log.warning("ntfy non raggiungibile: %s", e)
            return False

    def _telegram(self, titolo: str, testo: str) -> bool:
        try:
            r = requests.post(f"https://api.telegram.org/bot{self.telegram_token}/sendMessage", timeout=15,
                              json={"chat_id": self.telegram_chat, "text": f"{titolo}\n{testo}",
                                    "disable_web_page_preview": True})
            r.raise_for_status()
            return True
        except Exception as e:
            log.warning("Telegram non raggiungibile: %s", e)
            return False

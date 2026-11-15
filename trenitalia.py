"""Ricerca dei treni sul sito lefrecce.it (API non ufficiali di Trenitalia).

Due modalita':
  pubblica  la ricerca che fa chiunque sul sito: posti in vendita su quel treno
  carnet    ripete la ricerca fatta dalla pagina "Prenota con carnet", copiata
            dal browser come cURL in un file (vedi LEGGIMI.md)
"""
from __future__ import annotations

import copy
import json
import logging
import re
import shlex
from dataclasses import dataclass
from pathlib import Path

import requests

BFF = "https://www.lefrecce.it/Channels.Website.BFF.WEB/website"
HEADERS_BROWSER = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "it-IT,it;q=0.9,en;q=0.8",
    "Content-Type": "application/json",
    "Origin": "https://www.lefrecce.it",
    "Referer": "https://www.lefrecce.it/",
}

log = logging.getLogger("trenitalia")


class AccessoNegato(Exception):
    """Il sito ha risposto 401/403: richiesta scaduta o bloccata."""


# ------------------------------- risultati ----------------------------------
@dataclass
class Servizio:
    nome: str          # es. "Standard"
    disponibile: bool
    posti: int         # 0 se il sito non dice quanti
    offerte: list[str]


@dataclass
class Treno:
    numero: str
    stato: str | None
    servizi: list[Servizio]
    disponibile: bool

    def dettagli(self) -> str:
        if not self.servizi:
            return "posti in vendita" if self.disponibile else "esaurito"
        return "; ".join(f"{s.nome}: {s.posti or ('si' if s.disponibile else 0)}" for s in self.servizi)


# ---------------------------- client del sito -------------------------------
class Trenitalia:
    def __init__(self, classi_ammesse: list[str], classi_escluse: list[str],
                 file_carnet: Path | None = None):
        self.ammesse = [c.lower() for c in classi_ammesse]
        self.escluse = [c.lower() for c in classi_escluse]
        self.sessione = requests.Session()
        self.sessione.headers.update(HEADERS_BROWSER)
        self.file_carnet = file_carnet
        self.carnet = leggi_curl(file_carnet) if file_carnet else None   # ValueError se non valido

    @property
    def modo(self) -> str:
        return "carnet" if self.file_carnet else "pubblica"

    def ricarica_carnet(self) -> bool:
        """Se il file della richiesta carnet e' cambiato lo ricarica. True se l'ha ricaricato."""
        f = self.file_carnet
        if not f or not f.exists() or f.stat().st_mtime == self.carnet["mtime"]:
            return False
        self.carnet = leggi_curl(f)
        return True

    def id_stazione(self, nome: str, cache: dict) -> int:
        """Codice della stazione sul sito. `cache` evita di cercarla ogni volta."""
        if nome in cache:
            return cache[nome]
        r = self.sessione.get(f"{BFF}/locations/search", params={"name": nome, "limit": 10}, timeout=20)
        if r.status_code in (401, 403):
            raise AccessoNegato(f"HTTP {r.status_code} sulla ricerca stazioni")
        r.raise_for_status()
        risultati = r.json() or []
        scelta = next((s for s in risultati
                       if _norm(str(s.get("displayName") or s.get("name") or "")) == _norm(nome)), None)
        if scelta is None:
            if not risultati:
                raise RuntimeError(f"stazione non trovata: {nome}")
            scelta = risultati[0]
            log.warning("Per '%s' uso '%s'", nome, scelta.get("displayName") or scelta.get("name"))
        cache[nome] = int(scelta["id"])
        return cache[nome]

    def cerca(self, da: int, a: int, quando: str) -> dict:
        """Soluzioni di viaggio dirette da `da` ad `a` a partire da `quando` (ISO con fuso)."""
        if self.carnet:
            corpo = copy.deepcopy(self.carnet["corpo"])
            sostituisci(corpo, {"departureLocationId": da, "arrivalLocationId": a, "departureTime": quando}, set())
            r = requests.request(self.carnet["metodo"], self.carnet["url"], headers=self.carnet["headers"],
                                 json=corpo, timeout=25)
        else:
            r = self.sessione.post(f"{BFF}/ticket/solutions", json=corpo_ricerca(da, a, quando), timeout=25)
        if r.status_code in (401, 403):
            raise AccessoNegato(f"HTTP {r.status_code}")
        r.raise_for_status()
        return r.json()

    def trova_treno(self, dati: dict, ora: str) -> Treno | None:
        return trova_treno(dati, ora, self.ammesse, self.escluse)


# ------------------------- lettura delle risposte ---------------------------
def corpo_ricerca(da: int, a: int, quando: str) -> dict:
    return {
        "departureLocationId": da,
        "arrivalLocationId": a,
        "departureTime": quando,
        "adults": 1,
        "children": 0,
        "criteria": {"frecceOnly": True, "regionalOnly": False, "noChanges": True,
                     "order": "DEPARTURE_DATE", "limit": 10, "offset": 0},
        "advancedSearchRequest": {"bestFare": False},
    }


def trova_treno(dati: dict, ora: str, ammesse: list[str], escluse: list[str]) -> Treno | None:
    """Il Frecciarossa diretto che parte all'ora `ora` (HH:MM), con i posti nelle classi ammesse."""
    if not isinstance(dati, dict):
        return None
    for item in dati.get("solutions") or []:
        sol = item.get("solution") or item
        treni = sol.get("trains") or []
        if str(sol.get("departureTime") or "")[11:16] != ora or len(treni) != 1 or not e_frecciarossa(treni[0]):
            continue
        servizi = analizza_servizi(item, ammesse, escluse)
        stato = sol.get("status")
        disponibile = any(s.disponibile for s in servizi) if servizi else stato == "SALEABLE"
        return Treno(numero=str(treni[0].get("name") or treni[0].get("number") or "?"),
                     stato=stato, servizi=servizi, disponibile=disponibile)
    return None


def analizza_servizi(item: dict, ammesse: list[str], escluse: list[str]) -> list[Servizio]:
    servizi = []
    for griglia in item.get("grids") or []:
        for s in griglia.get("services") or []:
            nome = str(s.get("name") or s.get("groupName") or "?")
            low = nome.lower()
            if not any(c in low for c in ammesse) or any(c in low for c in escluse):
                continue
            posti, disponibile, offerte = 0, False, []
            for o in s.get("offers") or []:
                n = o.get("availableAmount")
                ok = o.get("status") == "SALEABLE" and (n is None or n > 0)
                disponibile = disponibile or ok
                if ok and n:
                    posti = max(posti, n)
                offerte.append(f"{o.get('name', '?')}={o.get('status', '?')}/{'?' if n is None else n}")
            servizi.append(Servizio(nome, disponibile, posti, offerte))
    return servizi


def e_frecciarossa(treno: dict) -> bool:
    testo = " ".join(str(treno.get(k) or "") for k in
                     ("trainCategory", "acronym", "denomination", "description")).upper()
    return not testo.strip() or "FRECCIAROSSA" in testo or re.search(r"\bFR\b", testo) is not None


def partenze_viste(dati: dict) -> list[str]:
    """Orari di partenza presenti nella risposta (per capire perche' un treno non si trova)."""
    if not isinstance(dati, dict):
        return []
    return sorted({str((it.get("solution") or it).get("departureTime") or "")[11:16]
                   for it in (dati.get("solutions") or []) if isinstance(it, dict)})


def _norm(s: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", s.lower()).split())


# ------------------------- richiesta carnet (cURL) --------------------------
def leggi_curl(path: Path) -> dict:
    """Legge un 'Copy as cURL (bash)' preso dagli strumenti per sviluppatori del browser."""
    if not path.exists():
        raise ValueError(f"manca il file {path.name} (vedi LEGGIMI.md, modalita' carnet)")
    testo = path.read_text(encoding="utf-8").strip()
    if "^" in testo and "\\\n" not in testo:
        raise ValueError("sembra un cURL in formato Windows (cmd): copia 'Copy as cURL (bash)'")
    try:
        tok = shlex.split(re.sub(r"\\\r?\n", " ", testo))
    except ValueError as e:
        raise ValueError(f"non riesco a leggere il cURL: {e}")
    if not tok or tok[0] != "curl":
        raise ValueError("il file deve contenere un comando che inizia con 'curl'")

    url = metodo = dati = None
    headers: dict[str, str] = {}
    con_valore = {"-H": "header", "--header": "header", "-b": "Cookie", "--cookie": "Cookie",
                  "-A": "User-Agent", "--user-agent": "User-Agent", "-e": "Referer", "--referer": "Referer",
                  "-X": "metodo", "--request": "metodo", "--url": "url",
                  "-d": "dati", "--data": "dati", "--data-raw": "dati", "--data-binary": "dati",
                  "--data-ascii": "dati"}
    i = 1
    while i < len(tok):
        t = tok[i]
        if t in con_valore and i + 1 < len(tok):
            campo, valore = con_valore[t], tok[i + 1]
            if campo == "header":
                k, _, v = valore.partition(":")
                headers[k.strip()] = v.strip()
            elif campo == "metodo":
                metodo = valore.upper()
            elif campo == "url":
                url = valore
            elif campo == "dati":
                dati = valore
            else:
                headers[campo] = valore
            i += 2
        elif t.startswith("-"):
            i += 1  # --compressed, -s, ecc.
        else:
            url = url or t
            i += 1

    if not url or not dati:
        raise ValueError("nel cURL mancano l'indirizzo o il corpo della richiesta (--data-raw)")
    try:
        corpo = json.loads(dati[1:] if dati.startswith("$") else dati)
    except json.JSONDecodeError:
        raise ValueError("il corpo della richiesta non e' JSON: hai copiato la richiesta giusta?")

    via = {"content-length", "accept-encoding", "host"}
    headers = {k: v for k, v in headers.items() if k.lower() not in via and not k.startswith(":")}
    trovate: set[str] = set()
    sostituisci(copy.deepcopy(corpo), {"departureTime": ""}, trovate)
    if "departureTime" not in trovate:
        raise ValueError("nella richiesta non c'e' 'departureTime': non sembra la ricerca dei treni")
    if "solutions" not in url:
        log.warning("L'indirizzo copiato non contiene 'solutions' (%s); provo lo stesso.", url)
    return {"url": url, "metodo": metodo or "POST", "headers": headers, "corpo": corpo,
            "mtime": path.stat().st_mtime}


def sostituisci(obj, valori: dict, trovate: set) -> None:
    """Sostituisce, a qualsiasi profondita', le chiavi di `valori` presenti in `obj`."""
    if isinstance(obj, dict):
        for k in list(obj):
            if k in valori:
                obj[k] = valori[k]
                trovate.add(k)
            else:
                sostituisci(obj[k], valori, trovate)
    elif isinstance(obj, list):
        for x in obj:
            sostituisci(x, valori, trovate)

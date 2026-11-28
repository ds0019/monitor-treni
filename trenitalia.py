"""Ricerca dei treni sul sito lefrecce.it (API non ufficiali di Trenitalia).

Due modalita':
  pubblica  la ricerca che fa chiunque sul sito: posti IN VENDITA su quel treno
            (non dice se col carnet si puo' prenotare: il carnet ha una quota sua)
  carnet    gli stessi passaggi della pagina "Prenota con carnet" del sito:
              1. carnet/verify              numero, nome e cognome -> codice del viaggio (travelId)
              2. carnet/solutions/bookings  treni prenotabili con QUEL carnet
            Per ogni treno il sito risponde SALEABLE (prenotabile) o con un messaggio
            tipo "Posti Esauriti sul treno ...".
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import re
import time
from dataclasses import dataclass, field

import requests

BFF = "https://www.lefrecce.it/Channels.Website.BFF.WEB/website"
HEADERS_BROWSER = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "it-IT,it;q=0.9,en;q=0.8",
    "Content-Type": "application/json",
    "X-Requested-With": "Fetch",
    "Origin": "https://www.lefrecce.it",
    "Referer": "https://www.lefrecce.it/",
}

log = logging.getLogger("trenitalia")


class AccessoNegato(Exception):
    """Il sito ha risposto 401/403: richiesta rifiutata (di solito il sistema anti-bot)."""


class CarnetNonValido(Exception):
    """Il sito non accetta il carnet: numero, nome o cognome sbagliati, oppure carnet scaduto."""


@dataclass
class Carnet:
    numero: str
    nome: str
    cognome: str


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
    messaggi: list[str] = field(default_factory=list)   # es. "Posti Esauriti sul treno 9391..."

    def dettagli(self) -> str:
        if self.servizi:
            return "; ".join(f"{s.nome}: {s.posti or ('si' if s.disponibile else 0)}" for s in self.servizi)
        if self.messaggi:
            return " ".join(self.messaggi)
        return "prenotabile" if self.disponibile else "esaurito"


# ---------------------------- client del sito -------------------------------
class Trenitalia:
    def __init__(self, classi_ammesse: list[str], classi_escluse: list[str], carnet: Carnet | None = None):
        self.ammesse = [c.lower() for c in classi_ammesse]
        self.escluse = [c.lower() for c in classi_escluse]
        self.sessione = requests.Session()
        self.sessione.headers.update(HEADERS_BROWSER)
        self.carnet = carnet
        self.usa_carnet = carnet is not None     # spento quando il sito blocca la parte carnet
        self.travel_id: str | None = None
        self.rimasti: int | None = None          # viaggi rimasti sul carnet
        self.regionale = False
        self.stazioni_carnet: dict[str, int] = {}
        self.cart_id: str | None = None
        self._sessione_pronta = False

    @property
    def modo(self) -> str:
        return "carnet" if self.carnet and self.usa_carnet else "pubblica"

    # --- sessione e carnet ---
    def _prepara_sessione(self) -> None:
        """Come fa il sito quando si apre: token anti-falsificazione e canale. Se non riesce, si prova lo stesso."""
        if self._sessione_pronta:
            return
        self._sessione_pronta = True
        try:
            r = self.sessione.post(f"{BFF}/whitelist/enabled", data="{}", timeout=20)
            token = (r.json() or {}).get("token") if r.ok else None
            if token:
                self.sessione.headers["X-CSRF-Token"] = str(token)
            r = self.sessione.get(f"{BFF}/channel/default", timeout=20)
            canale = (r.json() or {}).get("id") if r.ok else None
            if canale is not None:
                self.sessione.headers["Channel"] = str(canale)
        except Exception as e:
            log.warning("Preparazione della sessione non riuscita (%s): provo lo stesso.", e)

    def verifica_carnet(self) -> int | None:
        """Controlla il carnet sul sito (come il pulsante CONFERMA). Restituisce i viaggi rimasti."""
        self._prepara_sessione()
        c = self.carnet
        dati = self._post("carnet/verify", {"carnetNumber": c.numero, "firstName": c.nome, "lastName": c.cognome})
        if not dati.get("travelId"):
            raise CarnetNonValido(f"il sito non ha riconosciuto il carnet ({_riassunto(dati)})")
        self.travel_id = str(dati["travelId"])
        massimo, usati = dati.get("maxUsageNumbers"), dati.get("currentUsageNumbers")
        self.rimasti = massimo - usati if isinstance(massimo, int) and isinstance(usati, int) else None
        self.regionale = bool(dati.get("regionalCarnetType"))
        self.stazioni_carnet = {}
        for loc in (dati.get("startLocations") or []) + (dati.get("endLocations") or []) + \
                [dati.get("startLocation"), dati.get("endLocation")]:
            if isinstance(loc, dict) and loc.get("id") is not None:
                for k in ("displayName", "name"):
                    if loc.get(k):
                        self.stazioni_carnet[_norm(str(loc[k]))] = int(loc["id"])
        return self.rimasti

    def _post(self, percorso: str, corpo: dict) -> dict:
        self.sessione.headers["CallerTimestamp"] = str(int(time.time() * 1000))
        r = self.sessione.post(f"{BFF}/{percorso}", json=corpo, timeout=25)
        if r.status_code in (401, 403):
            raise AccessoNegato(f"HTTP {r.status_code} su {percorso}: {_messaggio(r)}")
        if r.status_code >= 400:
            if percorso == "carnet/verify":
                raise CarnetNonValido(_messaggio(r))
            raise RuntimeError(f"HTTP {r.status_code} su {percorso}: {_messaggio(r)}")
        return r.json() or {}

    # --- stazioni e ricerca ---
    def id_stazione(self, nome: str, cache: dict) -> int:
        """Codice della stazione sul sito. `cache` evita di cercarla ogni volta."""
        if self.usa_carnet and _norm(nome) in self.stazioni_carnet:   # stazioni del carnet, se in uso
            return self.stazioni_carnet[_norm(nome)]
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

    def cerca(self, da: int, a: int, partenza: dt.datetime) -> dict:
        """Soluzioni da `da` ad `a` intorno all'orario `partenza` (datetime con fuso)."""
        if self.carnet and self.usa_carnet:
            return self._cerca_carnet(da, a, partenza)
        quando = (partenza - dt.timedelta(minutes=30)).isoformat(timespec="milliseconds")
        r = self.sessione.post(f"{BFF}/ticket/solutions", json=corpo_ricerca(da, a, quando), timeout=25)
        if r.status_code in (401, 403):
            raise AccessoNegato(f"HTTP {r.status_code}")
        r.raise_for_status()
        return r.json()

    def _cerca_carnet(self, da: int, a: int, partenza: dt.datetime) -> dict:
        if self.travel_id is None:
            self.verifica_carnet()
        ora = partenza.strftime("%H:%M")
        corpo = {
            "travelId": self.travel_id,
            # come il sito: ora italiana senza fuso; parto 10 minuti prima (5 risultati a pagina)
            "departureDateTime": (partenza - dt.timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%S.000"),
            "departureLocationId": da,
            "arrivalLocationId": a,
            "criteria": {"limit": 5, "order": "DEPARTURE_DATE", "frecceOnly": False, "intercityOnly": False,
                         "regionalOnly": self.regionale, "noChanges": False, "offset": 0},
        }
        soluzioni: list = []
        for pagina in range(3):                      # al massimo 15 soluzioni
            richiesta = {**corpo, "criteria": {**corpo["criteria"], "offset": 5 * pagina}}
            if self.cart_id:
                richiesta["cartId"] = self.cart_id
            dati = self._post("carnet/solutions/bookings", richiesta)
            self.cart_id = dati.get("cartId") or self.cart_id
            nuove = [s for s in (dati.get("solutions") or []) if isinstance(s, dict)]
            soluzioni += nuove
            orari = [str((s.get("solution") or s).get("departureTime") or "")[11:16] for s in nuove]
            if len(nuove) < 5 or ora in orari or max(orari) > ora:
                break
        return {"solutions": soluzioni}

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
    """Il Frecciarossa diretto che parte all'ora `ora` (HH:MM).
    Ricerca pubblica: guarda i posti nelle classi ammesse. Ricerca carnet: guarda lo stato SALEABLE."""
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
        messaggi = [str(m.get("message")).strip() for m in (item.get("messages") or [])
                    if isinstance(m, dict) and m.get("message")]
        return Treno(numero=str(treni[0].get("name") or treni[0].get("number") or "?"),
                     stato=stato, servizi=servizi, disponibile=disponibile, messaggi=messaggi)
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


def _messaggio(r: requests.Response) -> str:
    """Il messaggio d'errore del sito, se c'e' (es. 'Carnet non valido')."""
    try:
        return _riassunto(r.json())
    except Exception:
        return (r.text or "").strip()[:200] or f"HTTP {r.status_code}"


def _riassunto(dati) -> str:
    if isinstance(dati, dict):
        for k in ("message", "description", "error", "errorMessage"):
            if dati.get(k):
                return str(dati[k])[:200]
    return json.dumps(dati, ensure_ascii=False)[:200]

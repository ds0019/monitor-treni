#!/usr/bin/env python3
"""
monitor_carnet.py
Controlla se si libera un posto in 2a classe/Standard sui tuoi Frecciarossa
del lunedi' e del giovedi' e ti manda una notifica sul telefono (ntfy e/o Telegram).

  Andata : 07:50 Torino Porta Susa -> Milano Porta Garibaldi
  Ritorno: 18:10 Milano Centrale   -> Torino Porta Susa

Pensato per GitHub Actions (vedi LEGGIMI.md), ma gira anche su qualsiasi computer:
  pip install requests tzdata
  export NTFY_TOPIC="un-nome-segreto-a-tua-scelta"
  python3 monitor_carnet.py --test-notifica   # prova la notifica
  python3 monitor_carnet.py                   # modalita' pubblica, controllo continuo
  python3 monitor_carnet.py --una-volta       # un solo controllo (GitHub Actions / cron)
  python3 monitor_carnet.py --carnet          # modalita' carnet

Variabili d'ambiente facoltative:
  ESCLUDI        viaggi gia' prenotati da non controllare, separati da virgola,
                 es. "16/11 andata, 19/11" (senza andata/ritorno = tutto il giorno)
  GIORNI_AVANTI  quanti giorni in avanti controllare (predefinito 14)

Lo script NON prenota: ti avvisa, e prenoti tu dal sito o dall'app.
Usa le API non ufficiali del sito lefrecce.it: possono cambiare senza preavviso.
"""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import json
import os
import random
import re
import shlex
import sys
import time
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
except ImportError:
    sys.exit("Serve Python 3.9 o superiore.")

try:
    import requests
except ImportError:
    sys.exit("Manca il modulo 'requests'. Esegui:  pip install requests tzdata")

# ======================= CONFIGURAZIONE (modifica qui) =======================
VIAGGI = [
    {"nome": "Andata",  "da": "Torino Porta Susa", "a": "Milano Porta Garibaldi", "ora": "07:50"},
    {"nome": "Ritorno", "da": "Milano Centrale",   "a": "Torino Porta Susa",      "ora": "18:10"},
]
GIORNI_SETTIMANA = (0, 3)   # 0=lun 1=mar 2=mer 3=gio 4=ven  -> lunedi' e giovedi'
GIORNI_AVANTI = int(os.environ.get("GIORNI_AVANTI") or 14)  # finestra da controllare
INTERVALLO_MINUTI = 5

# Il tuo carnet vale solo in 2a classe/Standard
SERVIZI_AMMESSI = ("standard", "2ª", "2a classe", "seconda")
SERVIZI_ESCLUSI = ("salottino", "executive", "premium", "business")
ESCLUDI = os.environ.get("ESCLUDI", "")

# Notifiche: impostale come variabili d'ambiente (vedi LEGGIMI.md)
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
LINK_PRENOTA = "https://www.trenitalia.com/"
# ============================================================================

BFF = "https://www.lefrecce.it/Channels.Website.BFF.WEB/website"
TZ = ZoneInfo("Europe/Rome")
CARTELLA = Path(__file__).resolve().parent
FILE_STATO = CARTELLA / "stato.json"
FILE_CURL = CARTELLA / "richiesta_carnet.txt"
FILE_DEBUG = CARTELLA / "ultima_risposta.json"
GIORNI = ["lun", "mar", "mer", "gio", "ven", "sab", "dom"]

HEADERS_BROWSER = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "it-IT,it;q=0.9,en;q=0.8",
    "Content-Type": "application/json",
    "Origin": "https://www.lefrecce.it",
    "Referer": "https://www.lefrecce.it/",
}


class AccessoNegato(Exception):
    """Il sito ha risposto 401/403: richiesta scaduta o bloccata."""


def adesso() -> dt.datetime:
    return dt.datetime.now(TZ)


def log(msg: str) -> None:
    print(f"[{adesso():%d/%m %H:%M:%S}] {msg}", flush=True)


# ------------------------------- notifiche ----------------------------------
def notifica(titolo: str, testo: str, urgente: bool = True) -> bool:
    log(f"NOTIFICA >> {titolo} | {testo.replace(chr(10), ' | ')}")
    inviata = False
    if NTFY_TOPIC:
        try:
            r = requests.post(NTFY_SERVER.rstrip("/") + "/", timeout=15, json={
                "topic": NTFY_TOPIC, "title": titolo, "message": testo,
                "priority": 5 if urgente else 3, "tags": ["train"], "click": LINK_PRENOTA,
            })
            r.raise_for_status()
            inviata = True
        except Exception as e:
            log(f"ntfy non raggiungibile: {e}")
    if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
        try:
            r = requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", timeout=15, json={
                "chat_id": TELEGRAM_CHAT_ID, "text": f"{titolo}\n{testo}\n{LINK_PRENOTA}",
                "disable_web_page_preview": True,
            })
            r.raise_for_status()
            inviata = True
        except Exception as e:
            log(f"Telegram non raggiungibile: {e}")
    return inviata


# --------------------------------- stato ------------------------------------
def carica_stato() -> dict:
    try:
        return json.loads(FILE_STATO.read_text(encoding="utf-8"))
    except Exception:
        return {}


def salva_stato(stato: dict) -> None:
    FILE_STATO.write_text(json.dumps(stato, ensure_ascii=False, indent=2), encoding="utf-8")


# ------------------------------ date e orari --------------------------------
def partenza(data: dt.date, hhmm: str) -> dt.datetime:
    h, m = map(int, hhmm.split(":"))
    return dt.datetime.combine(data, dt.time(h, m), tzinfo=TZ)


def iso_ricerca(data: dt.date, hhmm: str) -> str:
    # cerca a partire da 30 minuti prima, cosi' il treno e' tra i primi risultati
    return (partenza(data, hhmm) - dt.timedelta(minutes=30)).isoformat(timespec="milliseconds")


def prossime_date(giorni_avanti: int) -> list[dt.date]:
    """Lunedi' e giovedi' (GIORNI_SETTIMANA) da oggi ai prossimi `giorni_avanti` giorni."""
    ora = adesso()
    date = []
    for i in range(giorni_avanti):
        d = ora.date() + dt.timedelta(days=i)
        if d.weekday() in GIORNI_SETTIMANA and any(partenza(d, v["ora"]) > ora for v in VIAGGI):
            date.append(d)
    return date


def escluso(data: dt.date, viaggio: str, esclusioni: set[str]) -> bool:
    """True se il viaggio e' in ESCLUDI (es. '16/11 andata', '2026-11-19', '16/11')."""
    forme = {data.isoformat(), f"{data:%d/%m}", f"{data.day}/{data.month}"}
    return any(f in esclusioni or f"{f} {viaggio.lower()}" in esclusioni for f in forme)


def leggi_esclusioni(testo: str) -> set[str]:
    return {" ".join(x.lower().split()) for x in re.split(r"[,;\n]+", testo) if x.strip()}


# ------------------------------- stazioni -----------------------------------
def _norm(s: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", s.lower()).split())


def id_stazione(sess: requests.Session, nome: str, stato: dict) -> int:
    cache = stato.setdefault("_stazioni", {})
    if nome in cache:
        return cache[nome]
    r = sess.get(f"{BFF}/locations/search", params={"name": nome, "limit": 10}, timeout=20)
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
        log(f"Attenzione: per '{nome}' uso '{scelta.get('displayName') or scelta.get('name')}'")
    cache[nome] = int(scelta["id"])
    return cache[nome]


# ------------------------- richiesta carnet (cURL) --------------------------
def leggi_curl(path: Path) -> dict:
    """Legge un 'Copy as cURL (bash)' preso dagli strumenti per sviluppatori del browser."""
    if not path.exists():
        raise ValueError(f"manca il file {path.name} (vedi LEGGIMI.md, modalita' carnet)")
    testo = path.read_text(encoding="utf-8").strip()
    if "^" in testo and "\\\n" not in testo:
        raise ValueError("sembra un cURL in formato Windows (cmd): copia 'Copy as cURL (bash)'")
    testo = re.sub(r"\\\r?\n", " ", testo)
    try:
        tok = shlex.split(testo)
    except ValueError as e:
        raise ValueError(f"non riesco a leggere il cURL: {e}")
    if not tok or tok[0] != "curl":
        raise ValueError("il file deve contenere un comando che inizia con 'curl'")

    url = metodo = dati = None
    headers: dict[str, str] = {}
    i = 1
    while i < len(tok):
        t = tok[i]
        nxt = tok[i + 1] if i + 1 < len(tok) else ""
        if t in ("-H", "--header"):
            k, _, v = nxt.partition(":")
            headers[k.strip()] = v.strip()
            i += 2
        elif t in ("-b", "--cookie"):
            headers["Cookie"] = nxt
            i += 2
        elif t in ("-A", "--user-agent"):
            headers["User-Agent"] = nxt
            i += 2
        elif t in ("-e", "--referer"):
            headers["Referer"] = nxt
            i += 2
        elif t in ("-X", "--request"):
            metodo = nxt.upper()
            i += 2
        elif t in ("-d", "--data", "--data-raw", "--data-binary", "--data-ascii"):
            dati = nxt
            i += 2
        elif t == "--url":
            url = nxt
            i += 2
        elif t.startswith("-"):
            i += 1  # --compressed, -s, ecc.
        else:
            url = url or t
            i += 1

    if not url or not dati:
        raise ValueError("nel cURL mancano l'indirizzo o il corpo della richiesta (--data-raw)")
    if dati.startswith("$"):
        dati = dati[1:]
    try:
        corpo = json.loads(dati)
    except json.JSONDecodeError:
        raise ValueError("il corpo della richiesta non e' JSON: hai copiato la richiesta giusta?")

    via = {"content-length", "accept-encoding", "host"}
    headers = {k: v for k, v in headers.items() if k.lower() not in via and not k.startswith(":")}
    trovate: set[str] = set()
    sostituisci(copy.deepcopy(corpo), {"departureTime": "", "departureLocationId": 0,
                                       "arrivalLocationId": 0}, trovate)
    if "departureTime" not in trovate:
        raise ValueError("nella richiesta non c'e' 'departureTime': non sembra la ricerca dei treni")
    if "solutions" not in url:
        log(f"Attenzione: l'indirizzo copiato non contiene 'solutions' ({url}); provo lo stesso.")
    return {"url": url, "metodo": metodo or "POST", "headers": headers, "corpo": corpo,
            "mtime": path.stat().st_mtime}


def sostituisci(obj, valori: dict, trovate: set) -> None:
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


# ------------------------------- ricerca ------------------------------------
def corpo_pubblico(dep: int, arr: int, quando: str) -> dict:
    return {
        "departureLocationId": dep,
        "arrivalLocationId": arr,
        "departureTime": quando,
        "adults": 1,
        "children": 0,
        "criteria": {"frecceOnly": True, "regionalOnly": False, "noChanges": True,
                     "order": "DEPARTURE_DATE", "limit": 10, "offset": 0},
        "advancedSearchRequest": {"bestFare": False},
    }


def cerca(sess, modo: str, modello: dict | None, dep: int, arr: int, quando: str) -> dict:
    if modo == "carnet":
        corpo = copy.deepcopy(modello["corpo"])
        sostituisci(corpo, {"departureLocationId": dep, "arrivalLocationId": arr,
                            "departureTime": quando}, set())
        r = requests.request(modello["metodo"], modello["url"], headers=modello["headers"],
                             json=corpo, timeout=25)
    else:
        r = sess.post(f"{BFF}/ticket/solutions", json=corpo_pubblico(dep, arr, quando), timeout=25)
    if r.status_code in (401, 403):
        raise AccessoNegato(f"HTTP {r.status_code}")
    r.raise_for_status()
    return r.json()


def e_frecciarossa(treno: dict) -> bool:
    testo = " ".join(str(treno.get(k) or "") for k in
                     ("trainCategory", "acronym", "denomination", "description")).upper()
    return not testo.strip() or "FRECCIAROSSA" in testo or re.search(r"\bFR\b", testo) is not None


def analizza_servizi(item: dict) -> list[dict]:
    servizi = []
    for griglia in item.get("grids") or []:
        for s in griglia.get("services") or []:
            nome = str(s.get("name") or s.get("groupName") or "?")
            low = nome.lower()
            if not any(a in low for a in SERVIZI_AMMESSI) or any(e in low for e in SERVIZI_ESCLUSI):
                continue
            posti, disponibile, offerte = 0, False, []
            for o in s.get("offers") or []:
                n = o.get("availableAmount")
                ok = o.get("status") == "SALEABLE" and (n is None or n > 0)
                disponibile = disponibile or ok
                if ok and n:
                    posti = max(posti, n)
                offerte.append(f"{o.get('name', '?')}={o.get('status', '?')}/{'?' if n is None else n}")
            servizi.append({"nome": nome, "disponibile": disponibile, "posti": posti, "offerte": offerte})
    return servizi


def trova_treno(dati: dict, ora: str) -> dict | None:
    if not isinstance(dati, dict):
        return None
    for item in dati.get("solutions") or []:
        sol = item.get("solution") or item
        dep = str(sol.get("departureTime") or "")
        treni = sol.get("trains") or []
        if dep[11:16] != ora or len(treni) != 1 or not e_frecciarossa(treni[0]):
            continue
        servizi = analizza_servizi(item)
        stato_sol = sol.get("status")
        disponibile = any(s["disponibile"] for s in servizi) if servizi else stato_sol == "SALEABLE"
        return {"numero": str(treni[0].get("name") or treni[0].get("number") or "?"),
                "stato": stato_sol, "servizi": servizi, "disponibile": disponibile}
    return None


# -------------------------------- giro --------------------------------------
def un_giro(sess, modo: str, modello: dict | None, stato: dict, giorni_avanti: int) -> list[str]:
    errori: list[str] = []
    esclusioni = leggi_esclusioni(ESCLUDI)
    for data in prossime_date(giorni_avanti):
        for v in VIAGGI:
            if partenza(data, v["ora"]) <= adesso() + dt.timedelta(minutes=5):
                continue
            chiave = f"{data.isoformat()} {v['nome']}"
            giorno = f"{GIORNI[data.weekday()]} {data:%d/%m}"
            etichetta = f"{v['nome']} {giorno} {v['ora']} {v['da']} -> {v['a']}"
            if escluso(data, v["nome"], esclusioni):
                log(f"{etichetta}: escluso (gia' prenotato)")
                continue
            try:
                dep = id_stazione(sess, v["da"], stato)
                arr = id_stazione(sess, v["a"], stato)
                dati = cerca(sess, modo, modello, dep, arr, iso_ricerca(data, v["ora"]))
            except AccessoNegato:
                raise
            except Exception as e:
                errori.append(f"{etichetta}: {e}")
                log(f"{etichetta}: errore {e}")
                continue

            esito = trova_treno(dati, v["ora"])
            if esito is None:
                FILE_DEBUG.write_text(json.dumps(dati, ensure_ascii=False, indent=2), encoding="utf-8")
                viste = sorted({str((it.get("solution") or it).get("departureTime") or "")[11:16]
                                for it in (dati.get("solutions") or []) if isinstance(it, dict)}
                               ) if isinstance(dati, dict) else []
                msg = f"treno delle {v['ora']} non trovato (partenze viste: {', '.join(viste) or 'nessuna'})"
                errori.append(f"{etichetta}: {msg}")
                log(f"{etichetta}: {msg}. Risposta salvata in {FILE_DEBUG.name}")
                continue

            dettagli = "; ".join(
                f"{s['nome']}: {s['posti'] if s['posti'] else ('si' if s['disponibile'] else 0)}"
                for s in esito["servizi"]) or ("posti in vendita" if esito["disponibile"] else "esaurito")
            log(f"{etichetta} (FR {esito['numero']}): "
                f"{'DISPONIBILE' if esito['disponibile'] else 'esaurito'} - {dettagli}")
            if esito["disponibile"] and stato.get(chiave) is not True:
                notifica(f"Posto libero! {v['nome']} {giorno} {v['ora']}",
                         f"FR {esito['numero']} {v['da']} → {v['a']}\n{dettagli}\nPrenota subito col carnet.")
            stato[chiave] = esito["disponibile"]
            time.sleep(random.uniform(1.5, 3.5))

    oggi = adesso().date().isoformat()
    for k in [k for k in stato if not k.startswith("_") and k[:10] < oggi]:
        del stato[k]
    return errori


def main() -> None:
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="Avvisa quando si libera un posto sui treni del lunedi' e giovedi'.")
    ap.add_argument("--carnet", action="store_true",
                    help="usa la richiesta copiata dalla pagina 'Prenota con carnet' (richiesta_carnet.txt)")
    ap.add_argument("--una-volta", action="store_true",
                    help="un solo controllo e poi esce (GitHub Actions, cron)")
    ap.add_argument("--giorni", type=int, default=GIORNI_AVANTI,
                    help=f"quanti giorni in avanti controllare (predefinito {GIORNI_AVANTI})")
    ap.add_argument("--intervallo", type=float, default=INTERVALLO_MINUTI, help="minuti tra i controlli")
    ap.add_argument("--test-notifica", action="store_true", help="invia una notifica di prova ed esce")
    args = ap.parse_args()

    if args.test_notifica:
        if not (NTFY_TOPIC or TELEGRAM_TOKEN):
            sys.exit("Imposta prima NTFY_TOPIC (o TELEGRAM_TOKEN e TELEGRAM_CHAT_ID).")
        ok = notifica("Test monitor carnet", "Se leggi questo, le notifiche funzionano.", urgente=False)
        sys.exit(0 if ok else 1)

    modo = "carnet" if args.carnet else "pubblica"
    modello = None
    if modo == "carnet":
        try:
            modello = leggi_curl(FILE_CURL)
        except ValueError as e:
            sys.exit(f"Modalita' carnet: {e}")
    if not (NTFY_TOPIC or (TELEGRAM_TOKEN and TELEGRAM_CHAT_ID)):
        log("Attenzione: nessuna notifica configurata (NTFY_TOPIC o TELEGRAM_*). Vedrai gli avvisi solo qui.")

    sess = requests.Session()
    sess.headers.update(HEADERS_BROWSER)
    stato = carica_stato()
    giorni = "/".join(GIORNI[g] for g in GIORNI_SETTIMANA)
    if args.una_volta:
        log(f"Modalita' {modo}: controllo {giorni} dei prossimi {args.giorni} giorni.")
    else:
        log(f"Modalita' {modo}: controllo {giorni} dei prossimi {args.giorni} giorni "
            f"ogni {args.intervallo:g} minuti. Ctrl+C per fermare.")

    try:
        while True:
            if modo == "carnet" and FILE_CURL.exists() and FILE_CURL.stat().st_mtime != modello["mtime"]:
                try:
                    modello = leggi_curl(FILE_CURL)
                    stato["_avvisato_accesso"] = False
                    log("Richiesta carnet aggiornata ricaricata.")
                except ValueError as e:
                    log(f"Nuovo {FILE_CURL.name} non valido ({e}); tengo quello vecchio.")
            try:
                errori = un_giro(sess, modo, modello, stato, args.giorni)
                stato["_avvisato_accesso"] = False
                if errori:
                    stato["_errori"] = stato.get("_errori", 0) + 1
                    if stato["_errori"] == 3:
                        notifica("Monitor carnet: problemi", "\n".join(errori[:3]), urgente=False)
                else:
                    stato["_errori"] = 0
            except AccessoNegato as e:
                if modo == "carnet":
                    msg = (f"La richiesta copiata e' scaduta o rifiutata ({e}). "
                           f"Ricopia il cURL in {FILE_CURL.name}: lo script lo ricarica da solo.")
                else:
                    msg = f"Il sito ha rifiutato la richiesta ({e}), forse un blocco anti-bot. Riprovo."
                log(msg)
                if not stato.get("_avvisato_accesso"):
                    notifica("Monitor carnet: accesso negato", msg, urgente=False)
                    stato["_avvisato_accesso"] = True
            salva_stato(stato)
            if args.una_volta:
                break
            time.sleep(max(30.0, args.intervallo * 60 + random.uniform(-20, 20)))
    except KeyboardInterrupt:
        salva_stato(stato)
        log("Fermato.")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
monitor_carnet.py - avvisa sul telefono quando si libera un posto sui tuoi Frecciarossa.

File del progetto:
  config.yaml        treni, giorni, classi, link della notifica  <- di solito si modifica solo questo
  trenitalia.py      ricerca dei treni sul sito Trenitalia
  notifiche.py       invio delle notifiche (ntfy / Telegram)
  monitor_carnet.py  questo file: configurazione, stato, ciclo dei controlli, avvio

Uso:
  pip install requests tzdata pyyaml
  python3 monitor_carnet.py --test-notifica   # prova la notifica
  python3 monitor_carnet.py                   # controllo continuo (computer)
  python3 monitor_carnet.py --una-volta       # un solo controllo (GitHub Actions, cron)
  python3 monitor_carnet.py --carnet          # modalita' carnet (richiesta_carnet.txt)

Variabili d'ambiente:
  NTFY_TOPIC, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID   segreti per le notifiche (vedi notifiche.py)
  ESCLUDI          viaggi gia' prenotati, es. "16/11 andata, 19/11" (si somma a config.yaml)
  GIORNI_SPECIALI  viaggi per cui avvisare a OGNI controllo finche' c'e' posto, stesso formato
                   (si somma a giorni_speciali di config.yaml; ESCLUDI vince su GIORNI_SPECIALI)
  GIORNI_AVANTI    sostituisce giorni_avanti di config.yaml
  LINK_NOTIFICA    sostituisce notifiche.link di config.yaml
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import random
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

try:
    import yaml
except ImportError:
    sys.exit("Manca il modulo 'pyyaml'. Esegui:  pip install requests tzdata pyyaml")

from notifiche import Notificatore
from trenitalia import AccessoNegato, Trenitalia, partenze_viste

TZ = ZoneInfo("Europe/Rome")
CARTELLA = Path(__file__).resolve().parent
FILE_CONFIG = CARTELLA / "config.yaml"
FILE_STATO = CARTELLA / "stato.json"
FILE_CURL = CARTELLA / "richiesta_carnet.txt"
FILE_DEBUG = CARTELLA / "ultima_risposta.json"
NOMI_GIORNI = ["lun", "mar", "mer", "gio", "ven", "sab", "dom"]
LINK_PREDEFINITO = "https://www.trenitalia.com/"

log = logging.getLogger("monitor")


def adesso() -> dt.datetime:
    return dt.datetime.now(TZ)


# ----------------------------- configurazione -------------------------------
class ConfigNonValida(Exception):
    pass


@dataclass
class Viaggio:
    nome: str
    da: str
    a: str
    ora: str   # "HH:MM"


@dataclass
class Config:
    viaggi: list[Viaggio]
    giorni: list[int]          # 0 = lunedi'
    giorni_avanti: int
    classi_ammesse: list[str]
    classi_escluse: list[str]
    escludi: set[str]
    speciali: set[str]         # avvisa a ogni controllo, non una volta sola
    link: str
    priorita: int
    intervallo_minuti: float


def carica_config(path: Path) -> Config:
    try:
        dati = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        raise ConfigNonValida(f"manca il file {path.name}")
    except yaml.YAMLError as e:
        raise ConfigNonValida(f"{path.name} non e' scritto bene: {e}")
    if not isinstance(dati, dict):
        raise ConfigNonValida(f"{path.name} non contiene le impostazioni attese")

    viaggi = []
    for i, v in enumerate(dati.get("viaggi") or [], 1):
        if not isinstance(v, dict) or any(k not in v for k in ("nome", "da", "a", "ora")):
            raise ConfigNonValida(f"viaggio n. {i}: servono nome, da, a, ora")
        viaggi.append(Viaggio(str(v["nome"]), str(v["da"]), str(v["a"]), _orario(v["ora"], v["nome"])))
    if not viaggi:
        raise ConfigNonValida("nessun treno nella sezione 'viaggi'")

    giorni = []
    for g in dati.get("giorni") or []:
        sigla = str(g).strip().lower()[:3]
        if sigla not in NOMI_GIORNI:
            raise ConfigNonValida(f"giorno non riconosciuto: '{g}' (usa lun, mar, mer, gio, ven, sab, dom)")
        giorni.append(NOMI_GIORNI.index(sigla))
    if not giorni:
        raise ConfigNonValida("nessun giorno nella sezione 'giorni'")

    classi = dati.get("classi") or {}
    notifiche = dati.get("notifiche") or {}
    escludi = list(dati.get("escludi") or []) + [os.environ.get("ESCLUDI", "")]
    speciali = list(dati.get("giorni_speciali") or []) + [os.environ.get("GIORNI_SPECIALI", "")]
    return Config(
        viaggi=viaggi,
        giorni=sorted(set(giorni)),
        giorni_avanti=_intero(os.environ.get("GIORNI_AVANTI") or dati.get("giorni_avanti", 14),
                              "giorni_avanti", 1, 180),
        classi_ammesse=[str(c) for c in (classi.get("ammesse") or ["standard"])],
        classi_escluse=[str(c) for c in (classi.get("escluse") or [])],
        escludi=leggi_lista(escludi),
        speciali=leggi_lista(speciali),
        link=os.environ.get("LINK_NOTIFICA") or str(notifiche.get("link") or LINK_PREDEFINITO),
        priorita=_intero(notifiche.get("priorita", 5), "notifiche.priorita", 1, 5),
        intervallo_minuti=float(dati.get("intervallo_minuti", 5)),
    )


def _orario(valore, viaggio) -> str:
    # Senza virgolette YAML legge 18:10 come numero in base 60 (1090): lo riconvertiamo.
    if isinstance(valore, int) and not isinstance(valore, bool):
        valore = f"{valore // 60}:{valore % 60:02d}"
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", str(valore).strip())
    if not m or int(m[1]) > 23 or int(m[2]) > 59:
        raise ConfigNonValida(f"viaggio '{viaggio}': orario non valido '{valore}' (scrivi ad es. \"07:50\")")
    return f"{int(m[1]):02d}:{m[2]}"


def _intero(valore, nome: str, minimo: int, massimo: int) -> int:
    try:
        n = int(str(valore).strip())
    except ValueError:
        raise ConfigNonValida(f"{nome}: serve un numero, trovato '{valore}'")
    if not minimo <= n <= massimo:
        raise ConfigNonValida(f"{nome}: deve essere tra {minimo} e {massimo}, trovato {n}")
    return n


def leggi_lista(voci: list) -> set[str]:
    """Liste come ESCLUDI e GIORNI_SPECIALI: "16/11 andata, 19/11" -> {"16/11 andata", "19/11"}."""
    testo = ",".join(str(x) for x in voci)
    return {" ".join(x.lower().split()) for x in re.split(r"[,;\n]+", testo) if x.strip()}


# --------------------------------- stato ------------------------------------
# stato.json ricorda com'era ogni treno al controllo precedente
# (chiave "AAAA-MM-GG Nome" -> True/False), cosi' la notifica parte una volta sola.
# Le chiavi che iniziano con "_" sono di servizio (codici stazioni, contatori errori).
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
    # cerca da 30 minuti prima, cosi' il treno e' tra i primi risultati
    return (partenza(data, hhmm) - dt.timedelta(minutes=30)).isoformat(timespec="milliseconds")


def prossime_date(cfg: Config) -> list[dt.date]:
    """Giorni da controllare: quelli della settimana scelti, nei prossimi `giorni_avanti` giorni."""
    ora = adesso()
    date = []
    for i in range(cfg.giorni_avanti):
        d = ora.date() + dt.timedelta(days=i)
        if d.weekday() in cfg.giorni and any(partenza(d, v.ora) > ora for v in cfg.viaggi):
            date.append(d)
    return date


def in_lista(data: dt.date, viaggio: str, lista: set[str]) -> bool:
    """True se il viaggio e' nella lista (voci come '16/11 andata', '2026-11-19', '16/11')."""
    forme = {data.isoformat(), f"{data:%d/%m}", f"{data.day}/{data.month}"}
    return any(f in lista or f"{f} {viaggio.lower()}" in lista for f in forme)


# --------------------------------- giro -------------------------------------
def un_giro(cfg: Config, ti: Trenitalia, notif: Notificatore, stato: dict) -> list[str]:
    """Controlla tutti i treni una volta. Restituisce gli errori incontrati."""
    errori: list[str] = []
    cache_stazioni = stato.setdefault("_stazioni", {})
    for data in prossime_date(cfg):
        for v in cfg.viaggi:
            if partenza(data, v.ora) <= adesso() + dt.timedelta(minutes=5):
                continue
            giorno = f"{NOMI_GIORNI[data.weekday()]} {data:%d/%m}"
            etichetta = f"{v.nome} {giorno} {v.ora} {v.da} -> {v.a}"
            if in_lista(data, v.nome, cfg.escludi):
                log.info("%s: escluso (gia' prenotato)", etichetta)
                continue
            try:
                da = ti.id_stazione(v.da, cache_stazioni)
                a = ti.id_stazione(v.a, cache_stazioni)
                dati = ti.cerca(da, a, iso_ricerca(data, v.ora))
            except AccessoNegato:
                raise
            except Exception as e:
                errori.append(f"{etichetta}: {e}")
                log.error("%s: errore %s", etichetta, e)
                continue

            treno = ti.trova_treno(dati, v.ora)
            if treno is None:
                FILE_DEBUG.write_text(json.dumps(dati, ensure_ascii=False, indent=2), encoding="utf-8")
                msg = f"treno delle {v.ora} non trovato (partenze viste: {', '.join(partenze_viste(dati)) or 'nessuna'})"
                errori.append(f"{etichetta}: {msg}")
                log.warning("%s: %s. Risposta salvata in %s", etichetta, msg, FILE_DEBUG.name)
                continue

            log.info("%s (FR %s): %s - %s", etichetta, treno.numero,
                     "DISPONIBILE" if treno.disponibile else "esaurito", treno.dettagli())
            # Notifica quando il treno passa da esaurito (o mai visto) a disponibile.
            # Per i giorni speciali avvisa a ogni controllo, finche' il posto resta libero.
            chiave = f"{data.isoformat()} {v.nome}"
            gia_avvisato = stato.get(chiave) is True
            speciale = in_lista(data, v.nome, cfg.speciali)
            if treno.disponibile and (speciale or not gia_avvisato):
                titolo = "Ancora libero!" if gia_avvisato else "Posto libero!"
                notif.invia(f"{titolo} {v.nome} {giorno} {v.ora}",
                            f"FR {treno.numero} {v.da} → {v.a}\n{treno.dettagli()}\nPrenota subito col carnet.")
            stato[chiave] = treno.disponibile
            time.sleep(random.uniform(1.5, 3.5))

    oggi = adesso().date().isoformat()
    for k in [k for k in stato if not k.startswith("_") and k[:10] < oggi]:
        del stato[k]
    return errori


# --------------------------------- avvio ------------------------------------
def configura_log() -> None:
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass
    formato = logging.Formatter("[%(asctime)s] %(message)s", datefmt="%d/%m %H:%M:%S")
    formato.converter = lambda s: dt.datetime.fromtimestamp(s, TZ).timetuple()
    gestore = logging.StreamHandler(sys.stdout)
    gestore.setFormatter(formato)
    logging.basicConfig(level=logging.INFO, handlers=[gestore], force=True)


def main() -> None:
    configura_log()
    ap = argparse.ArgumentParser(description="Avvisa quando si libera un posto sui tuoi Frecciarossa.")
    ap.add_argument("--config", type=Path, default=FILE_CONFIG, help="file di configurazione (config.yaml)")
    ap.add_argument("--carnet", action="store_true",
                    help=f"usa la richiesta copiata dalla pagina 'Prenota con carnet' ({FILE_CURL.name})")
    ap.add_argument("--una-volta", action="store_true", help="un solo controllo e poi esce (GitHub Actions, cron)")
    ap.add_argument("--giorni", type=int, help="quanti giorni in avanti controllare (sostituisce config.yaml)")
    ap.add_argument("--intervallo", type=float, help="minuti tra i controlli (sostituisce config.yaml)")
    ap.add_argument("--test-notifica", action="store_true", help="invia una notifica di prova ed esce")
    args = ap.parse_args()

    stato = carica_stato()
    try:
        cfg = carica_config(args.config)
    except ConfigNonValida as e:
        log.error("Errore in %s: %s", args.config.name, e)
        if stato.get("_errore_config") != str(e):   # avvisa una volta per lo stesso errore
            Notificatore(LINK_PREDEFINITO).invia("Monitor treni: config.yaml da correggere", str(e), urgente=False)
            stato["_errore_config"] = str(e)
            salva_stato(stato)
        sys.exit(1)
    stato.pop("_errore_config", None)
    if args.giorni:
        cfg.giorni_avanti = args.giorni
    intervallo = args.intervallo or cfg.intervallo_minuti

    notif = Notificatore(cfg.link, cfg.priorita)
    if args.test_notifica:
        if not notif.configurato:
            sys.exit("Imposta prima NTFY_TOPIC (o TELEGRAM_TOKEN e TELEGRAM_CHAT_ID).")
        sys.exit(0 if notif.invia("Test monitor treni", "Se leggi questo, le notifiche funzionano.",
                                  urgente=False) else 1)
    if not notif.configurato:
        log.warning("Nessuna notifica configurata (NTFY_TOPIC o TELEGRAM_*): vedrai gli avvisi solo qui.")

    try:
        ti = Trenitalia(cfg.classi_ammesse, cfg.classi_escluse, FILE_CURL if args.carnet else None)
    except ValueError as e:
        sys.exit(f"Modalita' carnet: {e}")

    giorni = "/".join(NOMI_GIORNI[g] for g in cfg.giorni)
    log.info("Modalita' %s: controllo %s dei prossimi %d giorni%s.", ti.modo, giorni, cfg.giorni_avanti,
             "" if args.una_volta else f" ogni {intervallo:g} minuti (Ctrl+C per fermare)")
    if cfg.speciali:
        log.info("Giorni speciali (avviso a ogni controllo): %s", ", ".join(sorted(cfg.speciali)))

    try:
        while True:
            try:
                if ti.ricarica_carnet():
                    stato["_avvisato_accesso"] = False
                    log.info("Richiesta carnet aggiornata ricaricata.")
            except ValueError as e:
                log.warning("Nuovo %s non valido (%s); tengo quello vecchio.", FILE_CURL.name, e)
            try:
                errori = un_giro(cfg, ti, notif, stato)
                stato["_avvisato_accesso"] = False
                stato["_errori"] = stato.get("_errori", 0) + 1 if errori else 0
                if stato["_errori"] == 3:   # avvisa dopo 3 giri di fila con problemi
                    notif.invia("Monitor treni: problemi", "\n".join(errori[:3]), urgente=False)
            except AccessoNegato as e:
                if ti.modo == "carnet":
                    msg = (f"La richiesta copiata e' scaduta o rifiutata ({e}). "
                           f"Ricopia il cURL in {FILE_CURL.name}.")
                else:
                    msg = f"Il sito ha rifiutato la richiesta ({e}), forse un blocco anti-bot. Riprovo."
                log.error(msg)
                if not stato.get("_avvisato_accesso"):
                    notif.invia("Monitor treni: accesso negato", msg, urgente=False)
                    stato["_avvisato_accesso"] = True
            salva_stato(stato)
            if args.una_volta:
                break
            time.sleep(max(30.0, intervallo * 60 + random.uniform(-20, 20)))
    except KeyboardInterrupt:
        salva_stato(stato)
        log.info("Fermato.")


if __name__ == "__main__":
    main()

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
  python3 monitor_carnet.py --pubblica        # ignora il carnet: guarda solo i posti in vendita

Variabili d'ambiente:
  NTFY_TOPIC, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID   segreti per le notifiche (vedi notifiche.py)
  CARNET_NUMERO, CARNET_NOME, CARNET_COGNOME     segreti del carnet: se ci sono tutti e tre lo script
                   controlla i posti prenotabili COL CARNET (modalita' carnet), altrimenti
                   solo i posti in vendita (modalita' pubblica)
  ESCLUDI          viaggi gia' prenotati, es. "16/11 andata, 19/11" (si somma a config.yaml)
  GIORNI_SPECIALI  viaggi per cui avvisare a OGNI controllo finche' c'e' posto, stesso formato
                   (si somma a giorni_speciali di config.yaml; ESCLUDI vince su GIORNI_SPECIALI)
  SOLO_GIORNI      se c'e', controlla SOLO questi viaggi, es. "23/11 andata, 26/11"
                   (ignora giorni e giorni_avanti; si somma a solo_giorni di config.yaml)
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
from trenitalia import AccessoNegato, Carnet, CarnetNonValido, Trenitalia, partenze_viste

TZ = ZoneInfo("Europe/Rome")
CARTELLA = Path(__file__).resolve().parent
FILE_CONFIG = CARTELLA / "config.yaml"
FILE_STATO = CARTELLA / "stato.json"
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
    solo: list[tuple[dt.date, str | None]]   # se non vuota: controlla solo questi (giorno, viaggio)
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
    solo = list(dati.get("solo_giorni") or []) + [os.environ.get("SOLO_GIORNI", "")]
    return Config(
        viaggi=viaggi,
        giorni=sorted(set(giorni)),
        giorni_avanti=_intero(os.environ.get("GIORNI_AVANTI") or dati.get("giorni_avanti", 14),
                              "giorni_avanti", 1, 180),
        classi_ammesse=[str(c) for c in (classi.get("ammesse") or ["standard"])],
        classi_escluse=[str(c) for c in (classi.get("escluse") or [])],
        escludi=leggi_lista(escludi),
        speciali=leggi_lista(speciali),
        solo=leggi_solo_giorni(solo, viaggi, adesso().date()),
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


def leggi_carnet() -> Carnet | None:
    """Il carnet dai secret CARNET_NUMERO, CARNET_NOME, CARNET_COGNOME (mai da config.yaml: la repo e' pubblica)."""
    valori = [os.environ.get(k, "").strip() for k in ("CARNET_NUMERO", "CARNET_NOME", "CARNET_COGNOME")]
    if not any(valori):
        return None
    if not all(valori):
        raise ConfigNonValida("per la modalita' carnet servono tutti e tre i secret: "
                              "CARNET_NUMERO, CARNET_NOME e CARNET_COGNOME")
    return Carnet(*valori)


def leggi_lista(voci: list) -> set[str]:
    """Liste come ESCLUDI e GIORNI_SPECIALI: "16/11 andata, 19/11" -> {"16/11 andata", "19/11"}."""
    testo = ",".join(str(x) for x in voci)
    return {" ".join(x.lower().split()) for x in re.split(r"[,;\n]+", testo) if x.strip()}


def leggi_solo_giorni(voci: list, viaggi: list[Viaggio], oggi: dt.date) -> list[tuple[dt.date, str | None]]:
    """SOLO_GIORNI: "23/11 andata, 26/11" -> [(23/11, "andata"), (26/11, None)]. None = tutti i treni."""
    nomi = {v.nome.lower() for v in viaggi}
    risultato = []
    for voce in leggi_lista(voci):
        parti = voce.split()
        if len(parti) > 1 and parti[0][:3] in NOMI_GIORNI:   # accetta anche "lun 23/11 andata"
            parti = parti[1:]
        if not 1 <= len(parti) <= 2:
            raise ConfigNonValida(f"SOLO_GIORNI: non capisco '{voce}' (scrivi ad es. \"23/11 andata\" o \"23/11\")")
        viaggio = parti[1] if len(parti) == 2 else None
        if viaggio and viaggio not in nomi:
            raise ConfigNonValida(f"SOLO_GIORNI: in '{voce}' il viaggio '{viaggio}' non esiste "
                                  f"(in config.yaml ci sono: {', '.join(sorted(nomi))})")
        risultato.append((_data(parti[0], oggi, voce), viaggio))
    return sorted(set(risultato), key=lambda x: (x[0], x[1] or ""))


def _data(testo: str, oggi: dt.date, voce: str) -> dt.date:
    """'23/11', '23/11/2026' o '2026-11-23'. Senza anno sceglie la data piu' vicina a oggi
    (cosi' a dicembre '05/01' vuol dire gennaio prossimo, e '16/11' passato resta passato)."""
    if m := re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", testo):
        anno, mese, giorno = int(m[1]), int(m[2]), int(m[3])
    elif m := re.fullmatch(r"(\d{1,2})/(\d{1,2})(?:/(\d{2}|\d{4}))?", testo):
        giorno, mese = int(m[1]), int(m[2])
        anno = None if m[3] is None else int(m[3]) + (2000 if len(m[3]) == 2 else 0)
    else:
        raise ConfigNonValida(f"SOLO_GIORNI: data non valida in '{voce}' (scrivi ad es. 23/11)")
    try:
        if anno is not None:
            return dt.date(anno, mese, giorno)
        data = dt.date(oggi.year, mese, giorno)
        if data < oggi - dt.timedelta(days=180):
            data = data.replace(year=oggi.year + 1)
        elif data > oggi + dt.timedelta(days=185):
            data = data.replace(year=oggi.year - 1)
        return data
    except ValueError:
        raise ConfigNonValida(f"SOLO_GIORNI: la data di '{voce}' non esiste")


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


def prossime_date(cfg: Config) -> list[dt.date]:
    """Giorni da controllare: quelli della settimana scelti, nei prossimi `giorni_avanti` giorni."""
    ora = adesso()
    date = []
    for i in range(cfg.giorni_avanti):
        d = ora.date() + dt.timedelta(days=i)
        if d.weekday() in cfg.giorni and any(partenza(d, v.ora) > ora for v in cfg.viaggi):
            date.append(d)
    return date


def da_controllare(cfg: Config) -> list[tuple[dt.date, Viaggio]]:
    """Coppie (giorno, treno) da controllare in questo giro, in ordine di partenza.
    Con SOLO_GIORNI solo quelle indicate, altrimenti i giorni scelti nei prossimi giorni_avanti."""
    if cfg.solo:
        coppie = [(d, v) for d, nome in cfg.solo for v in cfg.viaggi if nome in (None, v.nome.lower())]
    else:
        coppie = [(d, v) for d in prossime_date(cfg) for v in cfg.viaggi]
    limite = adesso() + dt.timedelta(minutes=5)   # i treni in partenza tra meno di 5 minuti non servono piu'
    visti, risultato = set(), []
    for d, v in sorted(coppie, key=lambda c: partenza(c[0], c[1].ora)):
        if partenza(d, v.ora) > limite and (d, v.nome) not in visti:
            visti.add((d, v.nome))
            risultato.append((d, v))
    return risultato


def in_lista(data: dt.date, viaggio: str, lista: set[str]) -> bool:
    """True se il viaggio e' nella lista (voci come '16/11 andata', '2026-11-19', '16/11')."""
    forme = {data.isoformat(), f"{data:%d/%m}", f"{data.day}/{data.month}", f"{data:%d/%m/%Y}"}
    return any(f in lista or f"{f} {viaggio.lower()}" in lista for f in forme)


# --------------------------------- giro -------------------------------------
def un_giro(cfg: Config, ti: Trenitalia, notif: Notificatore, stato: dict) -> list[str]:
    """Controlla tutti i treni una volta. Restituisce gli errori incontrati."""
    errori: list[str] = []
    cache_stazioni = stato.setdefault("_stazioni", {})
    coppie = da_controllare(cfg)
    if cfg.solo and not coppie:
        # SOLO_GIORNI impostata ma con sole date passate: lo dico una volta, non resto zitto per sempre
        voci = ", ".join(f"{d:%d/%m}" + (f" {n}" if n else "") for d, n in cfg.solo)
        log.warning("SOLO_GIORNI (%s): niente da controllare, sono tutti viaggi passati.", voci)
        if stato.get("_avviso_solo_vuoto") != voci:
            notif.invia("Monitor treni: niente da controllare",
                        f"In SOLO_GIORNI ci sono solo viaggi passati ({voci}). Aggiorna la variabile.", urgente=False)
            stato["_avviso_solo_vuoto"] = voci
    elif coppie:
        stato.pop("_avviso_solo_vuoto", None)

    if ti.carnet and coppie:
        # come il pulsante CONFERMA del sito: verifica il carnet (CarnetNonValido/AccessoNegato -> main)
        rimasti = ti.verifica_carnet()
        stato.pop("_avviso_carnet", None)
        log.info("Carnet verificato%s.", f": {rimasti} viaggi rimasti" if rimasti is not None else "")
        if rimasti is not None and rimasti <= 0:
            log.warning("Sul carnet non restano viaggi: non controllo i treni.")
            if not stato.get("_avviso_carnet_finito"):
                notif.invia("Monitor treni: carnet finito",
                            "Sul carnet non restano viaggi. Quando ne compri uno nuovo, aggiorna i secret CARNET_*.",
                            urgente=False)
                stato["_avviso_carnet_finito"] = True
            return errori
        stato.pop("_avviso_carnet_finito", None)

    for data, v in coppie:
        giorno = f"{NOMI_GIORNI[data.weekday()]} {data:%d/%m}"
        etichetta = f"{v.nome} {giorno} {v.ora} {v.da} -> {v.a}"
        if in_lista(data, v.nome, cfg.escludi):
            log.info("%s: escluso (gia' prenotato)", etichetta)
            continue
        try:
            da = ti.id_stazione(v.da, cache_stazioni)
            a = ti.id_stazione(v.a, cache_stazioni)
            dati = ti.cerca(da, a, partenza(data, v.ora))
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
        # Chiavi diverse per le due modalita': "in vendita" non vuol dire "prenotabile col carnet".
        chiave = f"{data.isoformat()} {v.nome}" + (" carnet" if ti.carnet else "")
        gia_avvisato = stato.get(chiave) is True
        speciale = in_lista(data, v.nome, cfg.speciali)
        if treno.disponibile and (speciale or not gia_avvisato):
            if ti.carnet:
                titolo = "Ancora libero col carnet!" if gia_avvisato else "Posto libero col carnet!"
                testo = f"FR {treno.numero} {v.da} → {v.a}\nPrenota subito."
            else:
                titolo = "Ancora in vendita:" if gia_avvisato else "Posti in vendita:"
                testo = (f"FR {treno.numero} {v.da} → {v.a}\n{treno.dettagli()}\n"
                         f"Col carnet potrebbero essere esauriti.")
            notif.invia(f"{titolo} {v.nome} {giorno} {v.ora}", testo)
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
    ap.add_argument("--pubblica", action="store_true",
                    help="ignora il carnet anche se ci sono i secret CARNET_*: guarda solo i posti in vendita")
    ap.add_argument("--una-volta", action="store_true", help="un solo controllo e poi esce (GitHub Actions, cron)")
    ap.add_argument("--giorni", type=int, help="quanti giorni in avanti controllare (sostituisce config.yaml)")
    ap.add_argument("--intervallo", type=float, help="minuti tra i controlli (sostituisce config.yaml)")
    ap.add_argument("--test-notifica", action="store_true", help="invia una notifica di prova ed esce")
    args = ap.parse_args()

    stato = carica_stato()
    try:
        cfg = carica_config(args.config)
        carnet = None if args.pubblica else leggi_carnet()
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

    ti = Trenitalia(cfg.classi_ammesse, cfg.classi_escluse, carnet)

    if cfg.solo:
        cosa = "solo " + ", ".join(f"{NOMI_GIORNI[d.weekday()]} {d:%d/%m}" + (f" {n}" if n else "")
                                   for d, n in cfg.solo)
    else:
        cosa = f"{'/'.join(NOMI_GIORNI[g] for g in cfg.giorni)} dei prossimi {cfg.giorni_avanti} giorni"
    log.info("Modalita' %s: controllo %s%s.", ti.modo, cosa,
             "" if args.una_volta else f" ogni {intervallo:g} minuti (Ctrl+C per fermare)")
    if cfg.speciali:
        log.info("Giorni speciali (avviso a ogni controllo): %s", ", ".join(sorted(cfg.speciali)))
    if not ti.carnet:
        log.info("Guardo i posti IN VENDITA, non quelli prenotabili col carnet "
                 "(per quelli servono i secret CARNET_NUMERO, CARNET_NOME, CARNET_COGNOME).")

    try:
        while True:
            try:
                errori = un_giro(cfg, ti, notif, stato)
                stato["_avvisato_accesso"] = False
                stato["_errori"] = stato.get("_errori", 0) + 1 if errori else 0
                if stato["_errori"] == 3:   # avvisa dopo 3 giri di fila con problemi
                    notif.invia("Monitor treni: problemi", "\n".join(errori[:3]), urgente=False)
            except CarnetNonValido as e:
                msg = (f"Il sito non accetta il carnet: {e}. Controlla i secret CARNET_NUMERO, CARNET_NOME "
                       f"e CARNET_COGNOME, oppure il carnet e' scaduto.")
                log.error(msg)
                if stato.get("_avviso_carnet") != str(e):   # una volta per lo stesso errore
                    notif.invia("Monitor treni: carnet non accettato", msg, urgente=False)
                    stato["_avviso_carnet"] = str(e)
            except AccessoNegato as e:
                msg = f"Il sito ha rifiutato la richiesta ({e}), forse il sistema anti-bot. Riprovo al prossimo giro."
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

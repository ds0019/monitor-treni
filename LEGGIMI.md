# Monitor posti Frecciarossa

Ogni 5 minuti controlla se si libera un posto in **2ª classe/Standard** sui treni scelti e ti manda una notifica sull'iPhone. Toccando la notifica si apre l'app Trenitalia. **Non prenota da solo**: quando arriva l'avviso prenoti tu, in fretta.

## I file

| File | A cosa serve | Quando toccarlo |
|---|---|---|
| `config.yaml` | treni, orari, giorni, classi, link della notifica | quando cambiano treni o giorni |
| `monitor_carnet.py` | legge la configurazione, ricorda lo stato, fa il giro dei controlli | quasi mai |
| `trenitalia.py` | ricerca dei treni sul sito Trenitalia | se Trenitalia cambia il sito |
| `notifiche.py` | invio delle notifiche (ntfy / Telegram) | quasi mai |
| `.github/workflows/monitor.yml` | dice a GitHub come lanciare lo script | quasi mai |

## Come funziona

1. **cron-job.org**, ogni 5 minuti, chiede a GitHub di lanciare il workflow.
2. **GitHub Actions** esegue `monitor_carnet.py --una-volta` sui suoi server.
3. Lo script cerca i treni dei giorni scelti (per i prossimi `giorni_avanti` giorni) e guarda i posti Standard.
4. Se un treno passa da esaurito a disponibile, manda **una** notifica con **ntfy**. Lo stato resta salvato nella cache di GitHub, così lo stesso posto non viene segnalato due volte.

## Cambiare treni, giorni o finestra

Apri `config.yaml` su GitHub, tocca la matita, modifica e premi **Commit changes**. Vale dal controllo successivo. Esempi:

- giorni: `giorni: [lun, mar, mer, gio]`
- un altro treno: aggiungi un blocco sotto `viaggi:`, con l'orario **tra virgolette** (`ora: "08:20"`)
- finestra: `giorni_avanti: 21`

Se sbagli qualcosa, ricevi una notifica "config.yaml da correggere" che dice cosa non va.

## Viaggi già prenotati

Il modo più comodo, anche dal telefono, è la variabile GitHub `ESCLUDI`:
**Settings → Secrets and variables → Actions → Variables** → `ESCLUDI`, ad esempio `16/11 andata, 19/11` (solo la data = tutto il giorno).

In alternativa puoi elencarli in `escludi:` dentro `config.yaml`: le due liste si sommano.

## Giorni speciali (avviso a ogni controllo)

Di solito lo stesso posto libero viene segnalato una volta sola. Per i viaggi che ti servono assolutamente, metti la variabile GitHub `GIORNI_SPECIALI`, con lo stesso formato (es. `17/11 andata, 19/11`): finché il posto resta libero ti arriva un avviso **a ogni controllo** (circa ogni 5 minuti), con il titolo "Ancora libero!".

Dopo aver prenotato, aggiungi il viaggio a `ESCLUDI`, che vince su `GIORNI_SPECIALI`, oppure toglilo dai giorni speciali. Puoi elencarli anche in `giorni_speciali:` dentro `config.yaml`.

## Impostazioni su GitHub

| Dove | Nome | Valore |
|---|---|---|
| Secrets | `NTFY_TOPIC` | il tuo argomento ntfy (segreto) |
| Variables | `ESCLUDI` | viaggi già prenotati (facoltativo) |
| Variables | `GIORNI_SPECIALI` | viaggi da segnalare a ogni controllo finché c'è posto (facoltativo) |
| Variables | `GIORNI_AVANTI` | se c'è, sostituisce `giorni_avanti` di config.yaml (facoltativo) |
| Secrets | `RICHIESTA_CARNET` | solo per la modalità carnet (facoltativo) |

**Non scrivere segreti in `config.yaml`**: la repo è pubblica.

## Avvio ogni 5 minuti (cron-job.org)

Il cronjob su cron-job.org chiama, con metodo **POST**:
`https://api.github.com/repos/TUONOME/monitor-treni/actions/workflows/monitor.yml/dispatches`

- Header: `Authorization: Bearer github_pat_...`, `Accept: application/vnd.github+json`, `Content-Type: application/json`
- Corpo: `{"ref":"main"}`
- Risposta attesa: **204**

Il token GitHub (fine-grained, solo questa repo, permesso **Actions: Read and write**) ha una scadenza: quando scade va rigenerato e sostituito su cron-job.org.

## Notifica che apre l'app Trenitalia

Serve un Comando Rapido sull'iPhone chiamato esattamente `Trenitalia`, con la sola azione **Apri app → Trenitalia**. Il link è in `config.yaml` (`notifiche.link`). Se metti `https://www.trenitalia.com/`, la notifica apre invece il sito.

## Modalità carnet (facoltativa)

Normalmente lo script guarda i posti Standard in vendita. Se un giorno ti capita che la notifica arriva ma col carnet il posto non c'è, puoi fargli usare la ricerca del carnet:

1. Da computer, in Chrome: sito Trenitalia → **Abbonamenti e carnet → Prenota** → inserisci i dati del carnet.
2. Strumenti per sviluppatori (F12) → **Network** → cerca un treno → tasto destro sulla richiesta `solutions` → **Copy → Copy as cURL (bash)**.
3. Incolla il testo in un secret `RICHIESTA_CARNET`.

## Cose da sapere

- **Blocco di Trenitalia:** se nel log compare `HTTP 403`, Trenitalia sta bloccando i server di GitHub.
- **Pausa dopo 60 giorni:** se nella repo non cambia nulla per 60 giorni, GitHub disattiva le esecuzioni programmate. Basta modificare un file ogni tanto.
- **API non ufficiali:** lo script usa le API non ufficiali di lefrecce.it. Se Trenitalia le cambia, va aggiornato `trenitalia.py`.
- **Regole di GitHub:** le condizioni d'uso riservano i server di GitHub allo sviluppo di software; un monitor come questo esce da quell'uso.

## Su un computer, invece che su GitHub

```
pip install requests tzdata pyyaml
export NTFY_TOPIC="il-tuo-argomento"
python3 monitor_carnet.py --test-notifica   # prova la notifica
python3 monitor_carnet.py                   # controlla di continuo finché lo lasci aperto
```

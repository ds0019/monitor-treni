# Monitor posti Frecciarossa

Ogni 5 minuti controlla se sui treni scelti si libera un posto **prenotabile col tuo carnet** e ti manda una notifica sull'iPhone. Toccando la notifica si apre l'app Trenitalia. **Non prenota da solo**: quando arriva l'avviso prenoti tu, in fretta.

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
3. Lo script verifica il carnet e cerca i treni dei giorni scelti come fa la pagina "Prenota con carnet" del sito.
4. Se un treno passa da esaurito a prenotabile, manda **una** notifica con **ntfy**. Lo stato resta salvato nella cache di GitHub, così lo stesso posto non viene segnalato due volte.

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

## Solo alcuni giorni (SOLO_GIORNI)

Se ti interessano pochi viaggi, crea la variabile GitHub `SOLO_GIORNI`, ad esempio `23/11 andata, 26/11`. Il monitor controlla **solo** quei viaggi e ignora `giorni` e `giorni_avanti`. Con la sola data controlla andata e ritorno; vanno bene anche date lontane o di sabato e domenica.

Quando tutte le date sono passate ti arriva un avviso: aggiorna la variabile. Se la svuoti (o la cancelli) torna il funzionamento normale. `ESCLUDI` vale anche qui, e vince sempre. In alternativa c'è `solo_giorni:` in `config.yaml`.

## Giorni speciali (avviso a ogni controllo)

Di solito lo stesso posto libero viene segnalato una volta sola. Per i viaggi che ti servono assolutamente, metti la variabile GitHub `GIORNI_SPECIALI`, con lo stesso formato (es. `17/11 andata, 19/11`): finché il posto resta libero ti arriva un avviso **a ogni controllo** (circa ogni 5 minuti), con il titolo "Ancora libero col carnet!".

Dopo aver prenotato, aggiungi il viaggio a `ESCLUDI`, che vince su `GIORNI_SPECIALI`, oppure toglilo dai giorni speciali. Puoi elencarli anche in `giorni_speciali:` dentro `config.yaml`.

## Impostazioni su GitHub

| Dove | Nome | Valore |
|---|---|---|
| Secrets | `NTFY_TOPIC` | il tuo argomento ntfy (segreto) |
| Secrets | `CARNET_NUMERO` | il numero del carnet |
| Secrets | `CARNET_NOME` | il nome dell'intestatario, come sul carnet |
| Secrets | `CARNET_COGNOME` | il cognome dell'intestatario, come sul carnet |
| Variables | `ESCLUDI` | viaggi già prenotati (facoltativo) |
| Variables | `SOLO_GIORNI` | se c'è, controlla solo questi viaggi (facoltativo) |
| Variables | `GIORNI_SPECIALI` | viaggi da segnalare a ogni controllo finché c'è posto (facoltativo) |
| Variables | `GIORNI_AVANTI` | se c'è, sostituisce `giorni_avanti` di config.yaml (facoltativo) |

**Non scrivere segreti in `config.yaml`**: la repo è pubblica. Numero, nome e cognome del carnet bastano per prenotare sul tuo carnet: tienili solo nei Secrets.

## Avvio ogni 5 minuti (cron-job.org)

Il cronjob su cron-job.org chiama, con metodo **POST**:
`https://api.github.com/repos/TUONOME/monitor-treni/actions/workflows/monitor.yml/dispatches`

- Header: `Authorization: Bearer github_pat_...`, `Accept: application/vnd.github+json`, `Content-Type: application/json`
- Corpo: `{"ref":"main"}`
- Risposta attesa: **204**

Il token GitHub (fine-grained, solo questa repo, permesso **Actions: Read and write**) ha una scadenza: quando scade va rigenerato e sostituito su cron-job.org.

## Notifica che apre l'app Trenitalia

Serve un Comando Rapido sull'iPhone chiamato esattamente `Trenitalia`, con la sola azione **Apri app → Trenitalia**. Il link è in `config.yaml` (`notifiche.link`). Se metti `https://www.trenitalia.com/`, la notifica apre invece il sito.

## Modalità carnet e modalità pubblica

Il carnet ha una **quota di posti sua**: un treno può avere biglietti in vendita e allo stesso tempo "Posti Esauriti" per il carnet. Per questo lo script ha due modalità.

- **Carnet** (con i tre secret `CARNET_*`): fa gli stessi passaggi della pagina "Prenota con carnet" del sito. Prima verifica il carnet (`carnet/verify`), poi cerca i treni prenotabili con quel carnet (`carnet/solutions/bookings`). La notifica dice "Posto libero col carnet!". Nel log trovi anche i viaggi rimasti sul carnet.
- **Pubblica** (senza i secret): guarda solo i posti Standard **in vendita**. La notifica dice "Posti in vendita", e col carnet il posto potrebbe non esserci.

**Blocchi del sito:** il sistema anti-bot di Trenitalia rifiuta spesso la verifica del carnet quando arriva dai server di GitHub (pagina "Access Denied"). In quel caso, per quel giro, lo script usa la ricerca pubblica ("Posti in vendita") e riprova il carnet dopo `carnet_ogni_minuti` minuti (in `config.yaml`, predefinito 30). Quando la verifica passa, torna da solo alla modalità carnet. Nel log ogni treno ha accanto `[carnet]` o `[pubblica]`.

Avvisi automatici, una volta sola: se il sito non accetta il carnet (dati sbagliati o carnet scaduto) e quando sul carnet non restano viaggi. Quando compri un carnet nuovo, aggiorna `CARNET_NUMERO`.

## Cose da sapere

- **Blocco di Trenitalia:** se nel log compare `HTTP 403`, il sistema anti-bot del sito sta rifiutando le richieste dai server di GitHub. Ti arriva una notifica "accesso negato".
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

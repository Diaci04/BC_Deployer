# Guida passo passo: usare BC Deployer dal PC aziendale

Il PC aziendale non può installare niente, quindi l'app deve girare su un server e tu la apri dal browser.
Questa guida la mette su **Azure App Service** usando solo il browser, tramite Azure Cloud Shell.
**Niente Docker**: Azure installa Python e le dipendenze da solo.

Tempo stimato: circa un'ora la prima volta. Ti servono:
- il PC personale, dove c'è il codice;
- un browser in cui puoi accedere al portale Azure.

```
PC personale ──git push──► repository privato ──git clone──► Azure Cloud Shell ──az webapp up──► App Service
                                                                                                   ▲
                                                          PC aziendale (solo browser) ─────────────┘
```

---

## Passo 0 — Cosa chiedere prima di iniziare

Prima di fare qualunque cosa, chiarisci questi punti con il tuo responsabile o con l'IT.

1. **Una subscription Azure** in cui creare risorse, con il ruolo *Contributor* almeno su un resource group.
   - Può essere quella della tua azienda o del cliente.
   - Per verificare: apri https://portal.azure.com → *Subscriptions*. Se la lista è vuota, non hai accesso.
2. **Il via libera a salvare i client secret dei tenant BC dei clienti in quest'app.**
   - I secret sono cifrati, ma restano credenziali di produzione dei clienti.
   - Non usare un account Azure o Render personale per credenziali dei clienti.
3. **Il costo**: piano B1, circa 12–13 € al mese. Verifica sul calcolatore prezzi Azure.

> **Se non ottieni una subscription**, c'è un piano B che non richiede nessun server. Usa la pipeline
> `azure-pipelines.yml` in Azure DevOps: esegue `bc_deploy.py` con gli stessi controlli, ma senza
> l'interfaccia web. Vedi in fondo.

---

## Passo 1 — Mettere il codice in un repository privato (dal PC personale)

Cloud Shell deve poter scaricare il codice. Il modo più semplice è un repository Git privato.

Puoi usare:
- **Azure DevOps**, se ne usi già uno (c'è `azure-pipelines.yml`): *Repos → New repository*;
- **GitHub**: *New repository → Private*.

Poi, nella cartella del progetto sul PC personale:

```powershell
git init -b main
git add .
git status          # controlla che NON compaiano .env, .venv o data/: sono esclusi da .gitignore
git commit -m "BC Deployer"
git remote add origin <URL del repository>
git push -u origin main
```

> Il file `.env` contiene la tua chiave e la password locali: **non deve mai finire nel repository**.

---

## Passo 2 — Aprire Azure Cloud Shell

Puoi farlo dal PC aziendale o da quello personale: basta un browser in cui accedi al portale Azure.
Alcune aziende bloccano il portale dai PC personali.

1. Apri https://portal.azure.com e accedi con l'account che ha la subscription.
2. Clicca l'icona **`>_`** in alto (Cloud Shell) e scegli **Bash**.
3. Alla domanda sullo storage scegli **"No storage account required"**, poi la subscription.
   - È una sessione temporanea: i file si perdono dopo circa 20 minuti di inattività.
   - Va bene così, perché il codice si riscarica ogni volta.

Imposta i nomi da usare nei comandi. Cambia `APP`: diventerà l'indirizzo `https://<APP>.azurewebsites.net`
e deve essere unico in tutto Azure.

```bash
RG=rg-bcdeployer
LOC=westeurope
APP=bcdeployer-tuonome
```

Se Cloud Shell si chiude, ripeti questi tre comandi prima di continuare.

---

## Passo 3 — Scaricare il codice

```bash
git clone <URL del repository> bc-deployer
cd bc-deployer
```

Ti verranno chieste delle credenziali:
- **Azure DevOps**: *Repos → Clone → Generate Git Credentials* e usa nome utente e password generati;
- **GitHub**: usa il tuo nome utente e, come password, un *personal access token* con permesso di sola lettura
  sul repository (*Settings → Developer settings → Fine-grained tokens*).

---

## Passo 4 — Generare password e chiave di cifratura

Genera una coppia **nuova**: non riusare quella del PC personale.

```bash
python3 -m venv /tmp/v && /tmp/v/bin/pip -q install -r requirements.txt
/tmp/v/bin/python -m app.cli hash-password      # scegli la password di login → stampa l'hash
/tmp/v/bin/python -m app.cli gen-key            # stampa la chiave
```

Salva subito la **chiave** nel tuo password manager.
- Senza la chiave, le connessioni salvate non sono più leggibili e vanno reinserite.
- La password invece la puoi sempre rigenerare.

---

## Passo 5 — Creare l'app su Azure

Dalla cartella `bc-deployer` in Cloud Shell:

```bash
az group create -n $RG -l $LOC
az webapp up -n $APP -g $RG -l $LOC --runtime "PYTHON:3.12" --sku B1
```

Il comando fa tre cose:
1. crea il piano e la web app;
2. carica il codice;
3. fa installare ad Azure le dipendenze da `requirements.txt`.

Ci vogliono alcuni minuti. L'app ancora non parte: mancano le impostazioni del passo successivo.

---

## Passo 6 — Configurare l'app

Incolla l'hash e la chiave del passo 4 al posto dei segnaposto. Usa gli **apici singoli**: l'hash
contiene il carattere `$`.

```bash
az webapp config appsettings set -g $RG -n $APP --settings \
  APP_ENV=production \
  DATA_DIR=/home/data \
  SQLITE_JOURNAL_MODE=DELETE \
  PUBLIC_BASE_URL=https://$APP.azurewebsites.net \
  TIMEZONE=Europe/Rome \
  'ADMIN_PASSWORD_HASH=$argon2id$v=19$...incolla-qui...' \
  'FERNET_KEYS=incolla-qui-la-chiave='

az webapp config set -g $RG -n $APP --always-on true --min-tls-version 1.2 --ftps-state Disabled \
  --startup-file "python -m uvicorn app.asgi:app --host 0.0.0.0 --port 8000 --workers 1 --proxy-headers --forwarded-allow-ips='*'"
az webapp config set -g $RG -n $APP --generic-configurations '{"healthCheckPath": "/health"}'
az webapp update -g $RG -n $APP --https-only true
az webapp restart -g $RG -n $APP
```

Cosa fanno le impostazioni principali:
- **`/home/data`**: è lo spazio persistente di App Service. Lì vivono il database e i `.app` caricati,
  e sopravvivono a riavvii e aggiornamenti.
- **Always On**: tiene l'app sempre accesa. Senza, i deploy programmati non partirebbero.
- **Startup command**: avvia un solo processo. Non aumentare istanze o worker, altrimenti i job partirebbero due volte.

---

## Passo 7 — Primo accesso (dal PC aziendale)

1. Apri `https://<APP>.azurewebsites.net/health`: deve rispondere `{"status":"ok", ...}`.
   Il primo avvio può richiedere un paio di minuti.
2. Apri `https://<APP>.azurewebsites.net` e accedi con utente `admin` e la password del passo 4.
3. *Connessioni → Nuova*: inserisci tenant, client ID, client secret ed environment, poi **Testa connessione**.
4. **Il primo deploy fallo su una Sandbox**: l'app non è ancora stata provata contro un BC reale.

Se non risponde, guarda i log in Cloud Shell:

```bash
az webapp log config -g $RG -n $APP --docker-container-logging filesystem
az webapp log tail -g $RG -n $APP
```

Gli errori tipici sono:
- un valore copiato male in `ADMIN_PASSWORD_HASH` o `FERNET_KEYS`;
- la build delle dipendenze ancora in corso.

---

## Passo 8 (consigliato) — Limitare chi può raggiungere l'app

L'app ha già login, limite ai tentativi e cookie sicuri. Puoi aggiungere un secondo livello.

**Login aziendale prima dell'app.**
- Portale → la web app → *Authentication* → *Add identity provider* → *Microsoft*.
- Così si arriva al login dell'app solo dopo aver fatto accesso con un account del tenant.
- Serve il permesso di creare una *app registration*. Se non ce l'hai, chiedilo all'IT.

**Solo dalla rete aziendale.**
- Si fa da *Networking → Access restriction*, consentendo solo gli IP di uscita aziendali.
- Spesso gli IP di uscita cambiano (proxy cloud): fattelo confermare dall'IT prima di affidarti a questo.

---

## Aggiornare l'app a una nuova versione

1. Sul PC personale: `git commit` e `git push`.
2. In Cloud Shell: ripeti le variabili del passo 2, poi

   ```bash
   cd bc-deployer && git pull     # se la sessione è scaduta: git clone, come al passo 3
   az webapp up -n $APP -g $RG -l $LOC --runtime "PYTHON:3.12" --sku B1
   ```

Le impostazioni del passo 6 restano. Il database si aggiorna da solo all'avvio.

Aggiorna quando non ci sono deploy in corso. Una run interrotta dal riavvio si può comunque rilanciare.

---

## Backup

- **Prima di un aggiornamento importante**, fai una copia coerente del database.
  Portale → la web app → *SSH* (si apre nel browser), poi:

  ```bash
  python -m app.cli backup
  ```

  Il backup va in `/home/data/backups/`.
- La cosa più importante da conservare è **la chiave `FERNET_KEYS`**, nel password manager.
- Per backup automatici: *Backups* nel menu della web app. Richiede uno storage account.

---

## Piano B — Solo pipeline Azure DevOps, senza server

Se non è possibile ottenere una subscription Azure, `azure-pipelines.yml` esegue `bc_deploy.py` su un agente
Microsoft:
- stesso ordine delle dipendenze, stessi controlli di versione e stessa gestione errori dell'app;
- parametri `environment`, `schemaSync` e `schedule`;
- esecuzione settimanale programmata.

Rinunci a interfaccia web, storico e notifiche.

1. Metti i `.app` nella cartella `apps/` del repository.
2. Definisci `BC_TENANT_ID`, `BC_CLIENT_ID` e `BC_CLIENT_SECRET` come variabili **segrete** della pipeline.
3. *Pipelines → New pipeline → Existing YAML file → azure-pipelines.yml*, poi *Run pipeline*,
   scegliendo environment e opzioni.

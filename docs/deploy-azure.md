# Pubblicazione su Azure App Service (Linux, container)

Tutti i comandi si possono eseguire da **Azure Cloud Shell** (portale Azure → icona `>_` → Bash):
serve solo il browser, niente da installare sul PC. Per caricare il codice in Cloud Shell usa
`git clone` del repository oppure *Gestisci file → Carica* con lo zip del progetto.

L'immagine viene costruita direttamente in Azure con `az acr build`: non serve Docker in locale.

## 0. Variabili per i comandi

```bash
RG=rg-bcdeployer
LOC=westeurope
ACR=bcdeployeracr$RANDOM          # nome globale univoco, solo lettere minuscole e numeri
PLAN=plan-bcdeployer
APP=bcdeployer-tuaazienda         # diventa https://$APP.azurewebsites.net
SA=bcdeployerdata$RANDOM          # storage account per il volume persistente
KV=kv-bcdeployer-$RANDOM          # Key Vault per i segreti
```

## 1. Registry e immagine

```bash
az group create -n $RG -l $LOC
az acr create -g $RG -n $ACR --sku Basic
cd bc-deployer                     # cartella con il Dockerfile
az acr build -r $ACR -t bc-deployer:1.0.0 .
```

## 2. Piano e web app

Serve almeno il piano **B1**: l'opzione *Always On* (necessaria allo scheduler) non c'è nel piano gratuito.

```bash
az appservice plan create -g $RG -n $PLAN --is-linux --sku B1
az webapp create -g $RG -p $PLAN -n $APP \
  --container-image-name $ACR.azurecr.io/bc-deployer:1.0.0 \
  --assign-identity '[system]'

# La web app scarica l'immagine dal registry con la propria identità (niente password)
PRINCIPAL=$(az webapp identity show -g $RG -n $APP --query principalId -o tsv)
az role assignment create --assignee $PRINCIPAL --role AcrPull \
  --scope $(az acr show -n $ACR --query id -o tsv)
az webapp config set -g $RG -n $APP --generic-configurations '{"acrUseManagedIdentityCreds": true}'
```

## 3. Volume persistente (`/data`)

Raccomandato: una share **Azure Files** montata su `/data`. Contiene il DB SQLite e i `.app` caricati,
e si può proteggere con Azure Backup.

```bash
az storage account create -g $RG -n $SA --sku Standard_LRS --kind StorageV2 --min-tls-version TLS1_2
az storage share-rm create -g $RG --storage-account $SA -n bcdeployer-data --quota 20
KEY=$(az storage account keys list -g $RG -n $SA --query [0].value -o tsv)
az webapp config storage-account add -g $RG -n $APP \
  --custom-id data --storage-type AzureFiles \
  --account-name $SA --share-name bcdeployer-data --access-key "$KEY" --mount-path /data
```

> **SQLite su Azure Files**: la modalità WAL di SQLite non funziona su file system di rete. Per questo
> sotto si imposta `SQLITE_JOURNAL_MODE=DELETE`. Per un solo utente va bene. Se preferisci un database
> gestito, vedi il punto 8 (Postgres).

Alternativa più semplice, senza storage account: lo storage integrato `/home` di App Service.
In questo caso imposta `WEBSITES_ENABLE_APP_SERVICE_STORAGE=true`, `DATA_DIR=/home/data` e
`SQLITE_JOURNAL_MODE=DELETE`.

## 4. Segreti in Key Vault

Prima genera i due segreti. Puoi farlo nello stesso Cloud Shell, dentro la cartella del progetto:

```bash
python3 -m venv /tmp/v && /tmp/v/bin/pip -q install argon2-cffi cryptography pydantic-settings sqlalchemy
/tmp/v/bin/python -m app.cli hash-password      # → ADMIN_PASSWORD_HASH
/tmp/v/bin/python -m app.cli gen-key            # → FERNET_KEYS
```

```bash
az keyvault create -g $RG -n $KV --enable-rbac-authorization true
az role assignment create --assignee $(az ad signed-in-user show --query id -o tsv) \
  --role "Key Vault Secrets Officer" --scope $(az keyvault show -n $KV --query id -o tsv)
az keyvault secret set --vault-name $KV -n admin-password-hash --value '$argon2id$v=19$...'
az keyvault secret set --vault-name $KV -n fernet-keys --value 'la-chiave-generata='
az role assignment create --assignee $PRINCIPAL --role "Key Vault Secrets User" \
  --scope $(az keyvault show -n $KV --query id -o tsv)
```

Conserva una copia di `FERNET_KEYS` anche fuori da Azure, per esempio nel password manager aziendale.
Senza quella chiave i client secret salvati nel DB (anche nei backup) non sono più leggibili.

## 5. Variabili d'ambiente

```bash
az webapp config appsettings set -g $RG -n $APP --settings \
  WEBSITES_PORT=8000 \
  APP_ENV=production \
  DATA_DIR=/data \
  SQLITE_JOURNAL_MODE=DELETE \
  PUBLIC_BASE_URL=https://$APP.azurewebsites.net \
  TIMEZONE=Europe/Rome \
  MAX_APP_SIZE_MB=100 \
  "ADMIN_PASSWORD_HASH=@Microsoft.KeyVault(VaultName=$KV;SecretName=admin-password-hash)" \
  "FERNET_KEYS=@Microsoft.KeyVault(VaultName=$KV;SecretName=fernet-keys)"
```

Le altre variabili sono elencate in [.env.example](../.env.example). Si possono modificare anche dal portale:
*Web app → Impostazioni → Variabili di ambiente*. Ogni modifica riavvia l'app.

## 6. HTTPS, Always On, health check, una sola istanza

```bash
az webapp update -g $RG -n $APP --https-only true
az webapp config set -g $RG -n $APP --always-on true --min-tls-version 1.2 --ftps-state Disabled
az webapp config set -g $RG -n $APP --generic-configurations '{"healthCheckPath": "/health"}'
az webapp restart -g $RG -n $APP
```

- **Una sola istanza**: non attivare autoscale né scale-out. Lo scheduler e i lock di deploy vivono
  nel processo, e con due istanze i job partirebbero due volte.
- **Accesso solo dalla rete aziendale** (consigliato):
  ```bash
  az webapp config access-restriction add -g $RG -n $APP --rule-name ufficio \
    --action Allow --ip-address 203.0.113.0/24 --priority 100
  ```
  Per un secondo livello di protezione puoi attivare anche *Authentication* (Easy Auth con Entra ID):
  così all'app arriva solo chi ha un account aziendale, prima ancora del login dell'app.
- App Service chiude le richieste dopo 230 secondi. Non è un problema: gli upload verso BC girano
  in background e il log live manda un heartbeat ogni 15 secondi. Un upload dal browser di decine di MB
  deve però completarsi entro quel tempo.

Verifica: `https://$APP.azurewebsites.net/health` deve rispondere `{"status":"ok",...}`.
Log in tempo reale: `az webapp log tail -g $RG -n $APP`. Prima va abilitato il logging del container:
`az webapp log config -g $RG -n $APP --docker-container-logging filesystem`.

## 7. Aggiornamento a una nuova versione

```bash
az acr build -r $ACR -t bc-deployer:1.1.0 .
az webapp config container set -g $RG -n $APP --container-image-name $ACR.azurecr.io/bc-deployer:1.1.0
```

All'avvio l'app applica da sola le migrazioni del DB. Se il riavvio interrompe un deploy in corso,
la run viene marcata *Interrotta* e si può rilanciare. Conviene quindi aggiornare quando non ci sono
deploy in esecuzione né job imminenti.

## 8. (Opzionale) Postgres invece di SQLite

```bash
az postgres flexible-server create -g $RG -n pg-bcdeployer --tier Burstable --sku-name Standard_B1ms \
  --version 16 --admin-user bcdadmin --admin-password '<password-robusta>' --public-access 0.0.0.0
az postgres flexible-server db create -g $RG -s pg-bcdeployer -d bcdeployer
az webapp config appsettings set -g $RG -n $APP --settings \
  "DATABASE_URL=postgresql+psycopg://bcdadmin:<password>@pg-bcdeployer.postgres.database.azure.com:5432/bcdeployer?sslmode=require"
```

`--public-access 0.0.0.0` consente l'accesso solo dai servizi Azure. Il volume `/data` serve comunque,
per i pacchetti `.app` caricati. Anche la password del DB è meglio tenerla in Key Vault, come gli
altri segreti.

## 9. Backup

Cosa salvare:
- il **database**;
- la chiave **`FERNET_KEYS`**, separatamente (punto 4);
- i `.app` in `/data/uploads`, che sono facoltativi perché ricaricabili.

**Azure Files** (con il volume del punto 3):
- *Recovery Services vault → Backup → Azure File Share*: snapshot giornalieri con retention
  configurabile. Il ripristino è di un singolo file o dell'intera share.
- Snapshot manuale prima di un'operazione delicata:
  `az storage share snapshot --account-name $SA --name bcdeployer-data`.

**Copia coerente del DB SQLite** (anche con l'app in funzione, usa l'API di backup di SQLite):

```bash
az webapp ssh -g $RG -n $APP        # oppure portale → Strumenti di sviluppo → SSH
python -m app.cli backup             # → /data/backups/bcdeployer-AAAAMMGG-HHMMSS.db
```

La copia finisce nella stessa share, quindi viene inclusa anche nei backup di Azure Files.

**Ripristino**: ferma l'app (`az webapp stop`), sostituisci `/data/bcdeployer.db` con la copia
(dal portale *Storage account → File share → Sfoglia*, oppure da un altro container), poi
`az webapp start`.

**Postgres**: backup automatici del Flexible Server (7 giorni di default, ripristino point-in-time).
Esportazione manuale: `pg_dump "$DATABASE_URL" > bcdeployer.sql`.

# Pubblicazione su Render o Fly.io

Vincoli comuni a entrambe le piattaforme:
- **volume persistente montato su `/data`**: contiene il DB SQLite e i `.app` caricati;
- **una sola istanza, sempre accesa**: lo scheduler vive nel processo, quindi niente sospensione
  per inattività e niente scale-out;
- **variabili obbligatorie**: `ADMIN_PASSWORD_HASH` e `FERNET_KEYS`, da salvare come segreti.
  Si generano con `python -m app.cli hash-password` e `python -m app.cli gen-key`. Le altre
  variabili sono in [.env.example](../.env.example).

---

## Render

Render si gestisce interamente dal browser (dashboard).

1. **New → Web Service**, scegli il repository (GitHub/GitLab) con questo progetto.
   *Language*: **Docker**. Render costruisce l'immagine dal `Dockerfile`.
2. **Instance type**: almeno **Starter**. Il piano gratuito va in sospensione e non supporta
   i dischi persistenti.
3. **Advanced → Add Disk**: *Mount path* `/data`, dimensione 1–5 GB.
4. **Advanced → Health Check Path**: `/health`.
5. **Environment**. Render imposta da solo `PORT`, che il container usa.

   | Chiave | Valore |
   |---|---|
   | `APP_ENV` | `production` |
   | `DATA_DIR` | `/data` |
   | `PUBLIC_BASE_URL` | `https://<nome>.onrender.com` |
   | `ADMIN_PASSWORD_HASH` | hash argon2 (incollalo così com'è, senza apici) |
   | `FERNET_KEYS` | chiave Fernet |

6. **Create Web Service**. Al termine apri `https://<nome>.onrender.com/health`.

In alternativa ai passi 1–5 puoi aggiungere un Blueprint `render.yaml` nella root del repository:

```yaml
services:
  - type: web
    name: bc-deployer
    runtime: docker
    plan: starter
    healthCheckPath: /health
    disk:
      name: bcd-data
      mountPath: /data
      sizeGB: 2
    envVars:
      - key: APP_ENV
        value: production
      - key: DATA_DIR
        value: /data
      - key: ADMIN_PASSWORD_HASH
        sync: false        # valore chiesto alla creazione, non salvato nel repository
      - key: FERNET_KEYS
        sync: false
```

Note:
- Con un disco collegato Render non fa deploy senza interruzione: durante un aggiornamento
  l'app resta ferma per qualche secondo. Un deploy BC in corso in quel momento risulta *Interrotto*.
- HTTPS è automatico; Render passa lo schema originale nell'header `X-Forwarded-Proto`.

**Backup su Render**
- Render fa **snapshot automatici giornalieri del disco**, conservati 7 giorni. Il ripristino si fa
  da *Disks → Snapshots*.
- Copia coerente del DB: tab **Shell** del servizio (si apre nel browser) → `python -m app.cli backup`.
  Per scaricare il file usa `render ssh` + `scp` da un PC con la Render CLI, oppure copialo su uno
  storage esterno.

---

## Fly.io

Fly.io si gestisce con la CLI `flyctl`. Non potendo installarla sul PC aziendale, hai due strade:
usarla da un altro computer, oppure far eseguire il deploy a una GitHub Action (vedi sotto).

1. Crea l'app senza deploy: `fly launch --no-deploy --copy-config` (oppure `fly apps create bc-deployer-tuonome`).
2. Metti questo `fly.toml` nella root del progetto:

   ```toml
   app = "bc-deployer-tuonome"
   primary_region = "fra"

   [env]
     APP_ENV = "production"
     DATA_DIR = "/data"
     PORT = "8000"
     PUBLIC_BASE_URL = "https://bc-deployer-tuonome.fly.dev"

   [[mounts]]
     source = "bcd_data"
     destination = "/data"
     snapshot_retention = 14

   [http_service]
     internal_port = 8000
     force_https = true
     auto_stop_machines = "off"     # lo scheduler deve restare acceso
     auto_start_machines = true
     min_machines_running = 1

     [[http_service.checks]]
       grace_period = "20s"
       interval = "30s"
       method = "GET"
       path = "/health"
       timeout = "5s"

   [[vm]]
     size = "shared-cpu-1x"
     memory = "512mb"
   ```

3. Volume e segreti:

   ```bash
   fly volumes create bcd_data --region fra --size 2
   fly secrets set ADMIN_PASSWORD_HASH='$argon2id$v=19$...' FERNET_KEYS='la-chiave='
   ```

4. Deploy e numero di macchine:

   ```bash
   fly deploy
   fly scale count 1      # una sola macchina: un volume si collega a una sola macchina
   ```

5. Verifica: `https://bc-deployer-tuonome.fly.dev/health`.

**Deploy da GitHub senza installare nulla in locale**: crea un token con `fly tokens create deploy`
(da qualunque PC con flyctl, una volta sola) e salvalo come secret `FLY_API_TOKEN` del repository.
Poi aggiungi `.github/workflows/fly.yml`:

```yaml
on: { push: { branches: [main] } }
jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: superfly/flyctl-actions/setup-flyctl@master
      - run: flyctl deploy --remote-only
        env:
          FLY_API_TOKEN: ${{ secrets.FLY_API_TOKEN }}
```

**Backup su Fly.io**
- I volumi hanno **snapshot automatici giornalieri**, conservati per `snapshot_retention` giorni
  (5 di default; 14 nel `fly.toml` sopra). Elenco: `fly volumes snapshots list <volume-id>`.
  Per ripristinare crei un nuovo volume da uno snapshot: `fly volumes create bcd_data --snapshot-id <id>`.
- Copia coerente del DB e download:

  ```bash
  fly ssh console -C "python -m app.cli backup /data/backups/manuale.db"
  fly ssh sftp get /data/backups/manuale.db ./bcdeployer-backup.db
  ```

---

## Ripristino (tutte le piattaforme)

1. Ferma l'app, oppure assicurati che non ci siano deploy in corso.
2. Sostituisci `/data/bcdeployer.db` con la copia di backup.
3. Riavvia con **la stessa `FERNET_KEYS`** che era in uso quando il backup è stato fatto.
   All'avvio l'app aggiorna lo schema se il backup viene da una versione precedente.

# BC Deployer

🇬🇧 English · [🇮🇹 Italiano](README.it.md)

Install and update `.app` extensions on **Business Central SaaS** from your browser, using the Automation API.

1. Upload your packages.
2. The app works out the install order from their dependencies and checks the target environment.
3. The deploy runs right away or at a time you choose, and you follow it live.

It runs on your own PC at http://localhost:8000. All calls to Entra ID and Business Central are made by the
local server, so the client secret never reaches the browser. Only Python is needed: no Docker, no build step.

> ⚠️ **Deprecation.** Microsoft is removing `extensionUpload` (extension upload through the Automation API)
> in **Business Central v30** (2027 release wave 1). An environment loses it when it is upgraded to v30:
> from **April 2027**, and at the latest by **early September 2027** if you postpone the update.
> Before then the app will need a backend based on the Admin Center API. The code is prepared for it
> (see [Development](#development)), but it is not implemented yet.

## Features

- **Dependency-aware deploys.** The install order comes from the apps' manifests. Dependencies that are
  missing or too old, a downgrade, or a Base Application that is too old stop the deploy before it starts.
  An app already installed at the same version is skipped.
- **Schema sync mode** *Add* or *Force Sync*, and **schedule** *current version* / *next minor* / *next major*,
  as in the Extension Management page of Business Central.
- **Scheduled deploys**: once at a given date and time, or recurring with a cron expression
  (Europe/Rome time zone by default).
- **Live status** for every app. When one app fails, the deploy stops and the remaining ones are marked as
  skipped. One deploy at a time per environment.
- **History** with filters, full logs and a **rerun** button for failed deployments.
- **Connections** grouped by customer, with a one-click **Copy** to add more environments for the same customer.
- **Notifications** at the end of a deploy by **email**, **Microsoft Teams** or **Slack**: every result or
  only failures, per connection.
- **Production safety**: for a production environment you must type its name to confirm.
- **Secrets**: client secrets and webhook URLs are encrypted at rest and never shown again after saving.
  Single admin login with a hashed password, CSRF protection and rate limiting.

## Requirements

- Windows with **Python 3.12 or later** (the `py` launcher is enough: `py -0` lists the installed versions).
- Internet access to download the libraries, and to reach `login.microsoftonline.com` and
  `api.businesscentral.dynamics.com`.
- An app registration in Entra ID with access to your Business Central environments
  (see [Connecting to Business Central](#connecting-to-business-central)).

## Installation (once)

1. Download or clone the repository.
2. Double-click **`Installa.bat`**. It takes a few minutes.
3. When asked for the **encryption key**:
   - if you already have saved connections, paste the key you used before;
   - otherwise press **Enter** and a new one is created.
4. Choose the **password** you will use to log in (at least 12 characters).

Password (hashed) and key are stored in the `.env` file in the app folder. Keep a copy of the key in a password
manager: without it, saved connections have to be entered again.

To change the password, run `Installa.bat` again and answer `s` (sì, "yes") to the question about changing it.

> The scripts and the in-app text are in Italian. The deployment logic does not depend on the language.

## Daily use

Double-click **`Avvia.bat`**. The browser opens: log in with user `admin` and your password.

- Keep the black window open, even minimized: closing it stops the app.
- Scheduled deploys only run while the PC is on and the app is open. If the app starts within 60 minutes of
  the planned time, the deploy still runs.

**Desktop shortcut:** right-click `Avvia.bat` → *Send to → Desktop (create shortcut)*. Do not move the app
folder afterwards, or the shortcut stops working.

**Start with Windows:** double-click **`Avvio-automatico.bat`**. The app starts by itself at every login,
minimized. Open it at http://localhost:8000. Double-click the same file again to turn it off.

## Connecting to Business Central

Do this once per tenant.

**In Entra ID (Azure)**
1. *App registrations → New registration*, then *Certificates & secrets → New client secret*.
2. *API permissions → Add → Dynamics 365 Business Central → Application permissions →*
   `Automation.ReadWrite.All` → **Grant admin consent**.

**In Business Central**, for every environment:
1. Open the *Microsoft Entra Applications* page → new record with the Client ID, state *Enabled*.
2. Assign the permission sets `D365 AUTOMATION` and `EXTEN. MGT. - ADMIN`.

**Optional, to pick the environment from a list:** also add the *Application* permission
`AdminCenter.ReadWrite.All` (same *Dynamics 365 Business Central* API) and authorize the app in the **Business
Central admin center**, page *Authorized Microsoft Entra apps*, by pasting its Client ID. Without this step the
app still works: you type the environment name yourself.

**In the app:** *Connessioni (Connections) → Nuova connessione (New)*. Enter tenant, client ID and client secret,
then press **Carica elenco da Business Central (Load list from Business Central)** to pick the environment and
company from dropdowns. Choosing a production environment ticks *Produzione* automatically. Save, then press
**Testa (Test)**.

To edit a connection, just **click its card**: the lists load automatically.

**More environments for the same customer:** press **Copia (Copy)** on a connection, type the new environment
name and save. Tenant, client ID, client secret and notifications are kept from the original.

## Running a deploy

1. *Deploy* → pick the connection and drag in the `.app` files.
2. Check the install order and the verification against the environment.
3. Choose the options:
   - **Schema sync mode**: *Add* (normal) or *Force Sync*, which also applies destructive schema changes;
   - **Schedule**: *current version* (now), or *next minor / major*, meaning the app is installed when the
     environment is updated to that version. With these two the run is marked as "scheduled" and the app
     does not wait for an outcome;
   - **When**: now, at a date and time, or recurring.
4. Start. For a production environment you have to type its name to confirm.

If an app fails, the deploy stops and the rest are skipped. The Automation API does not return an error
message, only a status: the details are in the *Extension Deployment Status* page of Business Central.
Errors returned directly by the API calls are shown in full. From *Storico (History)* you can review any
deploy and rerun it.

## Notifications

At the end of a deploy the app can notify you by **email**, **Teams** or **Slack**. For each connection you
choose whether to receive every result or only failures.

**Email:** the first time, open *Impostazioni (Settings)* and enter your mail (SMTP) server, then press
the test button. For Office 365:

| Field | Value |
|---|---|
| Server | `smtp.office365.com` |
| Port | `587` |
| Security | STARTTLS |
| User | your email address |
| Password | your password, or an app password if you use two-step verification |

If the test fails with an authentication error (`535 5.7.139`), SMTP sending is disabled for your account:
ask your IT department to enable *SMTP AUTH*, or to give you an internal mail server.

Then, in each connection, enter the recipients separated by commas.

**Teams / Slack:** paste a webhook URL in the connection. For Teams, use a Workflows flow ("post to a
channel when a webhook request is received"); for Slack, an Incoming Webhook. The URL is encrypted.

## Sharing the app with a colleague

1. Double-click **`Crea-pacchetto.bat`**: it creates `bc-deployer.zip` on your Desktop **without** your data
   (password, key and connections).
2. Send the zip.
3. Your colleague extracts it, runs `Installa.bat` (pressing **Enter** at the key question and choosing their
   own password), then runs `Avvia.bat` and creates their own connections.

Everyone has their own data. Never send your `.env`, and never send keys or client secrets by chat or email.

## Where the data lives

| What | Where |
|---|---|
| password (hashed) and encryption key | `.env` in the app folder |
| connections, email settings, history, scheduled deploys, uploaded packages | `%LOCALAPPDATA%\bcdeployer-data` |
| Python and libraries | `%LOCALAPPDATA%\bcdeployer-venv` |

The database is outside the app folder on purpose: a SQLite database inside a folder synced by OneDrive can
get corrupted.

**Backup:** with the app closed, copy the `.env` file and the `%LOCALAPPDATA%\bcdeployer-data` folder.

## Troubleshooting

| What you see | What to do |
|---|---|
| *Python non trovato* | you need the `py` command, Python 3.12 or later |
| error while downloading libraries (`ProxyError`, `SSL`) | the corporate proxy blocks `pypi.org`: ask your IT department |
| *Windows blocca alcune librerie* | application control prevents the app from running on that PC: run it on a server instead |
| *Prima esegui Installa.bat* | run `Installa.bat` first |
| the browser cannot find the page | the app is not running: start `Avvia.bat` |
| the window closes immediately | open PowerShell in the folder and run `.\Avvia.bat` to read the error |
| forgot the password | run `Installa.bat` again and change it |
| *Impossibile decifrare il segreto* | the key in `.env` is wrong: run `Installa.bat` and paste the right one |
| emails do not arrive | open the deploy in *Storico*: the end of the log says whether sending worked. Test the server from *Impostazioni* |

## Development

**Stack:** Python, FastAPI, SQLAlchemy (SQLite, switchable to Postgres through `DATABASE_URL`), Alembic
migrations applied at startup, APScheduler running in-process, Jinja2 and htmx with no build step.

**Configuration.** Everything lives in `.env`, written by `Installa.bat`. The other options (maximum file
size, package retention days, timeouts) are listed in [.env.example](.env.example).

**On a server.** To keep the app always on, or use it from several PCs, see the deployment guides
(in Italian):
- [Azure App Service, step by step](docs/guida-pc-aziendale.md)
- [Azure with containers](docs/deploy-azure.md)
- [Render or Fly.io](docs/deploy-render-fly.md)

It must run as a **single process**: the scheduler and the per-environment locks live in memory.

**Command line.** `bc_deploy.py` does the same deploy without the web interface, for example from
[azure-pipelines.yml](azure-pipelines.yml):

```bash
export BC_TENANT_ID=... BC_CLIENT_ID=... BC_CLIENT_SECRET=...
python bc_deploy.py --environment Sandbox --apps ./apps --schema-sync "Force Sync" --schedule "Next minor version"
```

**Tests.** The suite runs against a fake Business Central built on respx, following the Microsoft
documentation ([tests/fakebc.py](tests/fakebc.py)):

```powershell
py -m venv $env:LOCALAPPDATA\bcdeployer-venv-dev
& $env:LOCALAPPDATA\bcdeployer-venv-dev\Scripts\python -m pip install -r requirements-dev.txt
& $env:LOCALAPPDATA\bcdeployer-venv-dev\Scripts\python -m pytest
```

**How it talks to Business Central.** For each app: create or update the `extensionUpload` record
(Business Central keeps a single one per company), `PATCH` the package into `extensionContent`, call
`Microsoft.NAV.upload` to start the installation, then poll `extensionDeploymentStatus` with backoff. Requests
are retried on 429 and 5xx errors honoring `Retry-After`; the call that starts the installation is never
retried blindly, to avoid two installations.

**Known limits**
- `extensionDeploymentStatus` returns only a status, with no error message, and its possible values are not
  documented: `Completed` and `Failed` are treated as final, anything else as "in progress".
- To support the Admin Center API (`pteInstall`) after BC v30, implement the `DeployBackend` interface in
  [app/bc/base.py](app/bc/base.py). The orchestration and the web interface do not need to change.

## License

[BSD 3-Clause](LICENSE). You can use, modify and redistribute it, including for commercial purposes, as long as
you keep the copyright notice. It is provided as is, without warranty.

# BC Deployer

🇮🇹 Italiano · [🇬🇧 English](README.md)

Installa e aggiorna estensioni `.app` su **Business Central SaaS** dal browser:
1. carichi i pacchetti;
2. l'app calcola l'ordine di installazione e controlla l'environment;
3. il deploy parte subito o all'orario che scegli, e lo segui in tempo reale.

Gira sul tuo PC, su http://localhost:8000. Serve solo Python, cioè il comando `py`.

> ⚠️ **Scadenza.** Microsoft toglie l'upload delle estensioni dall'Automation API con **BC versione 30**.
> Ogni environment lo perde quando passa alla v30: da **aprile 2027**, al più tardi **a inizio settembre 2027**.
> Prima di allora l'app va aggiornata per usare l'Admin Center API.

## Prima installazione

1. Doppio clic su **`Installa.bat`**. Ci vuole qualche minuto.
2. Alla domanda sulla **chiave**:
   - se hai già delle connessioni salvate, incolla la chiave che usavi;
   - altrimenti premi **Invio**: ne crea una nuova.
3. Scegli la **password** per entrare: almeno 12 caratteri.

Password e chiave restano salvate nel file `.env` della cartella. Tieni una copia della chiave nel password
manager: senza, le connessioni salvate vanno reinserite.

## Uso di tutti i giorni

Doppio clic su **`Avvia.bat`**: si apre il browser. Entra con utente `admin` e la tua password.

- Lascia aperta la finestra nera, anche ridotta a icona: se la chiudi, l'app si ferma.
- I deploy programmati partono solo se il PC è acceso e l'app è aperta. Se l'app si avvia entro 60 minuti
  dall'orario previsto, il deploy parte comunque.

**Collegamento sul Desktop:** clic destro su `Avvia.bat` → *Invia a → Desktop (crea collegamento)*
(su Windows 11: *Mostra altre opzioni*). Non spostare la cartella dell'app, altrimenti il collegamento smette di funzionare.

**Avvio automatico:** doppio clic su **`Avvio-automatico.bat`**. L'app parte da sola a ogni accesso a Windows,
ridotta a icona. Si apre da http://localhost:8000. Un altro doppio clic sullo stesso file lo disattiva.

**Cambiare password:** rilancia `Installa.bat` e rispondi `s`.

## Primo collegamento a Business Central

Va fatto una volta per ogni tenant.

**In Entra ID (Azure)**
1. *App registrations → New registration*, poi *Certificates & secrets → New client secret*.
2. *API permissions → Add → Dynamics 365 Business Central → Application permissions →*
   `Automation.ReadWrite.All` → **Grant admin consent**.

**In Business Central**, per ogni environment:
1. Pagina *Microsoft Entra Applications* → nuovo record con il Client ID, stato *Enabled*.
2. Assegna i permission set `D365 AUTOMATION` ed `EXTEN. MGT. - ADMIN`.

**Nell'app:** *Connessioni → Nuova connessione*. Inserisci cliente, environment, tenant, client ID e
client secret, poi premi **Testa**. Il client secret viene cifrato e non si vede più.

**Altri environment dello stesso cliente:** sulla connessione premi **Copia**, scrivi il nuovo environment e
salva. Tenant, client ID, client secret e notifiche restano quelli dell'originale, e il nome si compila da solo.
Le connessioni sono raggruppate per cliente, sia nella lista sia nella scelta del deploy.

## Notifiche

A fine deploy l'app può avvisare via **email**, **Teams** o **Slack**. Si sceglie per ogni connessione,
nella sezione *Notifiche*, se ricevere tutti gli esiti o solo gli errori.

**Email:** la prima volta vai in *Impostazioni* e inserisci il server di posta (SMTP), poi premi
**Invia email di prova**. Per Office 365 i dati sono:

| Campo | Valore |
|---|---|
| Server | `smtp.office365.com` |
| Porta | `587` |
| Sicurezza | STARTTLS |
| Utente | il tuo indirizzo email |
| Password | la tua password, oppure una password per le app se hai la verifica in due passaggi |

Se la prova risponde con un errore di autenticazione (`535 5.7.139`), l'invio SMTP è disattivato per il tuo
account: chiedi all'IT di attivare *SMTP AUTH* o un server di posta interno.

Poi, in ogni connessione, scrivi i destinatari separati da virgola.

## Fare un deploy

1. *Deploy* → scegli la connessione e trascina i file `.app`.
2. Controlla l'ordine di installazione e la verifica sull'environment.
3. Scegli le opzioni:
   - **Schema sync mode**: *Add* (normale) oppure *Force Sync*, che applica anche modifiche che cancellano dati;
   - **Schedule**: *versione corrente* (subito) oppure *prossima minor / major*, cioè quando l'environment
     si aggiornerà a quella versione;
   - **Quando**: subito, a una data e ora, oppure ricorrente.
4. Avvia. Per un environment di produzione devi scriverne il nome per confermare.

Se un'app fallisce, il deploy si ferma e le successive vengono saltate. Il dettaglio dell'errore è nella pagina
*Extension Deployment Status* di BC. Da *Storico* puoi rivedere ogni deploy e rilanciarlo.

## Passare l'app a un collega

1. Doppio clic su **`Crea-pacchetto.bat`**: crea `bc-deployer.zip` sul Desktop **senza** i tuoi dati
   (password, chiave e connessioni).
2. Manda lo zip.
3. Il collega:
   1. lo estrae;
   2. lancia `Installa.bat`, preme **Invio** alla domanda sulla chiave e sceglie la sua password;
   3. lancia `Avvia.bat` e crea le sue connessioni.

Ognuno ha i suoi dati. Non mandare mai il tuo `.env` e non mandare chiavi o client secret per chat o email.

Al collega servono:
- Python 3.12 o più recente (`py -0` mostra le versioni);
- internet per scaricare le librerie;
- accesso a `login.microsoftonline.com` e `api.businesscentral.dynamics.com`.

## Dove sono i dati

| Cosa | Dove |
|---|---|
| password (cifrata) e chiave | `.env` nella cartella dell'app |
| connessioni, impostazioni email, storico, deploy programmati, pacchetti | `%LOCALAPPDATA%\bcdeployer-data` |
| Python e librerie | `%LOCALAPPDATA%\bcdeployer-venv` |

**Backup:** ad app chiusa, copia il file `.env` e la cartella `%LOCALAPPDATA%\bcdeployer-data`.

## Problemi comuni

| Cosa vedi | Cosa fare |
|---|---|
| *Python non trovato* | serve il comando `py`, versione 3.12 o più recente |
| errore nello scaricare le librerie (`ProxyError`, `SSL`) | il proxy aziendale blocca `pypi.org`: chiedi all'IT |
| *Windows blocca alcune librerie* | su quel PC l'app non può girare: serve un server (vedi sotto) |
| *Prima esegui Installa.bat* | lancia `Installa.bat` |
| il browser non trova la pagina | l'app non è aperta: lancia `Avvia.bat` |
| la finestra si chiude subito | aprila da PowerShell, nella cartella, con `.\Avvia.bat`, per leggere l'errore |
| password dimenticata | rilancia `Installa.bat` e cambiala |
| *Impossibile decifrare il segreto* | la chiave in `.env` è sbagliata: rilancia `Installa.bat` e incolla quella giusta |
| le email non arrivano | apri il deploy nello *Storico*: in fondo al log c'è l'esito dell'invio. Prova il server da *Impostazioni* |

---

## Per chi sviluppa

**Configurazione.** Tutto sta in `.env`, scritto da `Installa.bat`. Le altre opzioni (dimensione massima
dei file, giorni di conservazione dei pacchetti, timeout) sono in [.env.example](.env.example).

**Su un server.** Per tenere l'app sempre accesa o usarla da più PC, segui una di queste guide:
- [Azure App Service, passo passo](docs/guida-pc-aziendale.md);
- [Azure con container](docs/deploy-azure.md);
- [Render o Fly.io](docs/deploy-render-fly.md).

Deve girare con un solo processo.

**Da riga di comando.** `bc_deploy.py` fa lo stesso deploy senza interfaccia, anche da
[azure-pipelines.yml](azure-pipelines.yml):

```bash
python bc_deploy.py --environment Sandbox --apps ./apps --schema-sync "Force Sync" --schedule "Next minor version"
```

Le credenziali si passano con le variabili `BC_TENANT_ID`, `BC_CLIENT_ID` e `BC_CLIENT_SECRET`.

**Test:**

```powershell
py -m venv $env:LOCALAPPDATA\bcdeployer-venv-dev
& $env:LOCALAPPDATA\bcdeployer-venv-dev\Scripts\python -m pip install -r requirements-dev.txt
& $env:LOCALAPPDATA\bcdeployer-venv-dev\Scripts\python -m pytest
```

**Note:**
- Lo stato di un deploy in BC restituisce solo l'esito (riuscito o fallito), senza il messaggio d'errore.
- Per passare all'Admin Center API basta implementare l'interfaccia `DeployBackend` in
  [app/bc/base.py](app/bc/base.py).

## Licenza

Software libero, con licenza [BSD 3-Clause](LICENSE): puoi usarlo, modificarlo e ridistribuirlo, anche per
scopi commerciali, mantenendo l'avviso di copyright. Viene fornito così com'è, senza garanzie.

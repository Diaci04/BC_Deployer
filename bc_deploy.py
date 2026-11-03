#!/usr/bin/env python3
"""Installa/aggiorna estensioni .app su Business Central SaaS tramite Automation API (versione CLI).

Usa lo stesso modulo app.bc della web app. Credenziali da variabili d'ambiente:
  BC_TENANT_ID, BC_CLIENT_ID, BC_CLIENT_SECRET

Uso:
  python bc_deploy.py --environment Production --apps ./apps
  python bc_deploy.py --environment Sandbox --apps ./apps --schema-sync "Force Sync" --dry-run
"""
import argparse
import math
import os
import sys
import time
from pathlib import Path

from app.bc.automation import AutomationBackend
from app.bc.base import Schedule, SchemaSync
from app.bc.http import BCError, BCHttp
from app.bc.manifest import ManifestError, read_manifest
from app.bc.ordering import OrderingError, order_by_dependencies
from app.bc.polling import wait_for_deploy
from app.bc.precheck import Action, precheck


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _choice(enum, value: str):
    # Accetta anche le grafie della prima versione dello script ("Current Version")
    for member in enum:
        if member.value.lower() == value.lower():
            return member
    raise argparse.ArgumentTypeError(f"valori ammessi: {', '.join(m.value for m in enum)}")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--environment", required=True)
    p.add_argument("--apps", required=True, help="cartella con i .app")
    p.add_argument("--company", help="nome company (default: la prima)")
    p.add_argument("--schema-sync", default="Add", type=lambda v: _choice(SchemaSync, v), metavar="Add|Force Sync")
    p.add_argument("--schedule", default=Schedule.CURRENT.value, type=lambda v: _choice(Schedule, v),
                   metavar='"Current version"|"Next minor version"|"Next major version"')
    p.add_argument("--timeout", type=int, default=1200, help="secondi massimi di attesa per app")
    p.add_argument("--dry-run", action="store_true", help="mostra solo l'ordine, senza chiamare BC")
    a = p.parse_args()

    files = sorted(Path(a.apps).glob("*.app"))
    if not files:
        log(f"Nessun .app in {a.apps}")
        return 1
    try:
        by_id = {}
        for f in files:
            m = read_manifest(f)
            by_id[m.id] = (m, f)
        ordered = order_by_dependencies([m for m, _ in by_id.values()])
    except (ManifestError, OrderingError) as e:
        log(f"ERRORE: {e}")
        return 1

    log("Ordine di installazione:")
    for m in ordered:
        log(f"  {m.name} {m.version} ({by_id[m.id][1].name})")
    if a.dry_run:
        return 0

    try:
        creds = (os.environ["BC_TENANT_ID"], os.environ["BC_CLIENT_ID"], os.environ["BC_CLIENT_SECRET"])
    except KeyError as e:
        log(f"Variabile d'ambiente mancante: {e.args[0]}")
        return 1

    with BCHttp(*creds, a.environment, on_log=log) as http:
        backend = AutomationBackend(http, a.company, on_log=log)
        try:
            check = precheck(ordered, backend.list_apps())
            for plan in check.plans:
                log(f"  {plan.manifest.label}: {plan.action_label}")
            if not check.ok:
                for problem in check.problems:
                    log(f"ERRORE: {problem}")
                return 1

            for plan in check.plans:
                m = plan.manifest
                if plan.action == Action.SAME_VERSION:
                    continue
                log(f"Upload {m.label}...")
                handle = backend.upload_and_install(m, by_id[m.id][1], a.schema_sync, a.schedule)
                if a.schedule != Schedule.CURRENT:
                    log(f"  programmata ({a.schedule.value})")
                    continue
                ok, message = wait_for_deploy(
                    backend, handle, timeout_minutes=max(1, math.ceil(a.timeout / 60)), sleep=time.sleep,
                    on_change=lambda st: log(f"  stato: {st.raw_status or 'in attesa'}"),
                )
                if not ok:
                    log(f"ERRORE: {m.name} -> {message}")
                    return 1  # le app successive potrebbero dipendere da questa
                log(f"OK {m.name}")
        except BCError as e:
            log(f"ERRORE: {e}")
            return 1
    log("Deploy completato.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

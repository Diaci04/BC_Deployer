"""Verifiche prima del deploy, confrontando il set caricato con le app presenti nell'environment.

- stessa versione già installata → l'app viene saltata;
- versione più bassa di quella installata → bloccante (BC non supporta il downgrade di una PTE);
- dipendenze fuori dal set: devono essere installate con versione ≥ MinVersion;
- dipendenze dentro il set: la versione caricata deve soddisfare MinVersion;
- Application del manifest ≤ versione della Base Application installata.
"""
from dataclasses import dataclass, field
from enum import StrEnum

from .base import InstalledApp
from .manifest import AppManifest
from .version import Version

BASE_APP_ID = "437dbf0e-84ff-417a-965d-ed2bb9650972"


class Action(StrEnum):
    INSTALL = "install"
    UPGRADE = "upgrade"
    SAME_VERSION = "same_version"
    DOWNGRADE = "downgrade"


ACTION_LABELS = {
    Action.INSTALL: "Nuova installazione",
    Action.UPGRADE: "Aggiornamento",
    Action.SAME_VERSION: "Già installata: verrà saltata",
    Action.DOWNGRADE: "Downgrade: non consentito",
}


@dataclass
class AppPlan:
    manifest: AppManifest
    action: Action
    installed_version: Version | None = None

    @property
    def action_label(self) -> str:
        return ACTION_LABELS[self.action]


@dataclass
class PrecheckResult:
    plans: list[AppPlan] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems

    def action_for(self, app_id: str) -> Action | None:
        return next((p.action for p in self.plans if p.manifest.id == app_id), None)


def precheck(ordered: list[AppManifest], environment_apps: list[InstalledApp]) -> PrecheckResult:
    installed = {a.id: a for a in environment_apps if a.is_installed}
    in_set = {m.id: m for m in ordered}
    res = PrecheckResult()

    for m in ordered:
        cur = installed.get(m.id)
        if cur is None:
            action = Action.INSTALL
        elif m.version > cur.version:
            action = Action.UPGRADE
        elif m.version == cur.version:
            action = Action.SAME_VERSION
        else:
            action = Action.DOWNGRADE
            res.problems.append(
                f"{m.name}: versione caricata {m.version} inferiore a quella installata {cur.version}"
            )
        res.plans.append(AppPlan(m, action, cur.version if cur else None))
        if action == Action.SAME_VERSION:
            continue

        for d in m.dependencies:
            dep_label = d.name or d.id
            if d.id in in_set:
                if in_set[d.id].version < d.min_version:
                    res.problems.append(
                        f"{m.name} richiede {dep_label} ≥ {d.min_version}, ma nel set c'è {in_set[d.id].version}"
                    )
            elif d.id in installed:
                if installed[d.id].version < d.min_version:
                    res.problems.append(
                        f"{m.name} richiede {dep_label} ≥ {d.min_version}, installata {installed[d.id].version}"
                    )
            else:
                res.problems.append(
                    f"{m.name} richiede {dep_label} ({d.publisher}) ≥ {d.min_version}, "
                    "non installata e non presente tra i file caricati"
                )

        base = installed.get(BASE_APP_ID)
        if m.application and base and base.version < m.application:
            res.problems.append(
                f"{m.name} richiede Base Application ≥ {m.application}, l'environment ha {base.version}"
            )
    return res

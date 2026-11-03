"""Ordinamento delle app per dipendenze (le dipendenze presenti nel set vanno prima).

Rispetto a order_by_dependencies di bc_deploy.py: errori come eccezioni (non SystemExit),
rifiuto di due pacchetti con lo stesso app id, ordine stabile (a parità, alfabetico)
e indicazione del ciclo trovato.
"""
from .manifest import AppManifest


class OrderingError(Exception):
    pass


def order_by_dependencies(apps: list[AppManifest]) -> list[AppManifest]:
    by_id: dict[str, AppManifest] = {}
    for a in apps:
        if a.id in by_id:
            raise OrderingError(
                f"Due pacchetti con lo stesso app id: {by_id[a.id].label} e {a.label}. Tienine uno solo."
            )
        by_id[a.id] = a

    ordered: list[AppManifest] = []
    state: dict[str, int] = {}  # 1 = in visita, 2 = completata
    stack: list[AppManifest] = []

    def visit(a: AppManifest) -> None:
        if state.get(a.id) == 2:
            return
        if state.get(a.id) == 1:
            cycle = stack[stack.index(a):] + [a]
            raise OrderingError("Dipendenza circolare: " + " → ".join(x.name for x in cycle))
        state[a.id] = 1
        stack.append(a)
        for d in sorted(a.dependencies, key=lambda d: by_id[d.id].name.lower() if d.id in by_id else ""):
            if d.id in by_id:
                visit(by_id[d.id])
        stack.pop()
        state[a.id] = 2
        ordered.append(a)

    for a in sorted(apps, key=lambda a: (a.name.lower(), a.id)):
        visit(a)
    return ordered

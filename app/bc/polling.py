"""Attesa dell'esito di un deploy, con backoff. Usata dalla web app e dal CLI bc_deploy.py."""
from collections.abc import Callable

from .base import DeployBackend, DeployState, DeployStatus, UploadHandle

FIRST_DELAY = 5.0
MAX_DELAY = 30.0


def wait_for_deploy(
    backend: DeployBackend,
    handle: UploadHandle,
    *,
    timeout_minutes: int,
    sleep: Callable[[float], None],
    on_change: Callable[[DeployStatus], None] = lambda st: None,
) -> tuple[bool, str]:
    """Interroga lo stato con attesa crescente (5 s → 30 s) fino a esito o timeout.

    Restituisce (successo, messaggio d'errore). Il tempo è contato sommando le attese,
    così il comportamento è deterministico anche con una sleep finta nei test.
    """
    timeout = timeout_minutes * 60
    elapsed, delay, last = 0.0, FIRST_DELAY, object()
    while elapsed < timeout:
        sleep(delay)
        elapsed += delay
        delay = min(MAX_DELAY, delay * 1.5)
        st = backend.deploy_status(handle)
        if st.raw_status != last:
            on_change(st)
            last = st.raw_status
        if st.state == DeployState.COMPLETED:
            return True, ""
        if st.state == DeployState.FAILED:
            op = f" (operazione {st.operation_id})" if st.operation_id else ""
            return False, (
                st.message
                or f"BC ha segnalato '{st.raw_status}'{op}. L'Automation API non espone il dettaglio "
                "dell'errore: aprilo in Business Central nella pagina 'Extension Deployment Status'."
            )
    return False, (
        f"Nessun esito dopo {timeout_minutes} minuti. Il deploy potrebbe essere ancora in corso in BC: "
        "controlla la pagina 'Extension Deployment Status' prima di rilanciare."
    )

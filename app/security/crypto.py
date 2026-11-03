"""Cifratura a riposo dei segreti (client secret, URL dei webhook) con Fernet.

FERNET_KEYS contiene una o più chiavi separate da virgola: la prima cifra,
tutte decifrano. Per ruotare: aggiungi la nuova chiave in testa, riavvia,
esegui `python -m app.cli rotate-keys`, poi togli la vecchia.
"""
from cryptography.fernet import Fernet, InvalidToken, MultiFernet


class SecretBoxError(Exception):
    pass


class SecretBox:
    def __init__(self, keys: str):
        parts = [k.strip() for k in keys.split(",") if k.strip()]
        if not parts:
            raise SecretBoxError("FERNET_KEYS è vuota")
        try:
            self._f = MultiFernet([Fernet(k) for k in parts])
        except (ValueError, TypeError):
            # Non includere la chiave nel messaggio
            raise SecretBoxError("FERNET_KEYS contiene una chiave non valida") from None

    def encrypt(self, plaintext: str) -> str:
        return self._f.encrypt(plaintext.encode()).decode()

    def decrypt(self, token: str) -> str:
        try:
            return self._f.decrypt(token.encode()).decode()
        except InvalidToken:
            raise SecretBoxError(
                "Impossibile decifrare il segreto: FERNET_KEYS non contiene la chiave usata per cifrarlo"
            ) from None

    def rotate(self, token: str) -> str:
        return self._f.rotate(token.encode()).decode()

    @staticmethod
    def generate_key() -> str:
        return Fernet.generate_key().decode()

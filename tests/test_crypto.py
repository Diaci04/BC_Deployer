import pytest
from cryptography.fernet import Fernet

from app.security.crypto import SecretBox, SecretBoxError


def test_roundtrip_and_ciphertext_hides_plaintext():
    box = SecretBox(Fernet.generate_key().decode())
    token = box.encrypt("super-segreto")
    assert "super-segreto" not in token
    assert box.decrypt(token) == "super-segreto"


def test_same_plaintext_gives_different_ciphertexts():
    box = SecretBox(Fernet.generate_key().decode())
    assert box.encrypt("x") != box.encrypt("x")


def test_wrong_key_raises_without_leaking():
    k1, k2 = Fernet.generate_key().decode(), Fernet.generate_key().decode()
    token = SecretBox(k1).encrypt("s3cret")
    with pytest.raises(SecretBoxError) as exc:
        SecretBox(k2).decrypt(token)
    assert k1 not in str(exc.value) and k2 not in str(exc.value) and "s3cret" not in str(exc.value)


def test_invalid_or_empty_key():
    with pytest.raises(SecretBoxError, match="non valida"):
        SecretBox("non-una-chiave")
    with pytest.raises(SecretBoxError, match="vuota"):
        SecretBox(" , ")


def test_key_rotation():
    old, new = Fernet.generate_key().decode(), Fernet.generate_key().decode()
    token = SecretBox(old).encrypt("valore")
    rotated_box = SecretBox(f"{new},{old}")
    assert rotated_box.decrypt(token) == "valore"
    rotated = rotated_box.rotate(token)
    # Dopo la rotazione basta la nuova chiave
    assert SecretBox(new).decrypt(rotated) == "valore"

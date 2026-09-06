import os
import sys
import hashlib
import getpass
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.exceptions import InvalidTag

# Cryptographic parameters
SALT_SIZE = 16
ITERATIONS = 100000
KEY_LENGTH = 32

def derive_key(passphrase: str, salt: bytes) -> bytes:
    """
    Derives a 256-bit symmetric key from a passphrase and a salt using PBKDF2 HMAC-SHA256.
    """
    if isinstance(passphrase, str):
        passphrase_bytes = passphrase.encode('utf-8')
    else:
        passphrase_bytes = passphrase
    return hashlib.pbkdf2_hmac('sha256', passphrase_bytes, salt, ITERATIONS, KEY_LENGTH)

def encrypt_key(plaintext_key: bytes, passphrase: str) -> bytes:
    """
    Encrypts the plaintext key using AES-GCM with a key derived from the passphrase.
    Returns combined payload: salt (16 bytes) + nonce (12 bytes) + ciphertext.
    """
    salt = os.urandom(SALT_SIZE)
    derived_key = derive_key(passphrase, salt)
    aesgcm = AESGCM(derived_key)
    nonce = os.urandom(12)
    ciphertext = aesgcm.encrypt(nonce, plaintext_key, None)
    return salt + nonce + ciphertext

def decrypt_key(payload: bytes, passphrase: str) -> bytes:
    """
    Decrypts the combined payload (salt + nonce + ciphertext) using the passphrase.
    Returns the decrypted plaintext_key, or raises InvalidTag/ValueError on failure.
    """
    if len(payload) < SALT_SIZE + 12:
        raise ValueError("Invalid payload: too short")
    salt = payload[:SALT_SIZE]
    nonce = payload[SALT_SIZE:SALT_SIZE+12]
    ciphertext = payload[SALT_SIZE+12:]
    derived_key = derive_key(passphrase, salt)
    aesgcm = AESGCM(derived_key)
    return aesgcm.decrypt(nonce, ciphertext, None)

def get_passphrase(prompt: str, confirm: bool = False, override_passphrase: str = None) -> str:
    """
    Gets a passphrase securely. Checks override_passphrase first, then the PEER_PASSPHRASE
    environment variable, and falls back to interactive getpass.getpass.
    """
    if override_passphrase is not None:
        return override_passphrase

    env_pass = os.environ.get("PEER_PASSPHRASE")
    if env_pass is not None:
        return env_pass

    # Check if we are in a non-interactive environment
    if not sys.stdin.isatty():
        raise RuntimeError(
            "Non-interactive environment detected, but no passphrase provided via argument or PEER_PASSPHRASE env var."
        )

    if confirm:
        while True:
            pass1 = getpass.getpass(prompt)
            pass2 = getpass.getpass("Confirm passphrase: ")
            if pass1 == pass2:
                if not pass1:
                    print("Passphrase cannot be empty. Please try again.")
                    continue
                return pass1
            print("Passphrases do not match. Please try again.")
    else:
        return getpass.getpass(prompt)

def secure_delete_file(filepath: str):
    """
    Securely overwrites a file with random bytes and then zero bytes before deleting it.
    """
    if not os.path.exists(filepath):
        return
    try:
        size = os.path.getsize(filepath)
        # Open and overwrite with random bytes
        with open(filepath, "ba+", buffering=0) as f:
            f.write(os.urandom(size))
            f.flush()
            os.fsync(f.fileno())
        # Open and overwrite with zero bytes
        with open(filepath, "ba+", buffering=0) as f:
            f.write(b"\x00" * size)
            f.flush()
            os.fsync(f.fileno())
    except Exception:
        pass
    finally:
        try:
            os.remove(filepath)
        except Exception:
            pass

def clean_legacy_bytes(data: bytes) -> bytes:
    """
    Strips trailing newlines/carriage returns often added by editors or echo commands.
    """
    return data.rstrip(b"\r\n")

def load_or_create_peer_identity(
    encrypted_path: str = "/app/peer_key.enc",
    legacy_paths: list = None,
    passphrase_input: str = None
) -> bytes:
    """
    Handles loading, creating, or migrating the peer identity private key.
    - If a legacy file is found, prompts for a new passphrase, encrypts it, writes to encrypted_path,
      securely overwrites/deletes the legacy file, and returns the decrypted key.
    - If no key exists, generates a 32-byte key, prompts for a new passphrase, encrypts, writes, and returns the key.
    - If an encrypted key exists, prompts for the passphrase, decrypts, and returns the key.
    """
    if legacy_paths is None:
        legacy_paths = ["/app/peer_key.plain", "/app/peer_key.txt"]

    # 1. Check for legacy migration first
    legacy_found = None
    for path in legacy_paths:
        if os.path.exists(path) and os.path.getsize(path) > 0:
            legacy_found = path
            break

    if legacy_found:
        print(f"Legacy unencrypted key file found at: {legacy_found}")
        print("Migrating key to secure, encrypted format...")
        # Read the legacy key bytes
        with open(legacy_found, "rb") as f:
            raw_data = f.read()
        legacy_key_bytes = clean_legacy_bytes(raw_data)

        # Prompt for a new passphrase with confirmation
        passphrase = get_passphrase(
            prompt="Enter a new passphrase to secure your migrated private key: ",
            confirm=True,
            override_passphrase=passphrase_input
        )

        # Encrypt legacy key bytes
        payload = encrypt_key(legacy_key_bytes, passphrase)

        # Write encrypted file
        with open(encrypted_path, "wb") as f:
            f.write(payload)

        # Securely overwrite and delete legacy file
        secure_delete_file(legacy_found)
        print("Legacy key successfully secured, encrypted, and plaintext file deleted from disk.")
        return legacy_key_bytes

    # 2. Check if encrypted file exists
    if os.path.exists(encrypted_path):
        # Prompt for passphrase (no confirmation required)
        passphrase = get_passphrase(
            prompt="Enter passphrase to decrypt your private key: ",
            confirm=False,
            override_passphrase=passphrase_input
        )

        with open(encrypted_path, "rb") as f:
            payload = f.read()

        try:
            plaintext_key = decrypt_key(payload, passphrase)
            return plaintext_key
        except (InvalidTag, ValueError) as e:
            raise DecryptionError("Decryption failed: Incorrect passphrase or corrupted payload.") from e

    # 3. No key exists, generate a new one
    print("No existing peer identity key found. Generating a new peer key...")
    plaintext_key = os.urandom(32)

    # Prompt for a new passphrase with confirmation
    passphrase = get_passphrase(
        prompt="Enter a new passphrase to secure your new private key: ",
        confirm=True,
        override_passphrase=passphrase_input
    )

    payload = encrypt_key(plaintext_key, passphrase)

    # Write to disk
    with open(encrypted_path, "wb") as f:
        f.write(payload)

    print(f"New encrypted private key successfully written to {encrypted_path}.")
    return plaintext_key


class DecryptionError(Exception):
    """Raised when decryption fails due to incorrect passphrase or corrupted data."""
    pass

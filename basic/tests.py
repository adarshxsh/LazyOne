import os
import tempfile
import unittest
from unittest.mock import patch
from cryptography.exceptions import InvalidTag

from django.test import TestCase

from basic.peer_identity import (
    derive_key,
    encrypt_key,
    decrypt_key,
    get_passphrase,
    secure_delete_file,
    clean_legacy_bytes,
    load_or_create_peer_identity,
    DecryptionError
)

class PeerIdentityTests(TestCase):
    def setUp(self):
        # Create a temporary directory for file operations during tests
        self.test_dir = tempfile.TemporaryDirectory()
        self.enc_path = os.path.join(self.test_dir.name, "peer_key.enc")
        self.legacy_plain_path = os.path.join(self.test_dir.name, "peer_key.plain")
        self.legacy_txt_path = os.path.join(self.test_dir.name, "peer_key.txt")
        self.legacy_paths = [self.legacy_plain_path, self.legacy_txt_path]

    def tearDown(self):
        self.test_dir.cleanup()

    def test_key_derivation(self):
        passphrase = "my_secure_password"
        salt = os.urandom(16)
        key1 = derive_key(passphrase, salt)
        key2 = derive_key(passphrase, salt)
        
        # 1. Output must be exactly 32 bytes (256-bit key)
        self.assertEqual(len(key1), 32)
        # 2. Output must be deterministic
        self.assertEqual(key1, key2)

        # 3. Different salt must yield different key
        salt2 = os.urandom(16)
        key3 = derive_key(passphrase, salt2)
        self.assertNotEqual(key1, key3)

        # 4. Different passphrase must yield different key
        key4 = derive_key("another_password", salt)
        self.assertNotEqual(key1, key4)

    def test_encryption_decryption(self):
        plaintext = b"secret_peer_private_key_bytes_12345"
        passphrase = "super_safe_password"

        # Encrypt
        payload = encrypt_key(plaintext, passphrase)
        
        # Salt size is 16, Nonce is 12, overhead is 16 for AES-GCM tag
        expected_min_len = 16 + 12 + len(plaintext) + 16
        self.assertGreaterEqual(len(payload), expected_min_len)

        # Decrypt
        decrypted = decrypt_key(payload, passphrase)
        self.assertEqual(decrypted, plaintext)

        # Decrypt with wrong passphrase must raise exception
        with self.assertRaises((InvalidTag, ValueError)):
            decrypt_key(payload, "wrong_password")

        # Decrypt with corrupted payload
        corrupted_payload = bytearray(payload)
        corrupted_payload[-1] ^= 0xFF  # Corrupt the last byte
        with self.assertRaises((InvalidTag, ValueError)):
            decrypt_key(bytes(corrupted_payload), passphrase)

    def test_secure_delete_file(self):
        # Create a file with sensitive data
        test_file = os.path.join(self.test_dir.name, "temp_sensitive.txt")
        sensitive_data = b"very_sensitive_data_on_disk"
        with open(test_file, "wb") as f:
            f.write(sensitive_data)
        
        self.assertTrue(os.path.exists(test_file))
        secure_delete_file(test_file)
        
        # File should no longer exist
        self.assertFalse(os.path.exists(test_file))

    def test_get_passphrase_env_override(self):
        # When PEER_PASSPHRASE is in the environment, it should be used immediately
        with patch.dict(os.environ, {"PEER_PASSPHRASE": "env_secret"}):
            passphrase = get_passphrase("Prompt: ", confirm=False)
            self.assertEqual(passphrase, "env_secret")

    def test_get_passphrase_arg_override(self):
        # When override_passphrase is provided as an argument, it should be used immediately
        passphrase = get_passphrase("Prompt: ", confirm=False, override_passphrase="arg_secret")
        self.assertEqual(passphrase, "arg_secret")

    @patch("getpass.getpass")
    @patch("sys.stdin.isatty", return_value=True)
    def test_get_passphrase_interactive_no_confirm(self, mock_isatty, mock_getpass):
        mock_getpass.return_value = "interactive_secret"
        passphrase = get_passphrase("Enter: ", confirm=False)
        self.assertEqual(passphrase, "interactive_secret")
        mock_getpass.assert_called_once_with("Enter: ")

    @patch("getpass.getpass")
    @patch("sys.stdin.isatty", return_value=True)
    def test_get_passphrase_interactive_confirm(self, mock_isatty, mock_getpass):
        # Simulate different passwords then matching passwords
        mock_getpass.side_effect = ["pass1", "pass2", "pass_match", "pass_match"]
        passphrase = get_passphrase("Enter: ", confirm=True)
        self.assertEqual(passphrase, "pass_match")
        self.assertEqual(mock_getpass.call_count, 4)

    @patch("sys.stdin.isatty", return_value=False)
    def test_get_passphrase_non_interactive_no_env_raises_error(self, mock_isatty):
        # If in non-interactive environment and no env/arg, raise RuntimeError
        with self.assertRaises(RuntimeError):
            get_passphrase("Enter: ", confirm=False)

    def test_first_time_setup_flow(self):
        # No files exist initially
        self.assertFalse(os.path.exists(self.enc_path))
        
        # Run setup non-interactively by supplying passphrase_input
        ret_key = load_or_create_peer_identity(
            encrypted_path=self.enc_path,
            legacy_paths=self.legacy_paths,
            passphrase_input="test_password"
        )
        
        # 1. Returned key must be 32 bytes
        self.assertEqual(len(ret_key), 32)
        # 2. Encrypted file must now exist
        self.assertTrue(os.path.exists(self.enc_path))
        
        # Verify the file actually contains the ciphertext (not raw key bytes)
        with open(self.enc_path, "rb") as f:
            ciphertext_content = f.read()
        self.assertNotIn(ret_key, ciphertext_content)
        
        # 3. Decrypting the created file with the same passphrase should return the same key
        decrypted_key = decrypt_key(ciphertext_content, "test_password")
        self.assertEqual(decrypted_key, ret_key)

    def test_interactive_startup_flow(self):
        # Pre-seed encrypted file
        plaintext_key = os.urandom(32)
        payload = encrypt_key(plaintext_key, "correct_password")
        with open(self.enc_path, "wb") as f:
            f.write(payload)
            
        # Startup with correct password
        ret_key = load_or_create_peer_identity(
            encrypted_path=self.enc_path,
            legacy_paths=self.legacy_paths,
            passphrase_input="correct_password"
        )
        self.assertEqual(ret_key, plaintext_key)
        
        # Startup with incorrect password must fail with DecryptionError
        with self.assertRaises(DecryptionError):
            load_or_create_peer_identity(
                encrypted_path=self.enc_path,
                legacy_paths=self.legacy_paths,
                passphrase_input="wrong_password"
            )

    def test_legacy_migration_flow_plain(self):
        # Pre-seed legacy plaintext key (with typical trailing newlines)
        legacy_key = b"legacy_secret_key_bytes_abc_123"
        with open(self.legacy_plain_path, "wb") as f:
            f.write(legacy_key + b"\n")
            
        self.assertTrue(os.path.exists(self.legacy_plain_path))
        self.assertFalse(os.path.exists(self.enc_path))
        
        # Run loading with migration
        ret_key = load_or_create_peer_identity(
            encrypted_path=self.enc_path,
            legacy_paths=self.legacy_paths,
            passphrase_input="migration_passphrase"
        )
        
        # 1. Returned key should be the cleaned legacy key (without the trailing newline)
        self.assertEqual(ret_key, legacy_key)
        
        # 2. Legacy unencrypted file must have been deleted
        self.assertFalse(os.path.exists(self.legacy_plain_path))
        
        # 3. Encrypted file must have been created and contain correct encrypted key
        self.assertTrue(os.path.exists(self.enc_path))
        with open(self.enc_path, "rb") as f:
            payload = f.read()
        self.assertNotIn(legacy_key, payload)
        
        decrypted_key = decrypt_key(payload, "migration_passphrase")
        self.assertEqual(decrypted_key, legacy_key)

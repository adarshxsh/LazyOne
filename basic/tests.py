import os
import tempfile
import unittest
from unittest.mock import patch
from datetime import timedelta
from cryptography.exceptions import InvalidTag

from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone

from .models import UserProfile, Task, Dispute, RewardLedger, Conversation
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


class DisputeDepositBondTests(TestCase):
    def setUp(self):
        self.client = Client()

        # Task poster
        self.poster = User.objects.create_user(username='poster', password='password123')
        self.poster_profile = UserProfile.objects.create(user=self.poster, rewards=1000)

        # Task taker
        self.taker = User.objects.create_user(username='taker', password='password123')
        self.taker_profile = UserProfile.objects.create(user=self.taker, rewards=100)

        # Create task: reward = 300, 20% = 60 (> 50 minimum)
        self.deadline = timezone.now() + timedelta(days=2)
        self.task = Task.objects.create(
            title="Test Task",
            description="Test Description",
            reward=300,
            posted_by=self.poster,
            taken_by=self.taker,
            status='in_progress',
            deadline=self.deadline
        )
        Conversation.objects.create(task=self.task)

        # Create small reward task: reward = 100, 20% = 20 (min 50 applies)
        self.small_task = Task.objects.create(
            title="Small Task",
            description="Small Description",
            reward=100,
            posted_by=self.poster,
            taken_by=self.taker,
            status='in_progress',
            deadline=self.deadline
        )
        Conversation.objects.create(task=self.small_task)

    def test_deposit_bond_calculation(self):
        # 20% of 300 = 60 (> 50)
        self.assertEqual(self.task.deposit_bond_amount, 60)
        # 20% of 100 = 20 (< 50, so minimum 50 applies)
        self.assertEqual(self.small_task.deposit_bond_amount, 50)

    def test_raise_dispute_insufficient_rewards(self):
        # Set taker rewards to 30 (less than 60 required)
        self.taker_profile.rewards = 30
        self.taker_profile.save()

        self.client.login(username='taker', password='password123')
        response = self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Work not clear'}
        )

        self.assertRedirects(response, reverse('my_tasks'))
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'in_progress')
        self.assertFalse(Dispute.objects.filter(task=self.task).exists())

        # Balance should remain unchanged
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 30)

    def test_raise_dispute_success(self):
        self.client.login(username='taker', password='password123')
        response = self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Unreasonable request'}
        )

        # Deposit bond is 60. Taker balance was 100 -> now 40
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 40)

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'disputed')

        dispute = Dispute.objects.get(task=self.task)
        self.assertEqual(dispute.deposit_amount, 60)
        self.assertEqual(dispute.escrow_status, 'held')
        self.assertEqual(dispute.status, 'open')
        self.assertEqual(dispute.raised_by, self.taker)

        # Check ledger
        ledger = RewardLedger.objects.filter(user=self.taker, transaction_type='dispute_deposit').first()
        self.assertIsNotNone(ledger)
        self.assertEqual(ledger.amount, -60)

    def test_withdraw_dispute_success(self):
        # First raise dispute
        self.client.login(username='taker', password='password123')
        self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Dispute reason'}
        )

        dispute = Dispute.objects.get(task=self.task)

        # Withdraw dispute
        response = self.client.post(reverse('withdraw_dispute', args=[dispute.id]))
        self.assertRedirects(response, reverse('my_tasks'))

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'in_progress')

        dispute.refresh_from_db()
        self.assertEqual(dispute.escrow_status, 'refunded')
        self.assertEqual(dispute.status, 'resolved')

        # Balance restored: 40 + 60 = 100
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 100)

        # Check refund ledger
        ledger = RewardLedger.objects.filter(user=self.taker, transaction_type='dispute_refund').first()
        self.assertIsNotNone(ledger)
        self.assertEqual(ledger.amount, 60)

    def test_complete_disputed_task_refunds_deposit(self):
        # Taker raises dispute (deposit 60 deducted from 100 -> 40 left)
        self.client.login(username='taker', password='password123')
        self.client.post(
            reverse('raise_dispute', args=[self.task.id]),
            {'reason': 'Dispute reason'}
        )

        # Poster marks task as completed
        self.client.login(username='poster', password='password123')
        response = self.client.get(reverse('complete_task', args=[self.task.id]))
        self.assertRedirects(response, reverse('my_tasks'))

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'completed')

        dispute = Dispute.objects.get(task=self.task)
        self.assertEqual(dispute.escrow_status, 'refunded')
        self.assertEqual(dispute.status, 'resolved')

        # Taker balance: 40 + 300 (task reward) + 60 (deposit refund) = 400
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 400)

        # Check ledger entries for taker
        refund_ledger = RewardLedger.objects.filter(user=self.taker, transaction_type='dispute_refund').first()
        self.assertIsNotNone(refund_ledger)
        self.assertEqual(refund_ledger.amount, 60)

    def test_forfeit_deposit_method(self):
        dispute = Dispute.objects.create(
            task=self.task,
            raised_by=self.taker,
            reason='False dispute',
            deposit_amount=60,
            escrow_status='held'
        )
        self.taker_profile.rewards = 40
        self.taker_profile.save()

        # Forfeit deposit bond to poster
        dispute.forfeit_deposit(beneficiary=self.poster)

        dispute.refresh_from_db()
        self.assertEqual(dispute.escrow_status, 'forfeited')

        # Taker rewards remain 40 (already deducted when raised)
        self.taker_profile.refresh_from_db()
        self.assertEqual(self.taker_profile.rewards, 40)

        # Poster gets 1000 + 60 = 1060
        self.poster_profile.refresh_from_db()
        self.assertEqual(self.poster_profile.rewards, 1060)

        # Check forfeit ledger
        forfeit_ledger = RewardLedger.objects.filter(user=self.taker, transaction_type='dispute_forfeit').first()
        self.assertIsNotNone(forfeit_ledger)


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

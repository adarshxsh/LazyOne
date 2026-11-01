from django.db import IntegrityError, transaction
from django.test import TestCase

from ledger import services as ledger
from ledger.models import Account, Transfer

from .base import make_user


class LedgerTests(TestCase):
    def setUp(self):
        self.a = make_user('a', 500)
        self.b = make_user('b', 100)

    def test_wallet_opened_from_profile_points(self):
        self.assertEqual(ledger.balance_of(self.a), 500)
        self.assertEqual(ledger.check_invariants(), [])

    def test_transfer_moves_and_mirrors_profile(self):
        ledger.transfer([(ledger.user_account(self.a), -200), (ledger.user_account(self.b), 200)],
                        key='t1', reason=Transfer.Reason.TASK_PAYOUT)
        self.assertEqual(self.a.userprofile.__class__.objects.get(user=self.a).rewards, 300)
        self.assertEqual(ledger.balance_of(self.b), 300)
        self.assertEqual(ledger.check_invariants(), [])

    def test_idempotent_key_moves_once(self):
        legs = [(ledger.user_account(self.a), -50), (ledger.user_account(self.b), 50)]
        ledger.transfer(legs, key='same', reason=Transfer.Reason.TASK_PAYOUT)
        ledger.transfer(legs, key='same', reason=Transfer.Reason.TASK_PAYOUT)
        self.assertEqual(ledger.balance_of(self.b), 150)

    def test_overdraft_rejected(self):
        with self.assertRaises(ledger.InsufficientFunds):
            ledger.transfer([(ledger.user_account(self.b), -101), (ledger.user_account(self.a), 101)],
                            key='x', reason=Transfer.Reason.TASK_PAYOUT)
        self.assertEqual(ledger.balance_of(self.b), 100)

    def test_unbalanced_rejected(self):
        with self.assertRaises(ValueError):
            ledger.transfer([(ledger.user_account(self.a), -10)], key='y', reason=Transfer.Reason.TASK_PAYOUT)

    def test_db_constraint_blocks_negative_balance(self):
        acc = ledger.user_account(self.a)
        with self.assertRaises(IntegrityError), transaction.atomic():
            Account.objects.filter(pk=acc.pk).update(balance=-1)

    def test_check_invariants_detects_drift(self):
        Account.objects.filter(key=f"user:{self.a.pk}").update(balance=999)
        self.assertTrue(ledger.check_invariants())

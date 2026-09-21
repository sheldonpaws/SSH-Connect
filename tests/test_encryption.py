# -*- coding: utf-8 -*-
"""Тесты шифрования паролей (src/encryption.py)."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _bootstrap import ROOT  # noqa: E402,F401

from src import encryption  # noqa: E402


class TestEncryption(unittest.TestCase):
    def test_key_created_and_stable(self):
        k1 = encryption.get_or_create_key()
        k2 = encryption.get_or_create_key()
        self.assertTrue(k1)
        self.assertEqual(k1, k2)
        self.assertTrue(os.path.exists(encryption.KEY_FILE))

    def test_key_lives_outside_repo(self):
        # Ключ обязан лежать вне репозитория (%APPDATA%), иначе утечёт вместе с кодом.
        self.assertFalse(
            os.path.abspath(encryption.KEY_FILE).startswith(os.path.abspath(ROOT) + os.sep)
        )

    def test_roundtrip_unicode(self):
        secret = "пароль с пробелами & спецсимволами ' \" \\ / !"
        enc = encryption.encrypt_password(secret)
        self.assertNotEqual(enc, secret)
        self.assertEqual(encryption.decrypt_password(enc), secret)

    def test_empty_values(self):
        self.assertEqual(encryption.encrypt_password(""), "")
        self.assertEqual(encryption.decrypt_password(""), "")

    def test_field_aliases(self):
        # Fernet рандомизирован, поэтому сравниваем не шифротекст, а round-trip.
        self.assertEqual(encryption.decrypt_field(encryption.encrypt_field("x")), "x")
        enc = encryption.encrypt_password("y")
        self.assertEqual(encryption.decrypt_field(enc), "y")

    def test_invalid_ciphertext_returns_empty(self):
        self.assertEqual(encryption.decrypt_password("не-base64!!"), "")
        self.assertEqual(encryption.decrypt_password("YWJj"), "")

    def test_same_plaintext_different_ciphertext(self):
        a = encryption.encrypt_password("same")
        b = encryption.encrypt_password("same")
        self.assertNotEqual(a, b)
        self.assertEqual(encryption.decrypt_password(a), "same")
        self.assertEqual(encryption.decrypt_password(b), "same")


if __name__ == "__main__":
    unittest.main()

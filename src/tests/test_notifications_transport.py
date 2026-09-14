"""Real Web Push crypto with disposable keys and intercepted HTTP (no sends).

Run with the notification venv after installing notifications/requirements.txt.
The rest of the application intentionally has no Web Push dependency.
"""
import base64
from datetime import datetime, timedelta, timezone
import importlib.util
import json
import os
import unittest
from unittest.mock import patch

from src.notifications.scheduler import notification
from src.notifications.sender import WebPushTransport


HAS_WEBPUSH = importlib.util.find_spec("pywebpush") is not None


@unittest.skipUnless(HAS_WEBPUSH, "Install src/notifications/requirements.txt for real crypto tests")
class WebPushCryptoTests(unittest.TestCase):
    def setUp(self):
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives import serialization
        self.private = ec.generate_private_key(ec.SECP256R1())
        self.raw = self.encode(self.private.private_numbers().private_value.to_bytes(32, "big"))
        self.pem = self.private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                              serialization.NoEncryption()).decode()
        self.browser = ec.generate_private_key(ec.SECP256R1())
        self.auth = os.urandom(16)
        self.subscription = {"subscription": {
            "endpoint": "https://fcm.googleapis.com/fcm/send/synthetic-no-network",
            "keys": {"p256dh": self.encode(self.browser.public_key().public_bytes(
                serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)),
                     "auth": self.encode(self.auth)}}}
        self.now = datetime.now(timezone.utc)
        self.notice = notification("daily", "synthetic-test", self.now, self.now + timedelta(hours=1),
                                   [{"name": "Synthetic subject", "time": "07:30", "location": "Test room"}])
        self.calls = []
        self.status = 201

    @staticmethod
    def encode(value):
        return base64.urlsafe_b64encode(value).decode().rstrip("=")

    def respond(self, session, request, **kwargs):
        from requests import Response
        self.calls.append((request, kwargs))
        result = Response()
        result.status_code = self.status
        result._content = b"synthetic provider response"
        result.headers["Location"] = "https://example.invalid/never-follow-this"
        return result

    def send(self, private=None):
        transport = WebPushTransport(private or self.pem, "https://jdyece25-byte.github.io/schedule/")
        with patch("requests.sessions.Session.send", autospec=True, side_effect=self.respond):
            return transport.send(self.subscription, self.notice, self.now)

    def test_raw_and_pem_keys_encrypt_payload_and_sign_https_contact_origin(self):
        from http_ece import decrypt
        from py_vapid import Vapid
        for value in (self.raw, self.pem):
            with self.subTest(key_format="RAW" if value == self.raw else "PEM"):
                self.assertEqual(self.send(value), 201)
                request, options = self.calls[-1]
                self.assertFalse(options["allow_redirects"])
                self.assertEqual(request.headers["Content-Encoding"], "aes128gcm")
                self.assertNotIn(b"Synthetic subject", request.body)
                plaintext = decrypt(request.body, private_key=self.browser, auth_secret=self.auth)
                self.assertEqual(json.loads(plaintext), self.notice["payload"])
                self.assertTrue(Vapid.verify(request.headers["Authorization"]))
                encoded = request.headers["Authorization"].split("t=", 1)[1].split(",", 1)[0].split(".")[1]
                claims = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
                self.assertEqual(claims["sub"], "https://jdyece25-byte.github.io")
                self.assertEqual(claims["aud"], "https://fcm.googleapis.com")

    def test_redirect_is_blocked_and_nonaccepted_status_is_returned_without_logging(self):
        for status in (301, 403, 410, 429, 503):
            self.status = status
            before = len(self.calls)
            self.assertEqual(self.send(), status)
            self.assertEqual(len(self.calls), before + 1)
            self.assertFalse(self.calls[-1][1]["allow_redirects"])

    def test_expiry_bounds_provider_ttl(self):
        self.notice["expires"] = (self.now + timedelta(seconds=40)).isoformat()
        self.assertEqual(self.send(), 201)
        self.assertEqual(self.calls[-1][0].headers["TTL"], "40")

    def test_invalid_contact_url_is_rejected_before_signing_or_network(self):
        for subject in ("http://example.com/", "https://user:password@example.com/", "https://example.com/#private",
                        "https://example.com/?secret=private", "https://example.com:444/", "https://example.com/ spaced"):
            with self.subTest(subject=subject):
                with self.assertRaises(ValueError):
                    WebPushTransport(self.pem, subject)
        self.assertFalse(self.calls)


if __name__ == "__main__":
    unittest.main()

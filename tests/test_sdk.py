"""Run: python -m unittest discover -s tests (from sdks/python)."""

import base64
import json
import os
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from eza import Eza, EzaError, SignatureError, verify_webhook  # noqa: E402

# Signed by the Go gateway's webhooks.Sign: the SDKs must agree with it.
SECRET = "whsec_test_0123456789abcdef"
HEADER = "t=1790538762,v1=5ed06d16182620e9aee7fd9d3b02a369038ccbdd656de90a45b004c9fc51c52b"
BODY = '{"id":"evt_01","type":"payment.completed","livemode":false,"data":{"amount_kes":1500}}'
NOW = 1790538762 + 30


class Recorder:
    """A transport that records requests and plays back responses."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.seen = []

    def __call__(self, method, url, headers, body):
        self.seen.append((method, url, headers, json.loads(body) if body else None))
        status, hdrs, payload = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        return status, hdrs, json.dumps(payload).encode()


class Webhooks(unittest.TestCase):
    def test_accepts_the_gateway_signature(self):
        self.assertEqual(verify_webhook(BODY, HEADER, SECRET, now=NOW)["type"], "payment.completed")
        self.assertEqual(verify_webhook(BODY.encode(), HEADER, SECRET, now=NOW)["id"], "evt_01")

    def test_refuses_tampering_other_secrets_old_timestamps_and_junk(self):
        for body, header, secret, now in [
            (BODY.replace("1500", "15000"), HEADER, SECRET, NOW),
            (BODY, HEADER, "whsec_test_other", NOW),
            (BODY, HEADER, SECRET, NOW + 600),
            (BODY, "v1=abc", SECRET, NOW),
            (BODY, None, SECRET, NOW),
        ]:
            with self.assertRaises(SignatureError):
                verify_webhook(body, header, secret, now=now)


class Emails(unittest.TestCase):
    def test_send_posts_one_email_with_an_idempotency_key(self):
        t = Recorder((200, {}, {"id": "01928f3e"}))
        out = Eza(email_key="eza_live_mail", email_url="https://email.test/", transport=t).emails.send(
            from_="Shop <orders@mail.shop.co.ke>",
            to="wanjiku@gmail.com",
            subject="Your receipt",
            html="<p>Paid KES 1,500</p>",
            attachments=[{"filename": "r.txt", "content": b"KES 1,500"}],
        )
        self.assertEqual(out, {"id": "01928f3e"})
        method, url, headers, body = t.seen[0]
        self.assertEqual((method, url), ("POST", "https://email.test/emails"))
        self.assertEqual(headers["Authorization"], "Bearer eza_live_mail")
        self.assertEqual(len(headers["Idempotency-Key"]), 36)
        self.assertEqual(body["to"], ["wanjiku@gmail.com"])
        self.assertEqual(body["from"], "Shop <orders@mail.shop.co.ke>")
        self.assertEqual(body["attachments"][0]["content"], base64.b64encode(b"KES 1,500").decode())
        self.assertNotIn("cc", body)

    def test_on_eza_the_apps_settings_send_email_but_not_payedge(self):
        env = {"EZA_EMAIL_KEY": "eza_live_env", "EZA_EMAIL_URL": "https://email.env.test"}
        with mock.patch.dict(os.environ, env, clear=True):
            t = Recorder((200, {}, {"id": "e1"}))
            eza = Eza(transport=t)
            eza.emails.send(from_="a@b.co.ke", to=["c@d.co.ke"], subject="Hi", text="Hi")
            self.assertEqual(t.seen[0][1], "https://email.env.test/emails")
            self.assertEqual(t.seen[0][2]["Authorization"], "Bearer eza_live_env")
            with self.assertRaisesRegex(ValueError, "no API key for PayEdge"):
                eza.payedge.stk_push(phone="254712345678", amount_kes=1, account_reference="A")

    def test_errors_use_the_api_name_and_429_is_retried(self):
        t = Recorder(
            (429, {"retry-after": "0"}, {"statusCode": 429, "name": "rate_limit_exceeded", "message": "Slow down."}),
            (403, {"eza-request-id": "req_9"}, {"statusCode": 403, "name": "domain_not_verified", "message": "shop.co.ke is not verified."}),
        )
        eza = Eza("k", transport=t)
        with self.assertRaises(EzaError) as cm:
            eza.emails.send(from_="a@shop.co.ke", to="x@y.co.ke", subject="s", text="t", idempotency_key="k1")
        self.assertEqual(len(t.seen), 2)
        self.assertEqual(t.seen[1][2]["Idempotency-Key"], "k1")
        e = cm.exception
        self.assertEqual((e.status, e.code, e.request_id), (403, "domain_not_verified", "req_9"))
        self.assertIn("not verified", str(e))

    def test_send_checks_what_it_needs_first(self):
        eza = Eza("k", transport=lambda *a: self.fail("sent"))
        with self.assertRaises(ValueError):
            eza.emails.send(from_="a@b.c", to="a@b.c", subject="s")


class PayEdgeTests(unittest.TestCase):
    def test_stk_push_sends_the_key_body_and_idempotency_key(self):
        t = Recorder((201, {}, {"id": "tx1", "checkout_request_id": "ws_CO_1", "status": "pending"}))
        out = Eza("eza_live_k", payedge_url="https://payedge.test/", transport=t).payedge.stk_push(
            phone="254712345678", amount_kes=1500, account_reference="INV1024"
        )
        self.assertEqual(out["checkout_request_id"], "ws_CO_1")
        method, url, headers, body = t.seen[0]
        self.assertEqual(url, "https://payedge.test/v1/stk-push")
        self.assertEqual(body["idempotency_key"], headers["Idempotency-Key"])

    def test_decimals_are_refused(self):
        with self.assertRaises(ValueError):
            Eza("k", transport=lambda *a: self.fail("sent")).payedge.stk_push(
                phone="254712345678", amount_kes=1.5, account_reference="A"
            )

    def test_502_is_not_retried_and_503_is(self):
        t = Recorder((503, {}, {}), (502, {}, {"code": "mpesa_unavailable", "detail": "Safaricom did not accept it."}))
        with self.assertRaises(EzaError) as cm:
            Eza("k", transport=t).payedge.stk_push(phone="254712345678", amount_kes=5, account_reference="A")
        self.assertEqual(len(t.seen), 2)
        self.assertEqual(cm.exception.code, "mpesa_unavailable")


class OverHTTP(unittest.TestCase):
    def test_a_real_round_trip_escapes_references(self):
        seen = {}

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                seen["path"], seen["auth"] = self.path, self.headers["Authorization"]
                payload = json.dumps({"status": "completed"}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *a):
                pass

        srv = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=srv.handle_request, daemon=True).start()
        eza = Eza("eza_live_k", payedge_url=f"http://127.0.0.1:{srv.server_port}")
        self.assertEqual(eza.payedge.get_transaction("ws_CO/1")["status"], "completed")
        srv.server_close()
        self.assertEqual(seen["path"], "/v1/transactions/ws_CO%2F1")
        self.assertEqual(seen["auth"], "Bearer eza_live_k")


if __name__ == "__main__":
    unittest.main()

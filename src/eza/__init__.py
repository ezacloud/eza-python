"""Eza Cloud for Python: transactional email, PayEdge M-Pesa STK pushes and
signed webhooks. Standard library only; Python 3.9 or later.

On Eza, ``Eza()`` is enough to send email: apps receive EZA_EMAIL_KEY and
EZA_EMAIL_URL.

    from eza import Eza

    eza = Eza()
    eza.emails.send(
        from_="Shop <orders@mail.shop.co.ke>",
        to="wanjiku@gmail.com",
        subject="Your receipt",
        html="<p>Thanks for your order.</p>",
    )
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple, Union

__all__ = ["Eza", "EzaError", "SignatureError", "verify_webhook"]
__version__ = "0.2.0"

EMAIL_URL = "https://email.eza.co.ke"
PAYEDGE_URL = "https://payedge.eza.co.ke"
TOLERANCE_SECONDS = 300

# (method, url, headers, body) -> (status, headers with lowercase names, body)
Transport = Callable[[str, str, Dict[str, str], Optional[bytes]], Tuple[int, Dict[str, str], bytes]]


class EzaError(Exception):
    """An error an Eza API returned, or a network failure (status 0)."""

    def __init__(
        self,
        message: str,
        *,
        status: int = 0,
        code: str = "network_error",
        request_id: Optional[str] = None,
        fields: Optional[List[Dict[str, str]]] = None,
    ) -> None:
        super().__init__(message)
        #: HTTP status; 0 when no response arrived.
        self.status = status
        #: Stable error code, such as validation_error, domain_not_verified or mpesa_unavailable.
        self.code = code
        #: Quote this to Eza support.
        self.request_id = request_id
        #: Field errors on a validation failure: [{"field", "code", "message"}].
        self.fields = fields or []


class SignatureError(Exception):
    """A webhook whose signature or timestamp did not check out."""


def _env(name: str) -> Optional[str]:
    return os.environ.get(name) or None


def _list(value: Union[None, str, Sequence[str]]) -> Optional[List[str]]:
    if value is None:
        return None
    return [value] if isinstance(value, str) else list(value)


def _urllib_transport(timeout: float) -> Transport:
    def send(method: str, url: str, headers: Dict[str, str], body: Optional[bytes]) -> Tuple[int, Dict[str, str], bytes]:
        req = urllib.request.Request(url, data=body, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (https URLs we build)
                return resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, {k.lower(): v for k, v in e.headers.items()}, e.read()
        except (urllib.error.URLError, OSError) as e:
            raise EzaError(f"Eza: request failed: {getattr(e, 'reason', e)}") from e

    return send


class Eza:
    """Your Eza Cloud account.

    :param api_key: An Eza API key; defaults to EZA_API_KEY. PayEdge needs the
        payedge scope, email the email scope.
    :param email_key: A key for email only; defaults to api_key, then
        EZA_EMAIL_KEY (set on every app on Eza).
    :param email_url: Defaults to EZA_EMAIL_URL, then https://email.eza.co.ke.
    :param max_retries: Retries after a network error, a 429 or a 503/504.
        Sends and pushes always carry an idempotency key, so a retry never
        sends twice.
    :param transport: For tests.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        *,
        email_key: Optional[str] = None,
        email_url: Optional[str] = None,
        payedge_url: str = PAYEDGE_URL,
        timeout: float = 30.0,
        max_retries: int = 2,
        transport: Optional[Transport] = None,
    ) -> None:
        api_key = api_key or _env("EZA_API_KEY")
        self._keys = {"payedge": api_key, "email": email_key or api_key or _env("EZA_EMAIL_KEY")}
        self._urls = {
            "email": (email_url or _env("EZA_EMAIL_URL") or EMAIL_URL).rstrip("/"),
            "payedge": payedge_url.rstrip("/"),
        }
        self._max_retries = max_retries
        self._send = transport or _urllib_transport(timeout)
        self.emails = Emails(self)
        self.payedge = PayEdge(self)

    def _request(
        self,
        product: str,
        method: str,
        path: str,
        body: Optional[Mapping[str, Any]] = None,
        extra: Optional[Mapping[str, str]] = None,
    ) -> Dict[str, Any]:
        key = self._keys[product]
        if not key:
            raise ValueError(
                "Eza: no API key for email: pass api_key or set EZA_EMAIL_KEY (apps on Eza have it already)"
                if product == "email"
                else "Eza: no API key for PayEdge: pass api_key (a key with the payedge scope) or set EZA_API_KEY"
            )
        headers = {
            "Authorization": f"Bearer {key}",
            "Accept": "application/json",
            "User-Agent": f"eza-python/{__version__}",
        }
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode()
        headers.update(extra or {})
        attempt = 0
        while True:
            try:
                status, resp_headers, raw = self._send(method, self._urls[product] + path, headers, data)
            except EzaError:
                if attempt < self._max_retries:
                    time.sleep(0.25 * 2**attempt)
                    attempt += 1
                    continue
                raise
            if status in (429, 503, 504) and attempt < self._max_retries:
                try:
                    after = float(resp_headers.get("retry-after", "0"))
                except ValueError:
                    after = 0
                time.sleep(min(after, 10) if after > 0 else 0.25 * 2**attempt)
                attempt += 1
                continue
            try:
                parsed = json.loads(raw) if raw else None
            except ValueError:
                parsed = None
            if 200 <= status < 300:
                return parsed if isinstance(parsed, dict) else {}
            # PayEdge answers problem+json; the email API answers
            # {statusCode, name, message}.
            err = parsed if isinstance(parsed, dict) else {}
            message = err.get("detail") or err.get("message") or err.get("title") or f"HTTP {status}"
            fields = err.get("errors") or []
            if fields:
                message = "; ".join(f"{f.get('field')}: {f.get('message')}" for f in fields)
            raise EzaError(
                f"Eza: {message}",
                status=status,
                code=str(err.get("code") or err.get("name") or "http_error"),
                request_id=resp_headers.get("eza-request-id") or err.get("request_id"),
                fields=fields,
            )


class Emails:
    """Transactional email: password resets, receipts, sign-in codes."""

    def __init__(self, eza: Eza) -> None:
        self._eza = eza

    def send(
        self,
        *,
        from_: str,
        to: Union[str, Sequence[str]],
        subject: str,
        html: Optional[str] = None,
        text: Optional[str] = None,
        cc: Union[None, str, Sequence[str]] = None,
        bcc: Union[None, str, Sequence[str]] = None,
        reply_to: Union[None, str, Sequence[str]] = None,
        headers: Optional[Mapping[str, str]] = None,
        attachments: Optional[Sequence[Mapping[str, Any]]] = None,
        tags: Optional[Sequence[Mapping[str, str]]] = None,
        idempotency_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Sends one email and returns {"id": ...}. Until your domain is
        verified, email only reaches members of your organisation. The same
        idempotency_key returns the first email's ID and sends nothing.

        Attachments: [{"filename": "receipt.pdf", "content": b"...",
        "content_type": "application/pdf"}], bytes, up to 10 MB a message
        after encoding.
        """
        if not from_:
            raise ValueError("Eza: emails.send needs from_")
        if not to:
            raise ValueError("Eza: emails.send needs to")
        if not subject:
            raise ValueError("Eza: emails.send needs a subject")
        if not html and not text:
            raise ValueError("Eza: emails.send needs html, text or both")
        body: Dict[str, Any] = {"from": from_, "to": _list(to), "subject": subject}
        optional = {"cc": _list(cc), "bcc": _list(bcc), "reply_to": _list(reply_to), "html": html, "text": text,
                    "headers": dict(headers) if headers else None, "tags": list(tags) if tags else None}
        body.update({k: v for k, v in optional.items() if v is not None})
        if attachments:
            out = []
            for a in attachments:
                if not a.get("filename") or a.get("content") is None:
                    raise ValueError("Eza: each attachment needs filename and content")
                content = a["content"]
                encoded = base64.b64encode(content).decode() if isinstance(content, (bytes, bytearray)) else str(content)
                item = {"filename": a["filename"], "content": encoded}
                if a.get("content_type"):
                    item["content_type"] = a["content_type"]
                out.append(item)
            body["attachments"] = out
        key = idempotency_key or str(uuid.uuid4())
        return self._eza._request("email", "POST", "/emails", body, {"Idempotency-Key": key})

    def get(self, email_id: str) -> Dict[str, Any]:
        """An email you sent: its status and last delivery event. Recipients are masked."""
        if not email_id:
            raise ValueError("Eza: emails.get needs an ID")
        return self._eza._request("email", "GET", "/emails/" + urllib.parse.quote(email_id, safe=""))


class PayEdge:
    """PayEdge: M-Pesa STK pushes to your own Paybill or Till."""

    def __init__(self, eza: Eza) -> None:
        self._eza = eza

    def stk_push(
        self,
        *,
        phone: str,
        amount_kes: int,
        account_reference: str,
        description: Optional[str] = None,
        config_id: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Sends an STK push and returns the transaction once Safaricom has
        accepted the prompt; the result arrives at your webhook or through
        get_transaction(). The same idempotency_key returns the first
        transaction instead of prompting again."""
        if not isinstance(amount_kes, int) or isinstance(amount_kes, bool):
            raise ValueError("Eza: amount_kes must be a whole number of KES")
        key = idempotency_key or str(uuid.uuid4())
        body: Dict[str, Any] = {"phone": str(phone), "amount_kes": amount_kes,
                                "account_reference": account_reference, "idempotency_key": key}
        if description is not None:
            body["description"] = description
        if config_id is not None:
            body["config_id"] = config_id
        return self._eza._request("payedge", "POST", "/v1/stk-push", body, {"Idempotency-Key": key})

    def get_transaction(self, ref: str) -> Dict[str, Any]:
        """A transaction by its ID or checkout_request_id."""
        if not ref:
            raise ValueError("Eza: get_transaction needs an ID or checkout_request_id")
        return self._eza._request("payedge", "GET", "/v1/transactions/" + urllib.parse.quote(ref, safe=""))


def verify_webhook(
    body: Union[str, bytes],
    header: Optional[str],
    secret: str,
    *,
    tolerance_seconds: int = TOLERANCE_SECONDS,
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """Checks a webhook's Eza-Signature header against the raw request body
    and the endpoint's signing secret, and returns the parsed event. Pass the
    body exactly as received (request.body in Django, request.get_data() in
    Flask), not a re-serialised dict. Raises SignatureError otherwise."""
    if not secret:
        raise ValueError("Eza: the webhook signing secret is required")
    if not header:
        raise SignatureError("Missing Eza-Signature header")
    raw = body.decode() if isinstance(body, (bytes, bytearray)) else body
    ts: Optional[str] = None
    sigs: List[str] = []
    for part in header.split(","):
        k, _, v = part.strip().partition("=")
        if k == "t":
            ts = v
        elif k == "v1" and v:
            sigs.append(v.lower())
    if not ts or not ts.isdigit() or not sigs:
        raise SignatureError("Malformed Eza-Signature header")
    if abs((time.time() if now is None else now) - int(ts)) > tolerance_seconds:
        raise SignatureError("Webhook timestamp is outside the tolerance; possible replay")
    want = hmac.new(secret.encode(), f"{ts}.{raw}".encode(), hashlib.sha256).hexdigest()
    if not any(hmac.compare_digest(want, s) for s in sigs):
        raise SignatureError("Webhook signature does not match")
    event = json.loads(raw)
    if not isinstance(event, dict):
        raise SignatureError("Webhook body is not a JSON object")
    return event


Eza.verify_webhook = staticmethod(verify_webhook)  # type: ignore[attr-defined]

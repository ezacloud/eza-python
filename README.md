# Eza Cloud for Python

Transactional email, PayEdge M-Pesa STK pushes and signed webhooks. Python
3.9 or later; standard library only.

```sh
pip install eza-sdk
```

## Email

On Eza, every app has `EZA_EMAIL_KEY` and `EZA_EMAIL_URL` already:

```python
from eza import Eza

eza = Eza()
sent = eza.emails.send(
    from_="Shop <orders@mail.shop.co.ke>",
    to="wanjiku@gmail.com",
    subject="Your receipt",
    html="<p>Thanks for your order.</p>",
)
```

Elsewhere, pass an API key with the `email` scope: `Eza("eza_live_...")`.
Until your sending domain is verified, email only reaches members of your
organisation. Django apps can instead send through SMTP with the
`EZA_SMTP_*` variables (see the docs).

## PayEdge

```python
eza = Eza(os.environ["EZA_API_KEY"])  # the payedge scope
tx = eza.payedge.stk_push(phone="254712345678", amount_kes=1500, account_reference="INV1024")
```

## Webhooks

```python
from eza import verify_webhook, SignatureError

def eza_webhook(request):  # Django
    try:
        event = verify_webhook(request.body, request.headers.get("Eza-Signature"), os.environ["EZA_WEBHOOK_SECRET"])
    except SignatureError:
        return HttpResponse(status=400)
    ...
```

## Errors

API errors raise `eza.EzaError` with `status`, `code`, `request_id` and
`fields`.

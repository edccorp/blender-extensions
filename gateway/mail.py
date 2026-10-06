"""Sending a customer their repository secret.

Two moments need it. A purchase whose payment does not settle immediately
provisions through the webhook, minutes after the buyer has closed the tab
-- their token exists and nothing tells them. And a customer who has lost
their secret has, until now, had to write in and wait for a human.

Deliberately small, and deliberately off by default: with no RESEND_API_KEY
configured nothing is sent and every caller carries on exactly as it did
before. Sending is the kind of thing that should have to be switched on on
purpose, not the kind that starts happening because a module was imported.

Resend over HTTPS rather than SMTP through the company mailbox. SMTP would
have worked and was free of new vendors, but it wanted a licensed Microsoft
seat this tenant did not have spare, and it reports delivery to the next hop
and nothing after -- no bounces, no complaints, so a licence key that
silently fails to arrive looks exactly like one that was read. Resend
reports both, and its free tier is far above anything this sends. The
earlier SMTP implementation is in the history if it is ever wanted.
"""

import os

import httpx

API_URL = "https://api.resend.com/emails"
API_KEY = os.environ.get("RESEND_API_KEY", "").strip()
FROM = os.environ.get("EMAIL_FROM", "").strip()
#: Optional. Where replies go, when that should not be the sending address
#: -- a support list several people read, rather than an inbox nobody does.
REPLY_TO = os.environ.get("EMAIL_REPLY_TO", "").strip()
#: Short, because a purchase must not be held up by it: the token is
#: already saved by the time this runs.
TIMEOUT = float(os.environ.get("EMAIL_TIMEOUT", "15") or 15)

CONTACT = "Engineering Dynamics Company"


def configured():
    """Is there anywhere to send mail? Absent configuration means no."""
    return bool(API_KEY and FROM)


def from_domain():
    """The domain messages are sent from, for /healthz to report.

    The domain and not the address: this ends up on a public endpoint, and
    the domain is already public in DNS and in the From of every message,
    whereas the mailbox is not worth publishing. It is also where the
    mistakes are -- a From on a domain that is not verified with Resend is
    refused outright, and one character is all it takes.
    """
    # A From may carry a display name -- EDC Software <a@b.com> -- which
    # Resend accepts and which would otherwise report a domain with a
    # bracket stuck to it.
    return FROM.rsplit("@", 1)[-1].strip("<> ") if "@" in FROM else ""


def payload(to_address, subject, body):
    """The request Resend expects.

    Plain text on purpose: it is a licence key and setup steps, an HTML
    part would add nothing, and text is what survives every client and
    every spam filter unchanged.
    """
    message = {
        "from": FROM,
        "to": [to_address],
        "subject": subject,
        "text": body,
    }
    if REPLY_TO:
        message["reply_to"] = REPLY_TO
    return message


async def send(to_address, subject, body):
    """Send one message. True when Resend accepted it, False otherwise.

    Never raises. Every caller is doing something more important than this
    -- provisioning a purchase, answering a form -- and none of them should
    fail because a mail API was slow. A False is logged and the customer
    still has the page in front of them.

    Async rather than threaded: httpx is already how this service talks to
    Stripe and GitHub, so there is nothing to block the event loop with.
    """
    if not (configured() and to_address):
        return False
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            response = await client.post(
                API_URL,
                json=payload(to_address, subject, body),
                headers={"Authorization": f"Bearer {API_KEY}"},
            )
    except Exception as exc:                        # noqa: BLE001
        print(f"[gateway] ERROR: could not email {to_address}: "
              f"{type(exc).__name__}: {exc}")
        return False
    if response.status_code >= 300:
        # The body carries Resend's own reason -- an unverified domain, a
        # rejected address -- which is the thing worth having in the log.
        print(f"[gateway] ERROR: Resend refused mail to {to_address}: "
              f"{response.status_code} {response.text[:300]}")
        return False
    return True


def setup_steps():
    """What the customer does with the secret, in both emails."""
    return (
        "Set up Blender (4.2 or newer):\n"
        "  1. Edit > Preferences > Get Extensions > Repositories > + >\n"
        "     Add Remote Repository\n"
        "  2. URL: https://extensions.edccorp.com/index.json\n"
        "  3. Tick 'Requires Access Token' and paste the secret into Secret\n"
        "  4. Your products appear under Get Extensions - click Install\n"
        "  5. Save Preferences so it is still there next time\n"
    )


def purchase_body(name, token, products):
    """The email a buyer gets when their purchase is provisioned."""
    listed = "\n".join(f"  - {p}" for p in products) or "  - your EDC products"
    return (
        f"Hello {name},\n\n"
        "Thank you for your purchase. Your repository secret is:\n\n"
        f"    {token}\n\n"
        "It covers:\n"
        f"{listed}\n\n"
        f"{setup_steps()}\n"
        "Keep this secret to yourself - anyone with it can reach your EDC\n"
        "Software downloads. Keep this email, or store the secret somewhere\n"
        "safe.\n\n"
        f"-- {CONTACT}\n"
    )


def recovery_body(name, token):
    """The email sent when somebody asks for their secret again."""
    return (
        f"Hello {name},\n\n"
        "Someone asked us to send your EDC Software repository secret to\n"
        "this address. Here it is:\n\n"
        f"    {token}\n\n"
        f"{setup_steps()}\n"
        "If this was not you, nothing has changed and your secret still\n"
        "works - but tell us, and we will issue you a new one.\n\n"
        f"-- {CONTACT}\n"
    )


def recovery_body_several(entries):
    """The recovery email when one address is on more than one customer.

    `entries` is [(name, token), ...]. Every secret goes, each under the
    name it belongs to: sending only the first would leave the others
    unrecoverable by this address, and which one came first is down to how
    the customer file happens to sort.
    """
    listed = "\n".join(f"  {name}:\n\n    {token}\n" for name, token in entries)
    return (
        "Hello,\n\n"
        "Someone asked us to send the EDC Software repository secret for\n"
        f"this address. It is on {len(entries)} accounts, so here is each one:\n\n"
        f"{listed}\n"
        "Use the one for the products you want in Blender - Blender holds\n"
        "one secret per repository.\n\n"
        f"{setup_steps()}\n"
        "If this was not you, nothing has changed and these secrets still\n"
        "work - but tell us, and we will issue new ones.\n\n"
        f"-- {CONTACT}\n"
    )

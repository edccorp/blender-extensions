"""Sending a customer their repository secret.

Two moments need it. A purchase whose payment does not settle immediately
provisions through the webhook, minutes after the buyer has closed the tab
-- their token exists and nothing tells them. And a customer who has lost
their secret has, until now, had to write in and wait for a human.

Deliberately small, and deliberately off by default: with no EMAIL_HOST
configured nothing is sent and every caller carries on exactly as it did
before. Sending is the kind of thing that should have to be switched on
on purpose, not the kind that starts happening because a module was
imported.

SMTP rather than a transactional provider because the domain already sends
mail: SPF and DKIM are aligned for that mailbox, which is the part that
otherwise decides whether a licence key lands in the inbox or the spam
folder. The cost is that SMTP reports delivery to the next hop and nothing
after it -- no bounces, no complaints. If that becomes the thing that
hurts, `send` is the only function to reimplement.
"""

import os
import smtplib
import ssl
from email.message import EmailMessage

HOST = os.environ.get("EMAIL_HOST", "").strip()
PORT = int(os.environ.get("EMAIL_PORT", "587") or 587)
USER = os.environ.get("EMAIL_USER", "").strip()
PASSWORD = os.environ.get("EMAIL_PASSWORD", "")
FROM = os.environ.get("EMAIL_FROM", "").strip() or USER
#: How long to wait on the mail server. Short, because a purchase must not
#: be held up by it: the token is already saved by the time this runs.
TIMEOUT = float(os.environ.get("EMAIL_TIMEOUT", "15") or 15)

CONTACT = "Engineering Dynamics Company"


def configured():
    """Is there anywhere to send mail? Absent configuration means no."""
    return bool(HOST and USER and PASSWORD and FROM)


def build(to_address, subject, body):
    """The message, as a plain-text email.

    Plain text on purpose: it is a licence key and setup steps, an HTML
    part would add nothing, and text is what survives every client and
    every spam filter unchanged.
    """
    message = EmailMessage()
    message["From"] = FROM
    message["To"] = to_address
    message["Subject"] = subject
    message.set_content(body)
    return message


def send(to_address, subject, body):
    """Send one message. True when it was accepted, False otherwise.

    Never raises. Every caller is doing something more important than this
    -- provisioning a purchase, answering a form -- and none of them should
    fail because a mail server was slow. A False is logged and the customer
    still has the page in front of them.
    """
    if not (configured() and to_address):
        return False
    try:
        with smtplib.SMTP(HOST, PORT, timeout=TIMEOUT) as server:
            server.ehlo()
            server.starttls(context=ssl.create_default_context())
            server.ehlo()
            server.login(USER, PASSWORD)
            server.send_message(build(to_address, subject, body))
        return True
    except Exception as exc:                        # noqa: BLE001
        print(f"[gateway] ERROR: could not email {to_address}: "
              f"{type(exc).__name__}: {exc}")
        return False


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

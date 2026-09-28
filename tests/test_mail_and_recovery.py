"""Sending a customer their secret, and the two rules that make it safe.

A delayed payment provisions through the webhook minutes after the buyer
closed the tab: their token exists and nothing tells them. And a customer
who has lost their secret had to write in and wait for a human.

The risks are not in the sending. They are in a form that emails a secret:
it must send only to the address recorded on the customer, never the one
typed in, and it must answer identically whether or not the address belongs
to a customer -- otherwise it becomes a way to ask which firms buy from us,
one address at a time.
"""

import importlib.util
import pathlib
import sys
import types

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
GATEWAY = (ROOT / "gateway" / "main.py").read_text()
MAIL_PATH = ROOT / "gateway" / "mail.py"
MAIL_SOURCE = MAIL_PATH.read_text()


def load_mail(monkeypatch, **env):
    """mail.py with a given environment, since its config is read at import."""
    for key in ("EMAIL_HOST", "EMAIL_PORT", "EMAIL_USER", "EMAIL_PASSWORD",
                "EMAIL_FROM", "EMAIL_TIMEOUT"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    spec = importlib.util.spec_from_file_location("edc_mail", MAIL_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CONFIGURED = {
    "EMAIL_HOST": "smtp.office365.com",
    "EMAIL_USER": "software@edccorp.com",
    "EMAIL_PASSWORD": "app-password",
    "EMAIL_FROM": "software@edccorp.com",
}


# ------------------------------------------------------------- off by default

def test_nothing_sends_until_it_is_configured(monkeypatch):
    """Sending should have to be switched on deliberately, not start
    happening because a module was imported."""
    mail = load_mail(monkeypatch)
    assert mail.configured() is False
    assert mail.send("buyer@acme.com", "subject", "body") is False


@pytest.mark.parametrize("missing", ["EMAIL_HOST", "EMAIL_USER", "EMAIL_PASSWORD"])
def test_half_a_configuration_is_no_configuration(monkeypatch, missing):
    env = {k: v for k, v in CONFIGURED.items() if k != missing}
    assert load_mail(monkeypatch, **env).configured() is False


def test_a_full_configuration_is_ready(monkeypatch):
    assert load_mail(monkeypatch, **CONFIGURED).configured() is True


def test_the_from_address_falls_back_to_the_login(monkeypatch):
    env = {k: v for k, v in CONFIGURED.items() if k != "EMAIL_FROM"}
    mail = load_mail(monkeypatch, **env)
    assert mail.FROM == "software@edccorp.com"
    assert mail.configured() is True


# --------------------------------------------------------------- the message

def test_the_message_is_addressed_and_plain_text(monkeypatch):
    mail = load_mail(monkeypatch, **CONFIGURED)
    message = mail.build("buyer@acme.com", "Your secret", "body text")
    assert message["To"] == "buyer@acme.com"
    assert message["From"] == "software@edccorp.com"
    assert message["Subject"] == "Your secret"
    assert message.get_content_type() == "text/plain"


def test_the_purchase_email_carries_the_secret_and_what_to_do_with_it(monkeypatch):
    mail = load_mail(monkeypatch, **CONFIGURED)
    body = mail.purchase_body("Acme LLC", "edc_abc123", ["recon_toolkit"])
    assert "edc_abc123" in body
    assert "recon_toolkit" in body
    assert "index.json" in body          # they cannot use it without the URL
    assert "Requires Access Token" in body


def test_the_recovery_email_says_what_to_do_if_it_was_not_you(monkeypatch):
    """It is sent to the address on the account, so an unexpected one means
    somebody else typed that address in -- worth knowing, and worth saying
    that nothing has changed."""
    mail = load_mail(monkeypatch, **CONFIGURED)
    body = mail.recovery_body("Acme LLC", "edc_abc123")
    assert "edc_abc123" in body
    assert "not you" in body
    assert "new one" in body


def test_a_purchase_of_nothing_named_still_reads_as_a_sentence(monkeypatch):
    mail = load_mail(monkeypatch, **CONFIGURED)
    assert "your EDC products" in mail.purchase_body("Acme", "edc_x", [])


# ----------------------------------------------------- failure is not fatal

def test_a_failing_mail_server_does_not_raise(monkeypatch, capsys):
    """Every caller is doing something more important -- provisioning a
    purchase, answering a form -- and none may fail because mail was slow."""
    mail = load_mail(monkeypatch, **CONFIGURED)

    def explode(*args, **kwargs):
        raise OSError("connection reset")
    monkeypatch.setattr(mail.smtplib, "SMTP", explode)

    assert mail.send("buyer@acme.com", "s", "b") is False
    assert "could not email" in capsys.readouterr().out


def test_a_send_with_no_address_is_not_attempted(monkeypatch):
    mail = load_mail(monkeypatch, **CONFIGURED)
    monkeypatch.setattr(mail.smtplib, "SMTP",
                        lambda *a, **k: pytest.fail("tried to send to nobody"))
    assert mail.send("", "s", "b") is False


def test_the_password_is_not_printed_when_sending_fails(monkeypatch, capsys):
    # The log line goes to Railway's console, which is not where a mailbox
    # password should end up.
    mail = load_mail(monkeypatch, **CONFIGURED)

    def explode(*args, **kwargs):
        raise OSError("connection reset")
    monkeypatch.setattr(mail.smtplib, "SMTP", explode)
    mail.send("buyer@acme.com", "s", "b")
    assert "app-password" not in capsys.readouterr().out


# ------------------------------------------------------- how it is wired in

def test_the_purchase_email_goes_only_on_a_new_provisioning():
    """_provision_purchase runs again on every /welcome reload and on a
    webhook replay; both return from the already-processed branch before
    reaching this, so it cannot become one email per refresh."""
    body = GATEWAY.split("async def _provision_purchase(", 1)[1].split("\ndef ", 1)[0]
    assert body.count("_email_new_purchase(") == 1
    assert body.index("already_processed\": True") < body.index("_email_new_purchase(")


def test_sending_runs_off_the_event_loop():
    """smtplib blocks. On the event loop, a slow mail server would stall
    every other request the gateway is serving, including Blender's."""
    for helper in ("_email_new_purchase", "async def recover("):
        body = GATEWAY.split(helper, 1)[1].split("\n@app", 1)[0]
        assert "asyncio.to_thread(" in body, f"{helper} blocks the loop"


def test_a_purchase_with_no_address_does_not_reach_the_mail_layer():
    """A Stripe session can arrive without customer_details.email. send()
    would refuse it anyway, but only after a thread and a log line claiming
    an attempt to mail nobody -- which is the sort of line that gets
    investigated later."""
    body = GATEWAY.split("async def _email_new_purchase(", 1)[1].split("\nasync def ", 1)[0]
    assert "if not (email and mail.configured()):" in body
    assert body.index("mail.configured()") < body.index("asyncio.to_thread")


def test_a_purchase_is_not_held_up_by_the_mail():
    """The token is written before this runs, so the page shows it whether
    or not the message got out."""
    body = GATEWAY.split("async def _provision_purchase(", 1)[1].split("\ndef ", 1)[0]
    assert body.index("_write_customers_file") < body.index("_email_new_purchase(")


# ------------------------------------------------------------ /recover rules

def recover_body():
    return GATEWAY.split("async def recover(", 1)[1].split("\n@app", 1)[0]


def test_recovery_sends_to_the_address_on_file_not_the_one_typed():
    """The rule the whole design rests on: they are the same address when
    the rightful owner asks, and typing somebody else's only mails that
    person."""
    body = recover_body()
    assert 'mail.send, value["email"]' in body, \
        "the recovery email is addressed from the form, not from the record"


def test_recovery_never_shows_the_secret_on_the_page():
    body = recover_body()
    page = GATEWAY.split("def _recovery_page(", 1)[1].split("\n@app", 1)[0]
    assert "token" not in page, "the recovery page renders a token"
    assert "_recovery_page(" in body


def test_the_answer_is_the_same_whether_or_not_they_are_a_customer():
    """Otherwise this is a way to ask which firms buy from us, one address
    at a time."""
    body = recover_body()
    assert body.count("same_answer") >= 3
    # Checked on what the route returns, not on the prose: the docstring
    # explaining why it must never say "no such customer" contains the
    # phrase, and would fail a search for it.
    returns = [line for line in body.splitlines() if "return HTMLResponse" in line]
    assert returns, "the route returns nothing"
    assert all("same_answer" in line or "_recovery_page(" in line for line in returns)


def test_an_unconfigured_gateway_still_answers_the_same_way():
    # With no mail configured the page must not say so, or the form becomes
    # a probe for whether sending is switched on.
    body = recover_body()
    assert "if not mail.configured():" in body
    guard = body.split("if not mail.configured():", 1)[1].split("\n\n", 1)[0]
    assert "same_answer" in guard


def test_repeated_asking_does_not_repeat_the_email():
    body = recover_body()
    assert "RECOVERY_INTERVAL" in body
    assert body.index("_RECOVERY_SENT.get") < body.index("_read_customers_file")


def test_a_throttled_request_is_answered_identically_too():
    body = recover_body()
    throttle = body.split("< RECOVERY_INTERVAL:", 1)[1].split("customers,", 1)[0]
    assert "same_answer" in throttle


def test_a_customer_with_no_email_on_file_is_skipped_not_crashed():
    """Entries added by hand are a plain string with no address at all."""
    body = recover_body()
    assert "isinstance(value, dict)" in body

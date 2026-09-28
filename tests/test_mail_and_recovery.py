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

import asyncio
import importlib.util
import pathlib
import sys
import types

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
GATEWAY = (ROOT / "gateway" / "main.py").read_text()
MAIL_PATH = ROOT / "gateway" / "mail.py"
MAIL_SOURCE = MAIL_PATH.read_text()


def _stub_httpx():
    """A stand-in for httpx, so these tests need none of the gateway's
    runtime dependencies. Every test replaces AsyncClient anyway; this
    only has to exist for the import."""
    if "httpx" in sys.modules:
        return
    try:
        import httpx  # noqa: F401
    except ImportError:
        module = types.ModuleType("httpx")
        module.AsyncClient = object
        sys.modules["httpx"] = module


def load_mail(monkeypatch, **env):
    """mail.py with a given environment, since its config is read at import."""
    _stub_httpx()
    for key in ("RESEND_API_KEY", "EMAIL_FROM", "EMAIL_REPLY_TO",
                "EMAIL_TIMEOUT"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    spec = importlib.util.spec_from_file_location("edc_mail", MAIL_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CONFIGURED = {
    "RESEND_API_KEY": "re_test_key",
    "EMAIL_FROM": "software@edccorp.com",
}


class FakeResponse:
    def __init__(self, status_code=200, text='{"id":"abc"}'):
        self.status_code = status_code
        self.text = text


def fake_client(monkeypatch, mail, response=None, explode=None):
    """Replace httpx.AsyncClient with one that records the request."""
    sent = {}

    class Client:
        def __init__(self, *args, **kwargs):
            sent["timeout"] = kwargs.get("timeout")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json=None, headers=None):
            if explode is not None:
                raise explode
            sent.update(url=url, json=json, headers=headers)
            return response or FakeResponse()

    monkeypatch.setattr(mail.httpx, "AsyncClient", Client)
    return sent


# ------------------------------------------------------------- off by default

def test_nothing_sends_until_it_is_configured(monkeypatch):
    """Sending should have to be switched on deliberately, not start
    happening because a module was imported."""
    mail = load_mail(monkeypatch)
    assert mail.configured() is False
    assert asyncio.run(mail.send("buyer@acme.com", "subject", "body")) is False


@pytest.mark.parametrize("missing", ["RESEND_API_KEY", "EMAIL_FROM"])
def test_half_a_configuration_is_no_configuration(monkeypatch, missing):
    env = {k: v for k, v in CONFIGURED.items() if k != missing}
    assert load_mail(monkeypatch, **env).configured() is False


def test_a_full_configuration_is_ready(monkeypatch):
    assert load_mail(monkeypatch, **CONFIGURED).configured() is True


def test_a_reply_to_is_optional(monkeypatch):
    # Unset, replies go to the sending address, which is the old behaviour.
    mail = load_mail(monkeypatch, **CONFIGURED)
    assert "reply_to" not in mail.payload("buyer@acme.com", "s", "b")


def test_a_reply_to_is_used_when_given(monkeypatch):
    # For sending from an address nobody reads while replies reach a list
    # that several people do.
    mail = load_mail(monkeypatch, EMAIL_REPLY_TO="support@edccorp.com",
                     **CONFIGURED)
    assert mail.payload("buyer@acme.com", "s", "b")["reply_to"] == "support@edccorp.com"


# --------------------------------------------------------------- the message

def test_the_message_is_addressed_and_plain_text(monkeypatch):
    mail = load_mail(monkeypatch, **CONFIGURED)
    message = mail.payload("buyer@acme.com", "Your secret", "body text")
    assert message["to"] == ["buyer@acme.com"]
    assert message["from"] == "software@edccorp.com"
    assert message["subject"] == "Your secret"
    assert message["text"] == "body text"
    assert "html" not in message


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

def test_a_send_that_goes_out_is_addressed_and_authenticated(monkeypatch):
    mail = load_mail(monkeypatch, **CONFIGURED)
    sent = fake_client(monkeypatch, mail)
    assert asyncio.run(mail.send("buyer@acme.com", "Your secret", "body")) is True
    assert sent["url"] == "https://api.resend.com/emails"
    assert sent["json"]["to"] == ["buyer@acme.com"]
    assert sent["headers"]["Authorization"] == "Bearer re_test_key"
    assert sent["timeout"] == mail.TIMEOUT


def test_an_unreachable_api_does_not_raise(monkeypatch, capsys):
    """Every caller is doing something more important -- provisioning a
    purchase, answering a form -- and none may fail because mail was slow."""
    mail = load_mail(monkeypatch, **CONFIGURED)
    fake_client(monkeypatch, mail, explode=OSError("connection reset"))
    assert asyncio.run(mail.send("buyer@acme.com", "s", "b")) is False
    assert "could not email" in capsys.readouterr().out


def test_a_refusal_is_a_false_and_says_why(monkeypatch, capsys):
    """An unverified domain is refused with a reason, and that reason is
    the one thing worth having in the log."""
    mail = load_mail(monkeypatch, **CONFIGURED)
    fake_client(monkeypatch, mail,
                response=FakeResponse(403, '{"message":"domain not verified"}'))
    assert asyncio.run(mail.send("buyer@acme.com", "s", "b")) is False
    out = capsys.readouterr().out
    assert "403" in out and "domain not verified" in out


def test_a_send_with_no_address_is_not_attempted(monkeypatch):
    mail = load_mail(monkeypatch, **CONFIGURED)
    sent = fake_client(monkeypatch, mail)
    assert asyncio.run(mail.send("", "s", "b")) is False
    assert "url" not in sent, "tried to send to nobody"


def test_the_api_key_is_not_printed_when_sending_fails(monkeypatch, capsys):
    # The log line goes to Railway's console, which is not where a sending
    # credential should end up.
    mail = load_mail(monkeypatch, **CONFIGURED)
    fake_client(monkeypatch, mail, explode=OSError("connection reset"))
    asyncio.run(mail.send("buyer@acme.com", "s", "b"))
    assert "re_test_key" not in capsys.readouterr().out


def test_a_refusal_does_not_print_the_api_key_either(monkeypatch, capsys):
    mail = load_mail(monkeypatch, **CONFIGURED)
    fake_client(monkeypatch, mail, response=FakeResponse(401, '{"message":"bad key"}'))
    asyncio.run(mail.send("buyer@acme.com", "s", "b"))
    assert "re_test_key" not in capsys.readouterr().out


# ------------------------------------------------------- how it is wired in

def test_the_purchase_email_goes_only_on_a_new_provisioning():
    """_provision_purchase runs again on every /welcome reload and on a
    webhook replay; both return from the already-processed branch before
    reaching this, so it cannot become one email per refresh."""
    body = GATEWAY.split("async def _provision_purchase(", 1)[1].split("\ndef ", 1)[0]
    assert body.count("_email_new_purchase(") == 1
    assert body.index("already_processed\": True") < body.index("_email_new_purchase(")


def test_sending_is_awaited_rather_than_blocking_the_loop():
    """httpx is already how this service talks to Stripe and GitHub, so
    there is nothing to block the event loop with -- but only if the call
    is awaited rather than run synchronously."""
    for helper in ("_email_new_purchase", "async def recover("):
        body = GATEWAY.split(helper, 1)[1].split("\n@app", 1)[0]
        assert "await mail.send(" in body, f"{helper} does not await the send"


def test_a_purchase_with_no_address_does_not_reach_the_mail_layer():
    """A Stripe session can arrive without customer_details.email. send()
    would refuse it anyway, but only after a thread and a log line claiming
    an attempt to mail nobody -- which is the sort of line that gets
    investigated later."""
    body = GATEWAY.split("async def _email_new_purchase(", 1)[1].split("\nasync def ", 1)[0]
    assert "if not (email and mail.configured()):" in body
    assert body.index("mail.configured()") < body.index("await mail.send(")


def test_the_purchase_email_goes_to_the_buyer():
    """Not to the sending address, and not to anyone else on the record.
    Structural, because importing the gateway means importing FastAPI and
    reading the live environment -- but it pins the one argument that
    decides who gets somebody's licence key."""
    body = GATEWAY.split("async def _email_new_purchase(", 1)[1].split("\nasync def ", 1)[0]
    call = body.split("await mail.send(", 1)[1]
    assert call.lstrip().startswith("email,"), \
        "the purchase email is addressed to something other than the buyer"


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
    assert 'await mail.send(\n            value["email"]' in body, \
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


# ------------------------------------------------------- saying so on /healthz

def test_the_sending_domain_is_reported_without_the_mailbox(monkeypatch):
    # /healthz is public. The domain is already in DNS and in the From of
    # every message; the mailbox is not worth publishing alongside it.
    mail = load_mail(monkeypatch, **CONFIGURED)
    assert mail.from_domain() == "edccorp.com"
    assert "software" not in mail.from_domain()


def test_an_unconfigured_sender_has_no_domain_to_report(monkeypatch):
    assert load_mail(monkeypatch).from_domain() == ""


def test_a_from_that_is_not_an_address_reports_nothing(monkeypatch):
    # Rather than reporting the malformed value itself back out.
    mail = load_mail(monkeypatch, RESEND_API_KEY="re_test_key",
                     EMAIL_FROM="edccorp.com")
    assert mail.from_domain() == ""


def test_a_named_from_still_reports_a_clean_domain(monkeypatch):
    # "EDC Software <software@edccorp.com>" is a valid From that Resend
    # accepts, and the domain is what gets compared against the one it has
    # verified -- so it cannot come back with a bracket stuck to it.
    mail = load_mail(monkeypatch, RESEND_API_KEY="re_test_key",
                     EMAIL_FROM="EDC Software <software@edccorp.com>")
    assert mail.from_domain() == "edccorp.com"


def test_healthz_reports_whether_mail_is_configured():
    """The one subsystem whose failure is silent -- nobody notices a secret
    that never arrived until days later."""
    body = GATEWAY.split("async def healthz(", 1)[1].split("\n@app", 1)[0]
    assert '"mail": mail.configured()' in body


def test_healthz_reports_the_domain_mail_is_sent_from():
    # A From on a domain Resend has not verified is refused outright, and
    # one wrong character looks identical to a working configuration.
    body = GATEWAY.split("async def healthz(", 1)[1].split("\n@app", 1)[0]
    assert '"mail_from_domain": mail.from_domain()' in body


def test_unconfigured_mail_does_not_make_the_gateway_unhealthy():
    """Sending is optional and off by default: a gateway that cannot mail
    still serves every download, and `ok` must not claim otherwise."""
    body = GATEWAY.split("async def healthz(", 1)[1].split("\n@app", 1)[0]
    ok_line = [l for l in body.splitlines() if '"ok":' in l][0]
    assert "mail" not in ok_line
    # The only thing allowed to pull `ok` down after the fact is the
    # customers file, which is what actually stops downloads working.
    demotions = [l.strip() for l in body.splitlines() if 'body["ok"] = False' in l]
    assert len(demotions) == 1
    assert "_customers_cache" in body.split(demotions[0], 1)[0].rsplit("if ", 1)[-1]

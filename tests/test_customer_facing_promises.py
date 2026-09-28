"""What the purchase pages tell a customer has to be true.

The "payment still processing" page told a buyer they would receive their
access token by email. Nothing sends one, and nothing could: the token is
created by _provision_purchase after the payment settles, so it does not
exist when Stripe generates its receipt, and the gateway has no mail
capability at all. It was the worst possible moment to be wrong -- someone
who has just paid, has no token, and is being told to stop looking.

These check the promises rather than the prose: any page may be reworded,
but none of them may say a token arrives somewhere it cannot arrive.
"""

import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
GATEWAY = (ROOT / "gateway" / "main.py").read_text()


MAIL = (ROOT / "gateway" / "mail.py").read_text()


def waiting_branch():
    """The whole not-yet-paid branch, including what it works out first."""
    return GATEWAY.split('payment_status") not in', 1)[1].split("status_code=202", 1)[0]


def test_sending_is_off_until_it_is_configured():
    """The premise these promises rest on. Mail exists now, but it does
    nothing until EMAIL_HOST and the rest are set, so a page may only
    promise an email where one will actually be sent."""
    body = MAIL.split("def configured(", 1)[1].split("\ndef ", 1)[0]
    assert "API_KEY and FROM" in body
    send = MAIL.split("async def send(", 1)[1].split("\ndef ", 1)[0]
    assert "if not (configured() and to_address):" in send
    assert send.index("configured()") < send.index("client.post")


def test_a_page_promises_an_email_only_where_one_is_sent():
    """The sentence this file exists for. It may come back -- it is true
    once mail is configured -- but only behind that check."""
    waiting = waiting_branch()
    if "email" in waiting.lower():
        assert "mail.configured()" in waiting, \
            "the waiting page promises an email unconditionally"


def test_the_promise_is_absent_when_mail_is_off():
    # The false half of the condition has to be empty, not a softer claim.
    waiting = waiting_branch()
    assert 'if mail.configured() else ""' in waiting


def test_the_waiting_page_says_where_the_token_will_appear():
    """A buyer sent away with no token needs to know what to come back to."""
    page = GATEWAY.split("Payment still processing", 1)[1].split("status_code=202", 1)[0]
    assert "reload" in page.lower() or "revisit" in page.lower()
    assert "bookmark" in page.lower()


def test_the_waiting_page_offers_a_human_when_it_never_arrives():
    page = GATEWAY.split("Payment still processing", 1)[1].split("status_code=202", 1)[0]
    assert "Engineering Dynamics Company" in page


def test_nothing_tells_a_customer_to_reply_to_a_receipt():
    """Stripe's receipt is Stripe's; where a reply to it lands depends on
    the Stripe account's reply-to, not on anything here. The contact route
    is the one that is certainly ours."""
    assert "receipt email" not in GATEWAY.lower()


def test_the_welcome_page_does_not_claim_to_be_unrepeatable():
    """It re-provisions from the session and returns the same token, so
    'this page won't show it again' was false -- and it is the one cheap
    way back to a lost secret today."""
    assert "won't show it again" not in GATEWAY
    assert "wont show it again" not in GATEWAY


@pytest.mark.parametrize("page", ["Payment still processing", "Keep your repository secret"])
def test_every_lost_secret_route_points_at_the_same_place(page):
    # Three pages say what to do about a lost secret; all three must send
    # the customer somewhere that answers, which is the CLI's `show`.
    assert page in GATEWAY
    section = GATEWAY.split(page, 1)[1][:600]
    assert "Engineering Dynamics Company" in section


def test_the_token_is_still_created_after_payment_not_before():
    """The reason no email can carry it. If provisioning ever moves before
    the payment settles, the promise could be kept and this file is wrong."""
    welcome = GATEWAY.split("async def welcome(", 1)[1].split("\n@app", 1)[0]
    assert welcome.index("payment_status") < welcome.index("_provision_purchase")

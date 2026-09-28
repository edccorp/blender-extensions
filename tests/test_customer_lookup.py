"""Answering a customer who has lost their repository secret.

The gateway refuses to re-display a secret to anyone who only proved they
can type an email address -- deliberately, and it says so on the page --
and points them at Engineering Dynamics Company instead. Nothing on this
side answered that: `list` printed every customer's secret at once, which
is not what you want on screen while writing a support reply, and there was
no way to give somebody a new secret without losing the rest of their
record.
"""

import importlib.util
import json
import pathlib
import sys
import types

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SOURCE_PATH = ROOT / "tools" / "customer.py"
SOURCE = SOURCE_PATH.read_text()


@pytest.fixture
def cli(monkeypatch):
    """The CLI with its GitHub calls replaced by a customers dict in memory."""
    monkeypatch.setenv("CUSTOMERS_ADMIN_TOKEN", "test-token")
    spec = importlib.util.spec_from_file_location("customer_cli", SOURCE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    state = {"customers": {}, "saved": [], "sha": "sha-1"}
    monkeypatch.setattr(module, "fetch",
                        lambda: (state["customers"], state["sha"]))

    def save(customers, sha, message):
        state["customers"] = customers
        state["saved"].append(message)
    monkeypatch.setattr(module, "save", save)
    module.state = state
    return module


def customer(name, email=None, products=None, **extra):
    entry = {"name": name}
    if email:
        entry["email"] = email
    if products:
        entry["products"] = products
    entry.update(extra)
    return entry


# ------------------------------------------------------- finding a customer

def test_a_customer_is_found_by_the_email_they_wrote_in_from(cli):
    # Which is how a support request actually arrives: from the address
    # they bought with, not from a name they may spell differently.
    cli.state["customers"] = {"edc_aaa": customer("Acme LLC", "buyer@acme.com")}
    assert cli.find(cli.state["customers"], "buyer@acme.com") == ["edc_aaa"]


def test_an_email_is_matched_whatever_case_it_arrives_in(cli):
    cli.state["customers"] = {"edc_aaa": customer("Acme LLC", "Buyer@Acme.com")}
    assert cli.find(cli.state["customers"], "buyer@acme.COM") == ["edc_aaa"]


def test_the_secret_itself_still_wins(cli):
    cli.state["customers"] = {"edc_aaa": customer("Acme LLC", "buyer@acme.com")}
    assert cli.find(cli.state["customers"], "edc_aaa") == ["edc_aaa"]


def test_a_name_still_matches(cli):
    cli.state["customers"] = {"edc_aaa": customer("Acme LLC", "buyer@acme.com")}
    assert cli.find(cli.state["customers"], "acme llc") == ["edc_aaa"]


def test_a_plain_string_entry_has_no_email_to_match(cli):
    # The compact form is just a name; asking it for an email must not raise.
    cli.state["customers"] = {"edc_aaa": "Acme LLC"}
    assert cli.email_of("Acme LLC") == ""
    assert cli.find(cli.state["customers"], "buyer@acme.com") == []


def test_an_unknown_customer_says_what_it_looked_for(cli, capsys):
    cli.state["customers"] = {"edc_aaa": customer("Acme LLC")}
    with pytest.raises(SystemExit):
        cli.resolve_one(cli.state["customers"], "nobody@example.com")
    assert "name or email" in capsys.readouterr().err


# ------------------------------------------------------------ showing one

def test_show_prints_that_customer_and_not_the_others(cli, capsys):
    """`list` puts every customer's secret on screen, which is not what you
    want while writing a reply to one of them."""
    # The one asked for is deliberately not the first in the file, so a
    # lookup that ignored its argument could not pass by luck.
    cli.state["customers"] = {
        "edc_bbb": customer("Other Co", "someone@other.com"),
        "edc_aaa": customer("Acme LLC", "buyer@acme.com"),
    }
    cli.cmd_show(types.SimpleNamespace(customer="buyer@acme.com"))
    out = capsys.readouterr().out
    assert "edc_aaa" in out
    assert "edc_bbb" not in out
    assert "Other Co" not in out


def test_show_carries_the_setup_the_customer_needs(cli, capsys):
    cli.state["customers"] = {"edc_aaa": customer("Acme LLC")}
    cli.cmd_show(types.SimpleNamespace(customer="Acme LLC"))
    out = capsys.readouterr().out
    assert "extensions.edccorp.com/index.json" in out
    assert "Requires Access Token" in out


def test_show_changes_nothing(cli):
    cli.state["customers"] = {"edc_aaa": customer("Acme LLC")}
    cli.cmd_show(types.SimpleNamespace(customer="Acme LLC"))
    assert cli.state["saved"] == []
    assert list(cli.state["customers"]) == ["edc_aaa"]


def test_show_names_the_products_so_the_reply_can_be_checked(cli, capsys):
    cli.state["customers"] = {
        "edc_aaa": customer("Acme LLC", products=["recon_toolkit"])}
    cli.cmd_show(types.SimpleNamespace(customer="Acme LLC"))
    assert "recon_toolkit" in capsys.readouterr().out


# --------------------------------------------------------- reissuing one

def test_reissue_replaces_the_secret(cli, capsys):
    cli.state["customers"] = {"edc_old": customer("Acme LLC")}
    cli.cmd_reissue(types.SimpleNamespace(customer="Acme LLC"))
    tokens = list(cli.state["customers"])
    assert len(tokens) == 1
    assert tokens[0] != "edc_old"
    assert tokens[0].startswith("edc_")
    assert tokens[0] in capsys.readouterr().out


def test_reissue_keeps_everything_else_about_them(cli):
    """Rebuilding the entry from a name and products would quietly drop the
    email address, the purchase history and a dated term."""
    entry = customer("Acme LLC", "buyer@acme.com",
                     products={"recon_toolkit": "2027-07-03"},
                     stripe_sessions=["cs_test_123"])
    cli.state["customers"] = {"edc_old": entry}
    cli.cmd_reissue(types.SimpleNamespace(customer="Acme LLC"))
    kept = list(cli.state["customers"].values())[0]
    assert kept == entry


def test_reissue_retires_the_old_secret(cli):
    # The whole point when a secret has been seen by somebody else: sending
    # the same one back cannot help, because the problem is who else has it.
    cli.state["customers"] = {"edc_old": customer("Acme LLC")}
    cli.cmd_reissue(types.SimpleNamespace(customer="Acme LLC"))
    assert "edc_old" not in cli.state["customers"]


def test_reissue_is_committed_with_a_readable_message(cli):
    # The repo history is the audit trail, so the commit has to say what
    # happened and to whom.
    cli.state["customers"] = {"edc_old": customer("Acme LLC")}
    cli.cmd_reissue(types.SimpleNamespace(customer="Acme LLC"))
    assert cli.state["saved"] == ["Reissue repository secret: Acme LLC"]


def test_reissue_says_the_old_one_stops_working(cli, capsys):
    # And what the customer will see in the meantime, because they will
    # write in about it otherwise.
    cli.state["customers"] = {"edc_old": customer("Acme LLC")}
    cli.cmd_reissue(types.SimpleNamespace(customer="Acme LLC"))
    out = capsys.readouterr().out
    assert "old secret stops working" in out
    assert "access error" in out


def test_reissue_leaves_other_customers_alone(cli):
    cli.state["customers"] = {
        "edc_old": customer("Acme LLC"),
        "edc_bbb": customer("Other Co"),
    }
    cli.cmd_reissue(types.SimpleNamespace(customer="Acme LLC"))
    assert cli.state["customers"]["edc_bbb"] == customer("Other Co")


def test_a_reissued_secret_is_not_the_one_it_replaced(cli):
    # Generated the same way `add` generates one, rather than derived from
    # anything about the customer.
    body = SOURCE.split("def cmd_reissue(", 1)[1].split("\ndef ", 1)[0]
    assert 'secrets.token_urlsafe(18)' in body


# ------------------------------------------------------------ the commands

@pytest.mark.parametrize("command", ["show", "reissue"])
def test_the_commands_are_reachable(command):
    assert f'sub.add_parser("{command}"' in SOURCE


@pytest.mark.parametrize("command", ["show", "reissue", "revoke", "set-products"])
def test_every_lookup_accepts_an_email(command):
    """All four take a customer, and all four are reached from a support
    request that arrives as an email address."""
    block = SOURCE.split(f'sub.add_parser("{command}"', 1)[1].split("p.set_defaults", 1)[0]
    assert "email" in block, f"{command} does not say it accepts an email"

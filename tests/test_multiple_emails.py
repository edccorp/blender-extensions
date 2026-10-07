"""Several addresses on one repository secret -- a company license.

One person at a firm buying for everyone is the common case, and that one
person leaves. "emails" carries the others, so any of them can recover the
secret and a renewal bought with any of them lands on the same entry
instead of minting a second secret the firm's Blender installs never see.

"email" stays a single string: a gateway that predates "emails" ignores the
extra key and reads such an entry exactly as before, which is what makes
the change safe to ship in either order.
"""

import ast
import importlib.util
import pathlib
import types

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
GATEWAY = (ROOT / "gateway" / "main.py").read_text()
CLI_PATH = ROOT / "tools" / "customer.py"


def _gateway_helpers():
    ns = {}
    for node in ast.parse(GATEWAY).body:
        if getattr(node, "name", None) in {"_entry_emails", "_email_on_file",
                                           "_purchase_target"}:
            exec(compile(ast.Module([node], []), "<ast>", "exec"), ns)
    return ns


NS = _gateway_helpers()
entry_emails = NS["_entry_emails"]
purchase_target = NS["_purchase_target"]
email_on_file = NS["_email_on_file"]

COMPANY = {"name": "Acme LLC", "email": "it@acme.com",
           "emails": ["cad@acme.com", "Boss@Acme.com"]}


# ------------------------------------------------------------- the gateway

def test_every_address_on_the_entry_is_read_main_one_first():
    assert entry_emails(COMPANY) == ["it@acme.com", "cad@acme.com", "Boss@Acme.com"]


def test_a_single_address_entry_reads_as_before():
    assert entry_emails({"name": "x", "email": "a@b.com"}) == ["a@b.com"]


def test_the_compact_form_has_no_address():
    assert entry_emails("Acme LLC") == []


@pytest.mark.parametrize("junk", [None, "", "  ", 7, ["x@y.com"], {"a": 1}])
def test_a_hand_edited_main_address_of_the_wrong_shape_is_ignored(junk):
    # The failure this replaces: .strip() on a list took down every
    # purchase and recovery, for every customer, until the file was fixed.
    assert entry_emails({"name": "x", "email": junk}) == []


@pytest.mark.parametrize("junk", ["it@acme.com", 7, None, {"a": 1}])
def test_extra_addresses_that_are_not_a_list_are_ignored(junk):
    assert entry_emails({"name": "x", "email": "a@b.com", "emails": junk}) == ["a@b.com"]


def test_non_strings_inside_the_list_are_skipped():
    value = {"name": "x", "emails": [None, 3, "", "ok@b.com"]}
    assert entry_emails(value) == ["ok@b.com"]


def test_the_same_address_twice_counts_once():
    value = {"name": "x", "email": "a@b.com", "emails": ["A@B.com ", "a@b.com"]}
    assert entry_emails(value) == ["a@b.com"]


def test_any_address_matches_whatever_its_case():
    assert email_on_file(COMPANY, "CAD@acme.com") == "cad@acme.com"
    assert email_on_file(COMPANY, "boss@acme.com") == "Boss@Acme.com"
    assert email_on_file(COMPANY, " it@acme.com ") == "it@acme.com"


def test_an_address_not_on_the_entry_does_not_match():
    assert email_on_file(COMPANY, "someone@else.com") is None
    assert email_on_file("Acme LLC", "it@acme.com") is None


@pytest.mark.parametrize("route", [
    "async def recover(", "async def register(",
])
def test_every_route_that_matches_an_address_reads_all_of_them(route):
    body = GATEWAY.split(route, 1)[1].split("\n@app", 1)[0].split("\nasync def ", 1)[0]
    assert "_email_on_file(value," in body, f"{route} still matches one address"
    assert 'value.get("email", "").strip().lower()' not in body


def test_a_purchase_finds_its_customer_through_purchase_target():
    body = GATEWAY.split("async def _provision_purchase(", 1)[1].split("\nasync def ", 1)[0]
    assert "_purchase_target(customers, email)" in body
    assert 'value.get("email", "").strip().lower()' not in body


# ----------------------------------------------------------------- the CLI

@pytest.fixture
def cli(monkeypatch):
    monkeypatch.setenv("CUSTOMERS_ADMIN_TOKEN", "test-token")
    spec = importlib.util.spec_from_file_location("customer_cli_emails", CLI_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    state = {"customers": {}, "saved": []}
    monkeypatch.setattr(module, "fetch", lambda: (state["customers"], "sha"))

    def save(customers, sha, message):
        state["customers"] = customers
        state["saved"].append(message)
    monkeypatch.setattr(module, "save", save)
    module.state = state
    return module


def add_email(customer, *emails):
    return types.SimpleNamespace(customer=customer, emails=list(emails))


def remove_email(customer, email):
    return types.SimpleNamespace(customer=customer, email=email)


def test_the_cli_reads_addresses_the_way_the_gateway_does(cli):
    for value in (COMPANY, "Acme LLC", {"name": "x", "email": ["bad"]},
                  {"name": "x", "email": "a@b.com", "emails": ["A@b.com", 4, "c@d.com"]}):
        assert cli.emails_of(value) == entry_emails(value)


def test_add_email_keeps_the_main_address_and_appends(cli):
    cli.state["customers"] = {"edc_a": {"name": "Acme LLC", "email": "it@acme.com"}}
    cli.cmd_add_email(add_email("Acme LLC", "cad@acme.com", "boss@acme.com"))
    entry = cli.state["customers"]["edc_a"]
    assert entry["email"] == "it@acme.com"
    assert entry["emails"] == ["cad@acme.com", "boss@acme.com"]
    assert cli.state["saved"] == ["Add email for Acme LLC: cad@acme.com, boss@acme.com"]


def test_add_email_on_an_entry_with_none_sets_the_main_address(cli):
    cli.state["customers"] = {"edc_a": "Acme LLC"}
    cli.cmd_add_email(add_email("Acme LLC", "it@acme.com", "cad@acme.com"))
    assert cli.state["customers"]["edc_a"] == {
        "name": "Acme LLC", "email": "it@acme.com", "emails": ["cad@acme.com"]}


def test_an_address_already_there_is_not_added_twice(cli, capsys):
    cli.state["customers"] = {"edc_a": {"name": "Acme LLC", "email": "it@acme.com"}}
    cli.cmd_add_email(add_email("Acme LLC", "IT@acme.com"))
    assert "emails" not in cli.state["customers"]["edc_a"]
    assert cli.state["saved"] == []
    assert "already on" in capsys.readouterr().out


def test_an_address_on_another_customer_is_refused(cli):
    """Otherwise a purchase or a recovery has two entries to choose from."""
    cli.state["customers"] = {
        "edc_a": {"name": "Acme LLC", "email": "it@acme.com"},
        "edc_b": {"name": "Jo Smith", "email": "jo@x.com", "emails": ["cad@acme.com"]},
    }
    with pytest.raises(SystemExit):
        cli.cmd_add_email(add_email("Acme LLC", "cad@acme.com"))
    assert cli.state["saved"] == []


def test_a_customer_is_found_by_any_of_their_addresses(cli):
    cli.state["customers"] = {"edc_a": dict(COMPANY)}
    assert cli.find(cli.state["customers"], "boss@acme.com") == ["edc_a"]
    assert cli.find(cli.state["customers"], "CAD@ACME.COM") == ["edc_a"]


def test_removing_an_extra_address(cli):
    cli.state["customers"] = {"edc_a": {**COMPANY, "emails": list(COMPANY["emails"])}}
    cli.cmd_remove_email(remove_email("Acme LLC", "cad@acme.com"))
    assert cli.state["customers"]["edc_a"]["emails"] == ["Boss@Acme.com"]
    assert cli.state["customers"]["edc_a"]["email"] == "it@acme.com"


def test_removing_the_main_address_promotes_the_next(cli):
    cli.state["customers"] = {"edc_a": {**COMPANY, "emails": list(COMPANY["emails"])}}
    cli.cmd_remove_email(remove_email("Acme LLC", "it@acme.com"))
    entry = cli.state["customers"]["edc_a"]
    assert entry["email"] == "cad@acme.com"
    assert entry["emails"] == ["Boss@Acme.com"]


def test_removing_the_last_extra_drops_the_key(cli):
    cli.state["customers"] = {"edc_a": {"name": "Acme LLC", "email": "it@acme.com",
                                        "emails": ["cad@acme.com"]}}
    cli.cmd_remove_email(remove_email("Acme LLC", "cad@acme.com"))
    assert cli.state["customers"]["edc_a"] == {"name": "Acme LLC", "email": "it@acme.com"}


def test_removing_the_only_address_says_they_cannot_recover(cli, capsys):
    cli.state["customers"] = {"edc_a": {"name": "Acme LLC", "email": "it@acme.com"}}
    cli.cmd_remove_email(remove_email("Acme LLC", "it@acme.com"))
    assert "email" not in cli.state["customers"]["edc_a"]
    assert "cannot recover" in capsys.readouterr().out


def test_removing_an_address_that_is_not_there_is_refused(cli):
    cli.state["customers"] = {"edc_a": {"name": "Acme LLC", "email": "it@acme.com"}}
    with pytest.raises(SystemExit):
        cli.cmd_remove_email(remove_email("Acme LLC", "who@acme.com"))


def test_set_email_on_an_extra_moves_it_to_main_without_a_duplicate(cli):
    cli.state["customers"] = {"edc_a": {"name": "Acme LLC", "email": "it@acme.com",
                                        "emails": ["cad@acme.com"]}}
    cli.cmd_set_email(types.SimpleNamespace(customer="Acme LLC", email="cad@acme.com"))
    entry = cli.state["customers"]["edc_a"]
    assert entry["email"] == "cad@acme.com"
    assert "emails" not in entry


def test_show_lists_every_address(cli, capsys):
    cli.state["customers"] = {"edc_a": dict(COMPANY)}
    cli.cmd_show(types.SimpleNamespace(customer="Acme LLC"))
    out = capsys.readouterr().out
    for address in ("it@acme.com", "cad@acme.com", "Boss@Acme.com"):
        assert address in out


@pytest.mark.parametrize("command", ["add-email", "remove-email"])
def test_the_commands_are_reachable(command):
    assert f'sub.add_parser("{command}"' in CLI_PATH.read_text()


# ------------------------------------- one address on more than one licence

SEA = {
    "edc_ronny": {"name": "Ronny Wahba", "email": "rwahba@sealimited.com"},
    "edc_company": {"name": "SEA Limited (company license)",
                    "email": "jhiggins@sealimited.com",
                    "emails": ["rwahba@sealimited.com", "jswanson@sealimited.com"]},
    "edc_john": {"name": "John Swanson", "email": "jswanson@sealimited.com"},
}


@pytest.mark.parametrize("order", [list(SEA), list(reversed(SEA))])
def test_a_purchase_goes_to_the_licence_whose_main_address_it_is(order):
    """Whichever way the file sorts, Ronny's renewal lands on his own
    licence, not the company one that lists him as an extra."""
    customers = {t: SEA[t] for t in order}
    assert purchase_target(customers, "RWahba@sealimited.com") == "edc_ronny"
    assert purchase_target(customers, "jswanson@sealimited.com") == "edc_john"


def test_an_address_only_on_the_company_licence_goes_there():
    assert purchase_target(SEA, "jhiggins@sealimited.com") == "edc_company"


def test_an_extra_address_with_no_licence_of_its_own_extends_the_company_one():
    customers = {"edc_company": {"name": "Co", "email": "it@co.com",
                                 "emails": ["cad@co.com"]}}
    assert purchase_target(customers, "cad@co.com") == "edc_company"


def test_an_unknown_address_is_a_new_customer():
    assert purchase_target(SEA, "new@sealimited.com") is None
    assert purchase_target({"edc_x": "Plain String"}, "x@y.com") is None


def test_shared_adds_an_address_that_is_on_another_customer(cli, capsys):
    cli.state["customers"] = {
        "edc_ronny": {"name": "Ronny Wahba", "email": "rwahba@sealimited.com"},
        "edc_company": {"name": "SEA Limited"},
    }
    cli.cmd_add_email(types.SimpleNamespace(
        customer="SEA Limited", emails=["jhiggins@sealimited.com", "rwahba@sealimited.com"],
        shared=True))
    company = cli.state["customers"]["edc_company"]
    assert company["email"] == "jhiggins@sealimited.com"
    assert company["emails"] == ["rwahba@sealimited.com"]
    assert cli.state["customers"]["edc_ronny"] == {
        "name": "Ronny Wahba", "email": "rwahba@sealimited.com"}, "his own licence changed"
    assert "also on Ronny Wahba" in capsys.readouterr().out


def test_a_shared_address_is_never_made_the_main_one(cli):
    """The main address decides where purchases go; a shared one in that
    slot would pull the person's own renewals onto the company licence."""
    cli.state["customers"] = {
        "edc_ronny": {"name": "Ronny Wahba", "email": "rwahba@sealimited.com"},
        "edc_company": {"name": "SEA Limited"},
    }
    cli.cmd_add_email(types.SimpleNamespace(
        customer="SEA Limited", emails=["rwahba@sealimited.com"], shared=True))
    company = cli.state["customers"]["edc_company"]
    assert "email" not in company
    assert company["emails"] == ["rwahba@sealimited.com"]


def test_without_shared_the_refusal_says_how_to_allow_it(cli, capsys):
    cli.state["customers"] = {
        "edc_ronny": {"name": "Ronny Wahba", "email": "rwahba@sealimited.com"},
        "edc_company": {"name": "SEA Limited"},
    }
    with pytest.raises(SystemExit):
        cli.cmd_add_email(add_email("SEA Limited", "rwahba@sealimited.com"))
    assert "--shared" in capsys.readouterr().err

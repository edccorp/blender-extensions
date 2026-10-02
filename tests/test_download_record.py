"""Who has which version, in a form that can be read back.

GitHub's release download_count answers neither half of that question: on a
private repo it counts authenticated fetches of every kind, and it carries no
identity at all. Measured once on v0.49.2 of one toolkit, it stood at 20
against a single gateway request in the window.

The gateway is the only place that knows who asked. It was saying so to
stdout, where the answer survives as long as log retention and is not
queryable even then. So the stamp goes on the customer, in the file that is
already the record of who may have what.

What these tests hold is the part that could go wrong quietly: that recording
a download never changes what anyone is entitled to, that a secret does not
end up somewhere it need not be, and that failing to write the note never
costs anyone the file.
"""
import ast
import asyncio
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
GATEWAY = (ROOT / "gateway" / "main.py").read_text()

WANTED = {"_normalize_products", "_entitlements", "_record_download",
          "_stamp_customers", "_PENDING_DOWNLOADS", "DOWNLOAD_FLUSH_SECONDS"}


def _ns():
    import datetime as dt
    ns = {"os": __import__("os"), "datetime": dt.datetime,
          "timezone": dt.timezone, "dict": dict}
    for node in ast.parse(GATEWAY).body:
        name = getattr(node, "name", None)
        if name is None and isinstance(node, ast.Assign):
            name = getattr(node.targets[0], "id", None)
        if name is None and isinstance(node, ast.AnnAssign):
            # `_PENDING_DOWNLOADS: dict = {}` is an annotated assignment and
            # carries no .targets, so a reader that only knew about Assign
            # skipped it and the name simply was not there.
            name = getattr(node.target, "id", None)
        if name in WANTED:
            exec(compile(ast.Module([node], []), "<ast>", "exec"), ns)
    return ns


ns = _ns()


def test_a_stamp_cannot_change_what_a_customer_may_install():
    """The one thing this must never do. A plain string entry means every
    product forever, and most entries are that form; carrying a stamp means
    upgrading it to an object, and an object that normalised differently
    would silently re-scope somebody's licence."""
    entitle = ns["_entitlements"]
    before = entitle("Acme Reconstruction LLC")
    customers = {"tok": "Acme Reconstruction LLC"}
    ns["_stamp_customers"](customers, {"Acme Reconstruction LLC":
                                       {"file": "x.zip", "at": "2026-10-02T16:00:00Z"}})
    after = entitle(customers["tok"])
    assert before == after == {"name": "Acme Reconstruction LLC",
                               "products": {"*": None}}


def test_a_scoped_customer_keeps_their_scope_and_their_expiry():
    entitle = ns["_entitlements"]
    entry = {"name": "Smith Engineering",
             "products": {"recon_toolkit": "2027-07-03"}}
    customers = {"tok": dict(entry)}
    before = entitle(entry)
    ns["_stamp_customers"](customers, {"Smith Engineering":
                                       {"file": "y.zip", "at": "2026-10-02T16:00:00Z"}})
    assert entitle(customers["tok"]) == before
    assert customers["tok"]["last_download"]["file"] == "y.zip"


def test_only_the_customer_who_downloaded_is_stamped():
    customers = {"a": "Acme", "b": "Beta Ltd"}
    changed = ns["_stamp_customers"](
        customers, {"Acme": {"file": "x.zip", "at": "2026-10-02T16:00:00Z"}})
    assert changed
    assert customers["b"] == "Beta Ltd", "an untouched customer was rewritten"


def test_restamping_the_same_download_changes_nothing():
    """The flush runs on a timer, so it will see the same pending stamp twice
    if nothing new arrived. Writing an identical file would be a commit
    saying nothing, in the history that is the audit trail."""
    stamp = {"file": "x.zip", "at": "2026-10-02T16:00:00Z"}
    customers = {"a": {"name": "Acme", "last_download": stamp}}
    assert ns["_stamp_customers"](customers, {"Acme": dict(stamp)}) is False


def test_the_pending_record_holds_names_and_not_secrets():
    """It outlives the request that filled it. A repository secret in there
    would be one more place a secret lives, for a feature that does not need
    one."""
    ns["_PENDING_DOWNLOADS"].clear()
    ns["_record_download"]("Acme Reconstruction LLC", "Toolkit-1.0.0.zip")
    assert list(ns["_PENDING_DOWNLOADS"]) == ["Acme Reconstruction LLC"]
    assert "secret" not in str(ns["_PENDING_DOWNLOADS"]).lower()
    entry = ns["_PENDING_DOWNLOADS"]["Acme Reconstruction LLC"]
    assert entry["file"] == "Toolkit-1.0.0.zip"
    assert entry["at"].endswith("Z") and "T" in entry["at"]


def test_repeated_downloads_collapse_to_one_field():
    """A customer pulling five times is one row, not five. That is what keeps
    this out of the commit history as anything but a trickle."""
    ns["_PENDING_DOWNLOADS"].clear()
    for i in range(5):
        ns["_record_download"]("Acme", f"Toolkit-1.0.{i}.zip")
    assert len(ns["_PENDING_DOWNLOADS"]) == 1
    assert ns["_PENDING_DOWNLOADS"]["Acme"]["file"] == "Toolkit-1.0.4.zip"


# --- the bits that are about placement rather than logic ---------------------


def _package_handler():
    return GATEWAY.split('@app.get("/packages/{filename}")', 1)[1]


def test_the_stamp_is_taken_only_after_entitlement_is_checked():
    """A refused download is not a download. Recording before the 403 would
    log an attempt as a fetch."""
    body = _package_handler()
    assert body.index("_entitlement_state") < body.index("_record_download(")


def test_the_write_happens_after_the_bytes_not_before():
    """A download must not wait on a commit, nor fail because one failed."""
    body = _package_handler()
    assert "await _flush_downloads()" in body
    assert body.index("StreamingResponse") > body.index("_record_download(")
    close = body.split("async def _close():", 1)[1].split("return StreamingResponse", 1)[0]
    assert "upstream.aclose()" in close and "_flush_downloads()" in close


def test_a_failed_write_keeps_the_stamps_rather_than_dropping_them():
    flush = GATEWAY.split("async def _flush_downloads(", 1)[1].split("\n\n\n", 1)[0]
    assert "still pending" in flush
    # The pop only happens after the write returned.
    assert flush.index("_write_customers_file") < flush.index("_PENDING_DOWNLOADS.pop")


def test_it_does_nothing_at_all_without_somewhere_to_write():
    """Local runs and any deployment without the customers repo configured
    must not start reporting failures they can do nothing about."""
    flush = GATEWAY.split("async def _flush_downloads(", 1)[1].split("\n\n\n", 1)[0]
    assert "not CUSTOMERS_REPO or not ADMIN_GH_TOKEN" in flush


def test_the_flush_is_rate_limited():
    assert ns["DOWNLOAD_FLUSH_SECONDS"] >= 60
    flush = GATEWAY.split("async def _flush_downloads(", 1)[1].split("\n\n\n", 1)[0]
    assert "DOWNLOAD_FLUSH_SECONDS" in flush

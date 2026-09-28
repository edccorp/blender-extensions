"""The public site should say where to get a lost token back.

/recover was linked from the pages a customer reaches after buying, and
from the dialog Blender shows when a secret is refused -- but not from
anything on the public site. Someone who had uninstalled, or was setting
up a second machine, and went to extensions.edccorp.com looking for help
found nothing, and the page is noindex so search could not offer it
either.
"""

import importlib.util
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _module():
    spec = importlib.util.spec_from_file_location(
        "build_index", ROOT / "tools" / "build_index.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


BUILD = _module()
PAGE = BUILD.render_access_licensing_page()


def test_the_access_page_links_the_recovery_route():
    assert BUILD.RECOVER_URL in PAGE


def test_the_link_is_absolute_because_pages_serves_this_too():
    """These files are also served straight from GitHub Pages, where
    href="/recover" lands on edccorp.github.io/recover -- which does not
    exist. The route only lives on the gateway."""
    assert BUILD.RECOVER_URL.startswith("https://")
    assert 'href="/recover"' not in PAGE


def test_the_recovery_url_points_at_the_gateway_route():
    assert BUILD.RECOVER_URL == "https://extensions.edccorp.com/recover"


def test_it_is_asked_the_way_someone_who_lost_one_would_ask():
    # The FAQ is what a customer scans, and "lost" is the word they arrive
    # with -- not "recover", which is what we happen to call the route.
    faq = PAGE.split("Frequently asked questions", 1)[1]
    assert "lost my access token" in faq.lower()
    assert BUILD.RECOVER_URL in faq


def test_the_setup_steps_mention_it_too():
    """Where someone setting up a second machine is already reading, which
    is the other way this comes up and is not a lost token at all."""
    steps = PAGE.split("How access works", 1)[1].split("<h2>", 1)[0]
    assert BUILD.RECOVER_URL in steps
    assert "new computer" in steps


def test_the_page_does_not_promise_the_token_is_shown():
    # It is emailed to the address on the account and never rendered, which
    # is what stops the route handing a token to anyone who knows a
    # customer's email address.
    faq = PAGE.split("Frequently asked questions", 1)[1]
    answer = faq.split("lost my access token", 1)[1].split("<h3>", 1)[0]
    assert "never shown on the page" in answer


LAUNCH = (ROOT / "content" / "assets" / "launch.html").read_text()


def test_the_homepage_install_box_links_it_too():
    """The box is read with Blender open and nothing to paste into Secret,
    which is the moment a missing token actually stops someone."""
    install = LAUNCH.split('id="install"', 1)[1].split("</section>", 1)[0]
    assert BUILD.RECOVER_URL in install


def test_the_homepage_link_is_absolute_as_well():
    # Served from Pages as well as the gateway, same as the built pages.
    assert 'href="/recover"' not in LAUNCH


def test_the_homepage_link_looks_like_a_link():
    """The page resets every anchor to `color:inherit; text-decoration:none`,
    which is right for the nav and the buttons and wrong for a link inside
    a sentence -- it read as ordinary prose, so the one route a customer
    with no token has did not look clickable."""
    assert ".install p a" in LAUNCH
    rule = LAUNCH.split(".install p a", 1)[1].split("}", 1)[0]
    assert "text-decoration:underline" in rule
    # Colour as well as underline, because colour alone is not something
    # every reader can see.
    assert "var(--accent-ink)" in rule


def test_the_homepage_offers_it_where_the_token_is_pasted():
    # After the steps, not buried under the older-add-on note below them.
    install = LAUNCH.split('id="install"', 1)[1].split("</section>", 1)[0]
    assert install.index(BUILD.RECOVER_URL) < install.index("Already have an older")


def test_nothing_in_the_page_is_left_unexpanded():
    # It is one big f-string; a stray {NAME} ships to customers as literal
    # braces rather than a link.
    import re
    assert re.findall(r"\{[A-Za-z_]+\}", PAGE) == []

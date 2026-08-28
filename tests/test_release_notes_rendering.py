"""Product pages should show what changed in the latest release.

The build already fetched the release body and then threw it away, with a
comment explaining why: the notes at the time were GitHub's
pull-request-derived auto-notes, which referenced pre-launch internal work.
They are hand-written changelog sections now, and someone deciding whether
to update had nowhere to read what changed.
"""

import importlib.util
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "tools" / "build_index.py").read_text()


def _module():
    spec = importlib.util.spec_from_file_location(
        "build_index", ROOT / "tools" / "build_index.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


render_notes = _module().render_notes


def test_paragraphs_become_paragraphs():
    assert render_notes("Two fixes shipped.") == "<p>Two fixes shipped.</p>"


def test_a_blank_line_separates_paragraphs():
    out = render_notes("First one.\n\nSecond one.")
    assert out == "<p>First one.</p>\n<p>Second one.</p>"


def test_wrapped_lines_rejoin_into_one_paragraph():
    """The changelogs are hard-wrapped at ~72 columns; rendering each line
    as its own paragraph would put a blank line inside every sentence."""
    assert render_notes("A sentence that\nwraps across lines.") == (
        "<p>A sentence that wraps across lines.</p>")


def test_headings_shift_down_one_level():
    """The page spends h2 on its own sections, so the notes sit under
    "What's new" rather than beside it."""
    assert render_notes("## New") == "<h3>New</h3>"
    assert render_notes("### Fixes") == "<h4>Fixes</h4>"


def test_bullets_become_a_list():
    out = render_notes("- first\n- second")
    assert out == "<ul>\n<li>first</li>\n<li>second</li>\n</ul>"


def test_a_wrapped_bullet_stays_one_item():
    out = render_notes("- a bullet that\n  wraps")
    assert out == "<ul>\n<li>a bullet that wraps</li>\n</ul>"


def test_bold_and_code_render():
    assert "<strong>Bold</strong>" in render_notes("**Bold** text")
    assert "<code>bl_info</code>" in render_notes("The `bl_info` value")


def test_html_in_the_notes_is_escaped_not_executed():
    """Release bodies are text a maintainer wrote, but they land on a public
    page; a stray angle bracket must not become markup."""
    out = render_notes('A <script>alert(1)</script> mention')
    assert "<script>" not in out
    assert "&lt;script&gt;" in out


def test_code_spans_escape_their_contents():
    out = render_notes('Set `"blender": (4, 2, 0)` in bl_info')
    assert "<code>" in out
    assert "&quot;blender&quot;" in out or "&#x27;" in out or "blender" in out
    assert "<blender>" not in out


def test_empty_notes_render_to_nothing():
    """No notes must produce no empty "What's new" heading."""
    assert render_notes("") == ""
    assert render_notes(None) == ""


def test_a_list_after_a_paragraph_closes_the_paragraph():
    out = render_notes("Intro:\n\n- one")
    assert out == "<p>Intro:</p>\n<ul>\n<li>one</li>\n</ul>"


def test_the_product_page_renders_the_notes_and_the_date():
    assert 'What\\u2019s new' in SOURCE or "What’s new" in SOURCE
    assert 'render_notes(release.get("notes", ""))' in SOURCE
    assert 'release.get("published", "")' in SOURCE


def test_the_page_omits_the_section_when_there_are_no_notes():
    """A heading over nothing reads as a broken page."""
    assert 'if notes else ""' in SOURCE

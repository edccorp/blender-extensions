"""The CalibrationCity sample's footage must ship with it.

A movie clip cannot be packed into a .blend, so the sample's two stills
have to be real files served next to it. The .blend refers to them by
relative path, which means the names are not ours to choose: they are
whatever is already written inside it.
"""
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
ASSETS = ROOT / "content" / "assets"
MANIFEST = ASSETS / "CalibrationCity.files"

#: Exactly as the .blend refers to them (//CalibrationImage.jpg).
FOOTAGE = ("CalibrationImage.jpg", "DistortedImage.jpg")


def manifest_names():
    return [line.strip() for line in MANIFEST.read_text().splitlines()
            if line.strip() and not line.strip().startswith("#")]


@pytest.mark.parametrize("name", FOOTAGE)
def test_the_footage_is_published(name):
    path = ASSETS / name
    assert path.exists(), f"{name} is not in content/assets"
    assert path.stat().st_size > 10_000, f"{name} looks truncated"


def test_the_manifest_lists_exactly_the_footage():
    assert manifest_names() == list(FOOTAGE)


def test_the_manifest_does_not_list_the_blend_itself():
    # It is downloaded on its own path; listing it would re-fetch 26 MB.
    assert "CalibrationCity.blend" not in manifest_names()


def test_every_name_is_a_plain_filename():
    """The add-on writes these into a folder on someone's machine and
    refuses anything that could climb out of it, so a name with a path in
    it would simply be skipped and the sample would open without it."""
    for name in manifest_names():
        assert "/" not in name and "\\" not in name and ":" not in name
        assert name not in (".", "..")


def test_the_sample_blend_is_published_too():
    assert (ASSETS / "CalibrationCity.blend").exists()

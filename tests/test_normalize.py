from __future__ import annotations

from bellhaven_sync.normalize import (
    normalize_location,
    normalize_name,
    normalize_street,
    normalize_street_for_comparison,
    normalize_zip,
)


def test_name_strips_filler_phrases_and_parent_suffix():
    assert normalize_name("Bellhaven Senior Living (Parent Account)") == "bellhaven"
    assert normalize_name("Bellhaven Meadows of Findlay") == "bellhaven meadows findlay"
    assert normalize_name("The Arbors at Bellhaven Dayton") == "arbors bellhaven dayton"


def test_street_abbreviations_are_equivalent():
    assert normalize_street("875 Elm Street") == normalize_street("875 Elm St")
    assert normalize_street("1125 Logan Boulevard") == ("1125", "logan blvd")
    assert normalize_street("1800 North Blanchard Street") == ("1800", "n blanchard st")


def test_unit_designators_are_stripped():
    house, street = normalize_street("100 Main Street Suite 200")
    assert house == "100"
    assert street == "main st"


def test_hash_unit_designators_are_stripped():
    # "#2" has no word boundary before the hash, so it needs its own pattern.
    assert normalize_street("100 Main St #2") == normalize_street("100 Main St")
    assert normalize_street("100 Main St #2") == ("100", "main st")


def test_punctuated_unit_designators_are_stripped():
    assert normalize_street("100 Main St Apt. 2") == normalize_street("100 Main St")
    assert normalize_street("100 Main St Ste. 200") == normalize_street("100 Main St")
    assert normalize_street("100 Main Street Suite. 12") == ("100", "main st")
    assert normalize_street("100 Main St Apt. 2") == ("100", "main st")
    assert normalize_street("100 Main St Ste. 200") == ("100", "main st")


def test_street_names_starting_with_ste_are_not_unit_designators():
    # "ste" must be a whole word with a separator before the unit id; otherwise
    # Stevens/Steele collapse to bare "st" and can falsely match other streets.
    assert normalize_street("100 Stevens Street") == ("100", "stevens st")
    assert normalize_street("100 Steele Street") == ("100", "steele st")
    assert normalize_street("100 Stevens Street") != normalize_street("100 Main St Ste 200")


def test_comparison_keeps_unit_identifiers_and_canonicalizes_designators():
    assert normalize_street_for_comparison("100 Main Street Suite 200") == normalize_street_for_comparison(
        "100 Main St Ste. 200"
    )
    assert normalize_street_for_comparison("100 Main St Apartment 2") == normalize_street_for_comparison(
        "100 Main Street Apt. 2"
    )
    assert normalize_street_for_comparison("100 Main St #2") == normalize_street_for_comparison(
        "100 Main Street Unit 2"
    )
    assert normalize_street_for_comparison("100 Main St Suite 200") != normalize_street_for_comparison(
        "100 Main St Suite 100"
    )
    assert normalize_street_for_comparison("100 Main St Apt 2") != normalize_street_for_comparison(
        "100 Main St Apt 3"
    )
    # Matching still drops the unit so these remain the same physical address.
    assert normalize_street("100 Main St Suite 200") == normalize_street("100 Main St Suite 100")


def test_unit_suffix_forms_normalize_equivalently_to_bare_street():
    bare = normalize_street("100 Main St")
    assert normalize_street("100 Main St Ste 200") == bare
    assert normalize_street("100 Main St Ste. 200") == bare
    assert normalize_street("100 Main St Apt 2") == bare
    assert normalize_street("100 Main St Apt. 2") == bare
    assert normalize_street("100 Main St Suite 200") == bare
    assert normalize_street("100 Main Street Suite 200") == bare


def test_zip_keeps_first_five_digits():
    assert normalize_zip("45344-1234") == "45344"
    assert normalize_zip("45344") == "45344"


def test_state_is_uppercased():
    loc = normalize_location(state="oh", city="Findlay", street="1800 N Blanchard St", zip_code="45840")
    assert loc.state == "OH"
    assert loc.city == "findlay"
    assert loc.house_number == "1800"
    assert loc.street == "n blanchard st"


def test_ampersand_and_worded_filler_phrases_normalize_identically():
    ampersand = normalize_name("Bellhaven Nursing & Rehabilitation")
    worded = normalize_name("Bellhaven Nursing and Rehabilitation")
    assert ampersand == worded
    assert ampersand == "bellhaven"
    # The filler phrase itself must be gone, not left as "nursing rehabilitation".
    assert "nursing" not in ampersand
    assert "rehabilitation" not in ampersand

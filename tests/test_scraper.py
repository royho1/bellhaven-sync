from __future__ import annotations

from pathlib import Path

import pytest

from bellhaven_sync import scraper

FIXTURES = Path(__file__).parent / "fixtures" / "html"
BASE = "https://www.bellhavenseniorliving.com"


def fixture_fetcher(overrides: dict[str, str] | None = None):
    mapping = {
        f"{BASE}/": (FIXTURES / "homepage.html").read_text(encoding="utf-8"),
        f"{BASE}/communities": (FIXTURES / "communities.html").read_text(encoding="utf-8"),
        f"{BASE}/sitemap.xml": (FIXTURES / "sitemap.xml").read_text(encoding="utf-8"),
        f"{BASE}/communities/bellhaven-meadows-of-findlay": (FIXTURES / "findlay.html").read_text(
            encoding="utf-8"
        ),
        f"{BASE}/communities/bellhaven-of-new-carlisle": (FIXTURES / "new-carlisle.html").read_text(
            encoding="utf-8"
        ),
        f"{BASE}/communities/bellhaven-court-of-altoona": (FIXTURES / "altoona.html").read_text(
            encoding="utf-8"
        ),
        f"{BASE}/communities/bellhaven-crossings-of-lima": (FIXTURES / "lima.html").read_text(
            encoding="utf-8"
        ),
        f"{BASE}/communities/bellhaven-of-tiffin": (FIXTURES / "tiffin.html").read_text(encoding="utf-8"),
    }
    if overrides:
        mapping.update(overrides)

    def fetch(url: str) -> str:
        key = url.rstrip("/") if url.rstrip("/") != BASE else f"{BASE}/"
        # homepage is stored as BASE/
        if url.rstrip("/") == BASE:
            key = f"{BASE}/"
        if key not in mapping and url in mapping:
            key = url
        if key not in mapping:
            raise AssertionError(f"unexpected fetch: {url}")
        return mapping[key]

    return fetch


def test_listing_parser_finds_community_links():
    html = (FIXTURES / "communities.html").read_text(encoding="utf-8")
    links = scraper.parse_community_links(html, BASE, source="listing")
    assert f"{BASE}/communities/bellhaven-of-new-carlisle" in links
    assert f"{BASE}/communities/bellhaven-meadows-of-findlay" not in links


def test_sitemap_includes_findlay():
    xml = (FIXTURES / "sitemap.xml").read_text(encoding="utf-8")
    links = scraper.parse_sitemap_urls(xml, BASE)
    assert f"{BASE}/communities/bellhaven-meadows-of-findlay" in links


def test_facility_page_parses_address_and_care_type():
    html = (FIXTURES / "findlay.html").read_text(encoding="utf-8")
    facility = scraper.parse_facility_page(
        html, f"{BASE}/communities/bellhaven-meadows-of-findlay"
    )
    assert facility.name == "Bellhaven Meadows of Findlay"
    assert "Blanchard" in facility.street
    assert facility.city == "Findlay"
    assert facility.state == "OH"
    assert facility.zip == "45840"
    assert "Assisted Living" in facility.care_types


def test_one_line_address_with_full_state_name():
    html = """
    <html><body>
      <h1>Bellhaven of New Carlisle</h1>
      <div class="address">875 Elm Street, New Carlisle, Ohio 45344</div>
    </body></html>
    """
    facility = scraper.parse_facility_page(html, f"{BASE}/communities/bellhaven-of-new-carlisle")
    assert facility.street == "875 Elm Street"
    assert facility.city == "New Carlisle"
    assert facility.state == "OH"
    assert facility.zip == "45344"


def test_one_line_address_does_not_treat_street_direction_as_state():
    # "NE" is a valid two-letter token on many streets; the state must be the
    # abbreviation immediately before the ZIP, not the first match.
    street, city, state, zip5 = scraper._parse_address_block(
        "123 NE Main St, Dayton, OH 45402"
    )
    assert street == "123 NE Main St"
    assert city == "Dayton"
    assert state == "OH"
    assert zip5 == "45402"

    street, city, state, zip5 = scraper._parse_address_block(
        "500 US Highway 23, Toledo, OH 43604"
    )
    assert "US Highway 23" in street or street.startswith("500")
    assert city == "Toledo"
    assert state == "OH"
    assert zip5 == "43604"


def test_full_state_name_prefers_longest_match():
    street, city, state, zip5 = scraper._parse_address_block(
        "100 Capitol St, Charleston, West Virginia 25301"
    )
    assert street == "100 Capitol St"
    assert city == "Charleston"
    assert state == "WV"
    assert zip5 == "25301"


def test_enrichment_failure_marks_scrape_incomplete():
    homepage = (FIXTURES / "homepage.html").read_text(encoding="utf-8").replace(
        "35 communities", "5 communities"
    )
    base_fetch = fixture_fetcher({f"{BASE}/": homepage})

    def flaky(url: str) -> str:
        if url.endswith("bellhaven-of-tiffin"):
            raise RuntimeError("boom")
        return base_fetch(url)

    result = scraper.scrape_bellhaven(base_url=BASE, fetch=flaky, enrich_pages=True)
    assert result.facility_count == 5
    assert result.claimed_count == 5
    assert result.complete is False
    assert any("enrichment failed" in b.lower() for b in result.blockers)


def test_page_without_location_evidence_is_enrichment_failure():
    # HTTP-success-equivalent HTML with no parseable address must not count as
    # a successfully enriched facility, even when the URL count matches the claim.
    homepage = (FIXTURES / "homepage.html").read_text(encoding="utf-8").replace(
        "35 communities", "5 communities"
    )
    no_address = (FIXTURES / "no-address.html").read_text(encoding="utf-8")
    result = scraper.scrape_bellhaven(
        base_url=BASE,
        fetch=fixture_fetcher(
            {
                f"{BASE}/": homepage,
                f"{BASE}/communities/bellhaven-of-tiffin": no_address,
            }
        ),
        enrich_pages=True,
    )
    assert result.claimed_count == 5
    assert result.facility_count == 5
    assert result.complete is False
    assert any("usable location evidence" in b for b in result.blockers)
    tiffin = next(f for f in result.facilities if "tiffin" in f.url)
    assert not scraper.has_usable_location(tiffin)
    assert not tiffin.state
    assert not tiffin.street


def test_union_recovers_findlay_missing_from_listing():
    result = scraper.scrape_bellhaven(base_url=BASE, fetch=fixture_fetcher(), enrich_pages=True)
    urls = {f.url for f in result.facilities}
    assert f"{BASE}/communities/bellhaven-meadows-of-findlay" in urls
    findlay = next(f for f in result.facilities if "findlay" in f.url)
    assert "sitemap" in findlay.sources
    assert result.listing_count == 4
    assert result.sitemap_count == 5


def test_facility_in_listing_and_sitemap_keeps_both_sources():
    result = scraper.scrape_bellhaven(base_url=BASE, fetch=fixture_fetcher(), enrich_pages=True)
    lima = next(f for f in result.facilities if "lima" in f.url)
    assert "listing" in lima.sources
    assert "sitemap" in lima.sources


def test_short_union_raises_completeness_blocker():
    # Homepage claims 35; fixtures only produce 5. That is the Findlay-class gap.
    result = scraper.scrape_bellhaven(base_url=BASE, fetch=fixture_fetcher(), enrich_pages=True)
    assert result.claimed_count == 35
    assert result.facility_count == 5
    assert result.complete is False
    assert result.blockers
    assert "Suppressing missing-on-site" in result.blockers[0]


def test_overcount_also_raises_completeness_blocker():
    homepage = (FIXTURES / "homepage.html").read_text(encoding="utf-8").replace(
        "35 communities", "3 communities"
    )
    result = scraper.scrape_bellhaven(
        base_url=BASE,
        fetch=fixture_fetcher({f"{BASE}/": homepage}),
        enrich_pages=True,
    )
    assert result.claimed_count == 3
    assert result.facility_count == 5
    assert result.complete is False


def test_complete_when_claim_matches_union():
    homepage = (FIXTURES / "homepage.html").read_text(encoding="utf-8").replace("35 communities", "5 communities")
    result = scraper.scrape_bellhaven(
        base_url=BASE,
        fetch=fixture_fetcher({f"{BASE}/": homepage}),
        enrich_pages=True,
    )
    assert result.claimed_count == 5
    assert result.complete is True
    assert result.blockers == []


def test_urls_only_is_never_reconciliation_complete():
    # Even when the discovered URL count exactly matches the homepage claim,
    # skipping enrichment leaves no address/state evidence for matching.
    homepage = (FIXTURES / "homepage.html").read_text(encoding="utf-8").replace(
        "35 communities", "5 communities"
    )
    result = scraper.scrape_bellhaven(
        base_url=BASE,
        fetch=fixture_fetcher({f"{BASE}/": homepage}),
        enrich_pages=False,
    )
    assert result.claimed_count == 5
    assert result.facility_count == 5
    assert result.complete is False
    assert any("enrichment was intentionally skipped" in b for b in result.blockers)
    assert all(not f.state and not f.street for f in result.facilities)


def test_missing_claimed_count_is_a_completeness_blocker():
    homepage = "<html><body><h1>Bellhaven</h1><p>Welcome.</p></body></html>"
    result = scraper.scrape_bellhaven(
        base_url=BASE,
        fetch=fixture_fetcher({f"{BASE}/": homepage}),
        enrich_pages=True,
    )
    assert result.claimed_count is None
    assert result.complete is False
    assert any("completeness unknown" in b.lower() for b in result.blockers)


def test_claimed_count_parser():
    html = (FIXTURES / "homepage.html").read_text(encoding="utf-8")
    assert scraper.parse_claimed_count(html) == 35

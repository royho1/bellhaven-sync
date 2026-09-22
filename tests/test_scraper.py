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


def test_union_recovers_findlay_missing_from_listing():
    result = scraper.scrape_bellhaven(base_url=BASE, fetch=fixture_fetcher(), enrich_pages=True)
    urls = {f.url for f in result.facilities}
    assert f"{BASE}/communities/bellhaven-meadows-of-findlay" in urls
    findlay = next(f for f in result.facilities if "findlay" in f.url)
    assert "sitemap" in findlay.sources
    assert result.listing_count == 4
    assert result.sitemap_count == 5


def test_short_union_raises_completeness_blocker():
    # Homepage claims 35; fixtures only produce 5. That is the Findlay-class gap.
    result = scraper.scrape_bellhaven(base_url=BASE, fetch=fixture_fetcher(), enrich_pages=True)
    assert result.claimed_count == 35
    assert result.facility_count == 5
    assert result.complete is False
    assert result.blockers
    assert "Suppressing missing-on-site" in result.blockers[0]


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


def test_claimed_count_parser():
    html = (FIXTURES / "homepage.html").read_text(encoding="utf-8")
    assert scraper.parse_claimed_count(html) == 35

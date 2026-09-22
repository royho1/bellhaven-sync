"""Scrape Bellhaven's public site into facility records.

The site is server-rendered, so requests + BeautifulSoup is enough. Completeness
matters: `/communities` has historically been missing facilities that exist
elsewhere on the site (Findlay), while the homepage claims a higher count. The
scraper unions three sources and raises a completeness blocker when the union
comes up short of the homepage claim, so a missing listing is never mistaken
for stale CRM data.
"""

from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Iterable
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from .config import DEFAULT_TIMEOUT, Settings, load_settings

logger = logging.getLogger(__name__)

DEFAULT_SITE_BASE = "https://www.bellhavenseniorliving.com"

Fetcher = Callable[[str], str]

COMMUNITY_PATH_HINTS = ("/communities/", "/community/", "/locations/", "/location/")
NON_FACILITY_SLUGS = frozenset(
    {
        "communities",
        "community",
        "locations",
        "location",
        "about",
        "about-us",
        "contact",
        "careers",
        "privacy",
        "terms",
        "news",
        "blog",
        "sitemap",
    }
)

STATE_ABBREV = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID",
    "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
    "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
    "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT",
    "vermont": "VT", "virginia": "VA", "washington": "WA", "west virginia": "WV",
    "wisconsin": "WI", "wyoming": "WY",
}

CLAIMED_COUNT_RE = re.compile(
    r"(\d+)\s+(?:senior\s+)?(?:living\s+)?(?:communities|locations|facilities)",
    re.IGNORECASE,
)
ZIP_RE = re.compile(r"\b(\d{5})(?:-\d{4})?\b")
STATE_RE = re.compile(r"\b([A-Z]{2})\b")
PHONE_RE = re.compile(r"\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}")
CARE_TYPES = (
    "Assisted Living",
    "Memory Care",
    "Skilled Nursing",
    "Independent Living",
    "Rehabilitation",
    "Respite Care",
)


class ScrapeError(RuntimeError):
    """The scrape failed hard enough that no facility list should be trusted."""


@dataclass
class Facility:
    name: str
    url: str
    street: str = ""
    city: str = ""
    state: str = ""
    zip: str = ""
    phone: str = ""
    care_types: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)

    @property
    def facility_key(self) -> str:
        return self.url.rstrip("/").lower()

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ScrapeResult:
    facilities: list[Facility]
    claimed_count: int | None
    listing_count: int
    sitemap_count: int
    complete: bool
    blockers: list[str] = field(default_factory=list)

    @property
    def facility_count(self) -> int:
        return len(self.facilities)


def _same_host(url: str, base: str) -> bool:
    return urlparse(url).netloc == urlparse(base).netloc


def _is_facility_url(url: str, base: str) -> bool:
    if not _same_host(url, base):
        return False
    path = urlparse(url).path.rstrip("/")
    if not path or path == "":
        return False
    parts = [p for p in path.split("/") if p]
    if not parts:
        return False
    if parts[-1].lower() in NON_FACILITY_SLUGS:
        return False
    # Prefer paths that look like community pages; still accept deep links that
    # appear on the listing or sitemap so Findlay can be recovered.
    if any(hint.strip("/") in parts[0].lower() or path.lower().startswith(hint) for hint in COMMUNITY_PATH_HINTS):
        return len(parts) >= 2
    return False


def _normalize_url(url: str, base: str) -> str:
    absolute = urljoin(base.rstrip("/") + "/", url)
    parsed = urlparse(absolute)
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}".rstrip("/")


def parse_claimed_count(html: str) -> int | None:
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(" ", strip=True)
    matches = [int(m.group(1)) for m in CLAIMED_COUNT_RE.finditer(text) if 2 <= int(m.group(1)) <= 200]
    return max(matches) if matches else None


def parse_community_links(html: str, base: str, *, source: str) -> dict[str, str]:
    """Return {normalized_url: source_tag} for facility-like links on a page."""
    soup = BeautifulSoup(html, "html.parser")
    found: dict[str, str] = {}
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"].strip()
        if href.startswith("#") or href.startswith("mailto:") or href.startswith("tel:"):
            continue
        url = _normalize_url(href, base)
        if _is_facility_url(url, base):
            found[url] = source
    return found


def parse_sitemap_urls(xml_text: str, base: str) -> dict[str, str]:
    soup = BeautifulSoup(xml_text, "xml")
    found: dict[str, str] = {}
    for loc in soup.find_all("loc"):
        raw = (loc.get_text() or "").strip()
        if not raw:
            continue
        url = _normalize_url(raw, base)
        if _is_facility_url(url, base):
            found[url] = "sitemap"
    return found


def _slug_to_name(url: str) -> str:
    slug = urlparse(url).path.rstrip("/").split("/")[-1]
    return slug.replace("-", " ").replace("_", " ").title()


def _parse_address_block(text: str) -> tuple[str, str, str, str]:
    """Best-effort street/city/state/ZIP from a free-text address block."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    street = city = state = zip5 = ""
    for line in lines:
        zip_match = ZIP_RE.search(line)
        if not zip_match:
            if not street and not re.search(r"\d{3}[-.\s]?\d{3}", line):
                # Prefer the first line that looks like a street.
                if re.search(r"\d", line):
                    street = line
            continue
        zip5 = zip_match.group(1)
        before = line[: zip_match.start()].strip(" ,")
        state_match = STATE_RE.search(before)
        if state_match:
            state = state_match.group(1)
            city_part = before[: state_match.start()].strip(" ,")
            if "," in city_part:
                maybe_street, maybe_city = city_part.rsplit(",", 1)
                if re.search(r"\d", maybe_street) and not street:
                    street = maybe_street.strip()
                city = maybe_city.strip() or city
            else:
                city = city_part or city
        else:
            # "Findlay, Ohio 45840"
            lower = before.lower()
            for name, abbrev in STATE_ABBREV.items():
                if lower.endswith(name):
                    state = abbrev
                    city = before[: -len(name)].strip(" ,")
                    break
            if not state and "," in before:
                left, right = before.rsplit(",", 1)
                if re.search(r"\d", left) and not street:
                    street = left.strip()
                    city = right.strip()
                else:
                    city = left.strip()
        break

    if not street:
        for line in lines:
            if re.search(r"\d", line) and not ZIP_RE.search(line) and not PHONE_RE.search(line):
                street = line
                break
    return street, city, state, zip5


def parse_facility_page(html: str, url: str) -> Facility:
    soup = BeautifulSoup(html, "html.parser")
    title = ""
    if soup.title and soup.title.string:
        title = soup.title.string.split("|")[0].split("-")[0].strip()
    h1 = soup.find("h1")
    name = (h1.get_text(" ", strip=True) if h1 else "") or title or _slug_to_name(url)

    address_text = ""
    address_el = soup.find(attrs={"class": re.compile(r"address|location|contact", re.I)})
    if address_el:
        address_text = address_el.get_text("\n", strip=True)
    if not address_text:
        # Look for a line that contains a ZIP.
        body = soup.get_text("\n", strip=True)
        for line in body.splitlines():
            if ZIP_RE.search(line):
                # Take nearby context: previous line + this line.
                lines = body.splitlines()
                idx = lines.index(line)
                chunk = "\n".join(lines[max(0, idx - 1) : idx + 1])
                address_text = chunk
                break

    street, city, state, zip5 = _parse_address_block(address_text)

    phone = ""
    tel = soup.find("a", href=re.compile(r"^tel:", re.I))
    if tel:
        phone = PHONE_RE.search(tel.get_text(" ", strip=True) + " " + tel.get("href", ""))
        phone = phone.group(0) if phone else tel.get_text(" ", strip=True)
    if not phone:
        match = PHONE_RE.search(soup.get_text(" ", strip=True))
        phone = match.group(0) if match else ""

    page_text = soup.get_text(" ", strip=True)
    care_types = [ct for ct in CARE_TYPES if re.search(rf"\b{re.escape(ct)}\b", page_text, re.I)]

    return Facility(
        name=name,
        url=url,
        street=street,
        city=city,
        state=state,
        zip=zip5,
        phone=phone,
        care_types=care_types,
    )


def default_fetcher(settings: Settings | None = None) -> Fetcher:
    """HTTP fetcher that also caches responses under data/html/."""
    resolved = settings or load_settings()
    cache_dir = resolved.data_dir / "html"
    cache_dir.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    session.headers.update(
        {"User-Agent": "bellhaven-sync/0.1 (+local reconciliation tool)", "Accept": "text/html,application/xml"}
    )

    def fetch(url: str) -> str:
        slug = re.sub(r"[^a-zA-Z0-9]+", "_", urlparse(url).path.strip("/") or "home")[:80]
        cache_path = cache_dir / f"{slug}.html"
        response = session.get(url, timeout=DEFAULT_TIMEOUT)
        response.raise_for_status()
        cache_path.write_text(response.text, encoding="utf-8")
        return response.text

    return fetch


def scrape_bellhaven(
    *,
    base_url: str = DEFAULT_SITE_BASE,
    fetch: Fetcher | None = None,
    settings: Settings | None = None,
    enrich_pages: bool = True,
) -> ScrapeResult:
    """Scrape the site. `fetch` is injectable so tests never touch the network."""
    base = base_url.rstrip("/")
    do_fetch = fetch or default_fetcher(settings)

    homepage_html = do_fetch(f"{base}/")
    listing_html = do_fetch(f"{base}/communities")
    claimed = parse_claimed_count(homepage_html)

    listing_links = parse_community_links(listing_html, base, source="listing")
    sitemap_links: dict[str, str] = {}
    try:
        sitemap_html = do_fetch(f"{base}/sitemap.xml")
        sitemap_links = parse_sitemap_urls(sitemap_html, base)
    except Exception as exc:  # noqa: BLE001 - sitemap is optional; listing still works
        logger.warning("sitemap fetch failed: %s", exc)

    # Internal links from the listing page itself (and later from facility pages)
    # catch facilities the listing cards omit.
    union: dict[str, list[str]] = {}
    for url, source in {**sitemap_links, **listing_links}.items():
        union.setdefault(url, [])
        if source not in union[url]:
            union[url].append(source)

    # Walk each listing page once for more internal community links.
    extra_pages = list(listing_links.keys())[:40]
    for page_url in extra_pages:
        try:
            html = do_fetch(page_url) if enrich_pages else ""
        except Exception as exc:  # noqa: BLE001
            logger.warning("facility page fetch failed for %s: %s", page_url, exc)
            continue
        if not html:
            continue
        for url, source in parse_community_links(html, base, source="internal").items():
            sources = union.setdefault(url, [])
            if source not in sources:
                sources.append(source)

    facilities: list[Facility] = []
    for url, sources in sorted(union.items()):
        if enrich_pages:
            try:
                html = do_fetch(url)
                facility = parse_facility_page(html, url)
            except Exception as exc:  # noqa: BLE001
                logger.warning("could not parse facility page %s: %s", url, exc)
                facility = Facility(name=_slug_to_name(url), url=url)
        else:
            facility = Facility(name=_slug_to_name(url), url=url)
        facility.sources = sources
        facilities.append(facility)

    blockers: list[str] = []
    complete = True
    if claimed is not None and len(facilities) < claimed:
        complete = False
        blockers.append(
            f"Scrape incomplete: homepage claims {claimed} communities but the "
            f"union of listing + sitemap + internal links found {len(facilities)}. "
            "Suppressing missing-on-site proposals until the gap is closed."
        )

    return ScrapeResult(
        facilities=facilities,
        claimed_count=claimed,
        listing_count=len(listing_links),
        sitemap_count=len(sitemap_links),
        complete=complete,
        blockers=blockers,
    )


def save_facilities(result: ScrapeResult, path: Path) -> Path:
    import json

    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "claimed_count": result.claimed_count,
        "listing_count": result.listing_count,
        "sitemap_count": result.sitemap_count,
        "complete": result.complete,
        "blockers": result.blockers,
        "facility_count": result.facility_count,
        "facilities": [f.to_dict() for f in result.facilities],
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path

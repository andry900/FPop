#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

from playwright._impl._errors import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

DEFAULT_BANNED = [
    # ── Fuori scatola / senza scatola (EN) ──
    "loose",
    "no box",
    "without box",
    "out of box",
    "oob",
    "unboxed",
    # ── Fuori scatola / senza scatola (IT) ──
    "senza box",
    "senza scatola",
    "senza confezione",
    "no confezione",
    "no packaging",
    # ── Solo scatola (senza figure) ──
    "box only",
    "solo scatola",
    "scatola vuota",
    "empty box",
    # ── Danno scatola (EN) ──
    "not mint",
    "dmg",
    "box damage",
    "damaged box",
    "box damaged",
    "box wear",
    "box flaws",
    "box flaw",
    "box creased",
    "box crease",
    "corner ding",
    "corner dings",
    "shelf wear",
    "water damage",
    "heavily worn",
    "dented",
    "torn",
    "crushed",
    "missing flap",
    # ── Danno scatola (IT) ──
    "scatola rovinata",
    "scatola danneggiata",
    "scatola ammaccata",
    "scatola schiacciata",
    "con scatola rovinata",
    "scatola aperta",
    # ── Danno figure (EN) ──
    "broken",
    "paint defect",
    "paint flaw",
    # ── Danno figure (IT) ──
    "rotto",
    "rotta",
    "difettoso",
    "difettata",
    # ── Non autentici ──
    "custom",
    "replica",
    "fake",
    "unofficial",
    "bootleg",
    "falso",
    # ── Firmati ──
    "signed",
    "autographed",
    "autografato",
    "firmato",
    "autograph",
    "graph"
]

STOPWORDS = {
    "funko",
    "pop",
    "vinyl",
    "figure",
    "official",
    "nuovo",
    "new",
    "con",
    "senza",
    "the",
    "and",
    "for",
    "from",
    "per",
    "del",
    "della",
    "delle",
    "dei",
    "di",
    "da",
    "la",
    "il",
    "lo",
    "a",
    "in",
}

BUNDLE_KEYWORDS = [
    "lot",
    "lotto",
    "set",
    "bundle",
    "collection",
    "collezione",
    "multipack",
    "3 pack",
    "4 pack",
    "pack of"
]

PRICE_PATTERNS = [
    re.compile(r"€\s*([0-9][0-9\.,]*)", re.IGNORECASE),
    re.compile(r"([0-9][0-9\.,]*)\s*EUR", re.IGNORECASE),
    re.compile(r"EUR\s*([0-9][0-9\.,]*)", re.IGNORECASE),
]

# Fallback estimate used only when eBay row does not expose explicit
# import/tax fees for international listings.
ESTIMATED_IMPORT_RATE = 0.22

SHIPPING_COST_PATTERNS = [
    re.compile(r"\+?\s*EUR\s*([0-9][0-9\.,]*)\s+per la consegna", re.IGNORECASE),
    re.compile(r"\+?\s*([0-9][0-9\.,]*)\s*EUR\s+per la consegna", re.IGNORECASE),
]

IMPORT_FEE_PATTERNS = [
    re.compile(r"\b(?:iva|dogana|sdoganamento|spese di importazione)\b[^\n]*?EUR\s*([0-9][0-9\.,]*)", re.IGNORECASE),
    re.compile(r"\b(?:iva|dogana|sdoganamento|spese di importazione)\b[^\n]*?([0-9][0-9\.,]*)\s*EUR", re.IGNORECASE),
]


@dataclass
class SaleCandidate:
    title: str
    price: float
    price_display: str


@dataclass
class ProductResult:
    name: str
    url: str
    quantity: int
    query_tokens: list[str]
    scanned_rows: int
    valid_rows: int
    excluded_banned: int
    excluded_irrelevant: int
    excluded_bundle: int
    used_sales: list[SaleCandidate]
    average_price: float | None
    average_price_display: str | None
    note: str | None = None


@dataclass
class QuerySpec:
    phrase: str
    tokens: list[str]
    anchor_token: str | None
    required_number: str | None = None
    required_markers: list[str] | None = None
    allowed_title_numbers: list[str] | None = None


# If one of these marker groups appears in the search query, we require the
# same marker to appear in the listing title so premium variants are not mixed.
REQUIRED_MARKER_GROUPS: list[tuple[str, list[str]]] = [
    ("chase", ["chase"]),
    ("chalice", ["chalice"]),
    ("hot topic", ["hot topic"]),
    ("galactic toys", ["galactic toys", "galatic toys"]),
    ("box lunch", ["box lunch", "boxlunch"]),
    ("fye", ["fye"]),
    ("gamestop", ["gamestop", "game stop"]),
    ("entertainment earth", ["entertainment earth", "entertainment", "earth"]),
    ("special edition", ["special edition"]),
    ("ae", ["ae exclusive", "ae"]),
    ("px", ["px"]),
    ("aaa anime", ["aaa anime", "aaa"]),
]


def normalize_text(value: str) -> str:
    lowered = value.lower().strip()
    lowered = unicodedata.normalize("NFKD", lowered)
    lowered = "".join(ch for ch in lowered if not unicodedata.combining(ch))
    lowered = re.sub(r"\s+", " ", lowered)
    return lowered


def compact_text(value: str) -> str:
    return re.sub(r"\s+", "", normalize_text(value))


def has_keyword(text: str, keyword: str) -> bool:
    text_norm = normalize_text(text)
    key_norm = normalize_text(keyword)
    if key_norm in text_norm:
        return True
    return key_norm.replace(" ", "") in compact_text(text)


def has_banned_match(text: str, banned_keyword: str) -> bool:
    text_norm = normalize_text(text)
    banned_norm = normalize_text(banned_keyword)
    if not banned_norm:
        return False

    # For single words, require word boundaries so terms like "torn"
    # do not match unrelated words like "tornado".
    if " " not in banned_norm:
        return re.search(rf"(?<![a-z0-9]){re.escape(banned_norm)}(?![a-z0-9])", text_norm) is not None

    return has_keyword(text_norm, banned_norm)


def parse_euro(value: str) -> float:
    cleaned = value.strip().replace(" ", "")

    if "," in cleaned and "." in cleaned:
        cleaned = cleaned.replace(".", "").replace(",", ".")
    elif "," in cleaned:
        cleaned = cleaned.replace(",", ".")

    return float(cleaned)


def format_euro(value: float) -> str:
    base = f"{value:,.2f}"
    base = base.replace(",", "X").replace(".", ",").replace("X", ".")
    return f"{base} €"


def filter_outliers(
    sales: list[SaleCandidate],
    low_deviation: float = 0.30,
    high_deviation: float = 0.25,
) -> tuple[list[SaleCandidate], list[SaleCandidate]]:
    """Return (kept, removed) splitting sales outside median*ratio bounds.

    By default, prices with deviation >30% below or >25% above the median
    are removed. Equivalent bounds: [median * 0.70, median * 1.25].
    The median is computed on the full list so it is not skewed by outliers.
    If fewer than 3 items are present nothing is removed.
    """
    if len(sales) < 3:
        return sales, []

    prices = sorted(s.price for s in sales)
    mid = len(prices) // 2
    if len(prices) % 2 == 1:
        median = prices[mid]
    else:
        median = (prices[mid - 1] + prices[mid]) / 2.0

    low_threshold = median * (1.0 - low_deviation)
    high_threshold = median * (1.0 + high_deviation)
    kept = [s for s in sales if low_threshold <= s.price <= high_threshold]
    removed = [s for s in sales if s.price < low_threshold or s.price > high_threshold]
    return kept, removed


def dedupe_sales(sales: list[SaleCandidate]) -> list[SaleCandidate]:
    seen: set[tuple[str, str]] = set()
    unique: list[SaleCandidate] = []
    for sale in sales:
        key = (normalize_text(sale.title), f"{sale.price:.2f}")
        if key in seen:
            continue
        seen.add(key)
        unique.append(sale)
    return unique


def extract_price(price_text: str) -> tuple[float, str] | None:
    amounts: list[float] = []
    for pattern in PRICE_PATTERNS:
        for match in pattern.finditer(price_text):
            amount_raw = match.group(1)
            try:
                amounts.append(parse_euro(amount_raw))
            except ValueError:
                continue

    if amounts:
        # Prefer the lowest explicit amount when eBay shows a range or
        # additional converted prices in the same field.
        amount = min(amounts)
        return amount, format_euro(amount)
    return None


def extract_row_price_candidates(row_text: str) -> list[float]:
    amounts: list[float] = []
    for pattern in PRICE_PATTERNS:
        for match in pattern.finditer(row_text):
            amount_raw = match.group(1)
            try:
                amounts.append(parse_euro(amount_raw))
            except ValueError:
                continue
    return amounts


def pick_international_base_price(
    row_text: str,
    listed_price: float,
    shipping: float | None,
    import_fees: float | None,
) -> float:
    """Choose the most likely item-only price from an international row.

    eBay can show multiple EUR amounts in one card (converted total, original
    listing price, shipping). We prefer the smallest non-fee amount as base
    item price and then add VAT/import and extra shipping separately.
    """
    candidates = extract_row_price_candidates(row_text)
    if not candidates:
        return listed_price

    def is_same_amount(a: float, b: float | None) -> bool:
        return b is not None and abs(a - b) < 0.02

    filtered = [
        amount
        for amount in candidates
        if not is_same_amount(amount, shipping) and not is_same_amount(amount, import_fees)
    ]
    if not filtered:
        return listed_price

    return min(filtered)


def extract_title_from_row_text(row_text: str) -> str:
    ui_noise_markers = [
        "venduti ",
        "nuovo | venditore",
        "usato | venditore",
        "venditore professionale",
        "viene aperta una nuova finestra o scheda",
        "proposta d'acquisto",
        "fai un'offerta",
        "acquista ora",
        "vedi inserzioni simili",
        "vendi un oggetto simile",
        "positivi",
        "per la consegna",
    ]

    lines = [line.strip() for line in row_text.splitlines() if line.strip()]
    for line in lines:
        line_norm = normalize_text(line)
        if "nuovo annuncio" in line_norm:
            line = re.sub(r"(?i)^nuovo annuncio\s*", "", line).strip()
            line_norm = normalize_text(line)
        if not line:
            continue
        if "€" in line or "eur" in line_norm:
            continue
        if any(marker in line_norm for marker in ui_noise_markers):
            continue
        if line_norm in {"acquista ora", "fai un'offerta", "asta", "spese di spedizione"}:
            continue
        if len(line_norm) < 4:
            continue

        # Ignore lines that are mostly digits/symbols.
        alnum_chars = sum(ch.isalnum() for ch in line_norm)
        alpha_chars = sum(ch.isalpha() for ch in line_norm)
        if alnum_chars > 0 and alpha_chars / alnum_chars < 0.35:
            continue

        return line
    return ""


def is_less_words_section_row(row_text: str) -> bool:
    text = normalize_text(row_text)
    # eBay sections that broaden the search or inject unrelated results.
    return (
        "meno parole" in text
        and (
            "risultati trovati" in text
            or "risultati per" in text
            or "correlate" in text
        )
    ) or (
        "venditori ebay internazionali" in text
        or "oggetti trovati dai venditori ebay internazionali" in text
    )


def is_international_row(row_text: str) -> bool:
    text = normalize_text(row_text)
    markers = [
        "venditori ebay internazionali",
        "oggetti trovati dai venditori ebay internazionali",
        "da stati uniti",
        "da regno unito",
        "da cina",
        "da giappone",
        "da canada",
        "da australia",
        "international",
    ]
    return any(marker in text for marker in markers)


def extract_shipping_cost(row_text: str) -> float | None:
    for pattern in SHIPPING_COST_PATTERNS:
        match = pattern.search(row_text)
        if not match:
            continue
        try:
            return parse_euro(match.group(1))
        except ValueError:
            continue
    return None


def extract_import_fees(row_text: str) -> float | None:
    text = normalize_text(row_text)
    for pattern in IMPORT_FEE_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        try:
            return parse_euro(match.group(1))
        except ValueError:
            continue
    return None


def query_from_url(url: str) -> str:
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    nkw_values = query.get("_nkw", [])
    if not nkw_values:
        return ""
    return nkw_values[0]


def tokenize(value: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", normalize_text(value))


def detect_required_markers(text: str) -> list[str]:
    text_norm = normalize_text(text)
    required: list[str] = []

    for canonical, aliases in REQUIRED_MARKER_GROUPS:
        if any(has_keyword(text_norm, alias) for alias in aliases):
            required.append(canonical)

    return required


def build_query_spec(
    product_name: str,
    product_url: str,
    required_number: str | None = None,
    allowed_title_numbers: list[str] | None = None,
) -> QuerySpec:
    query_text = query_from_url(product_url)
    source = normalize_text(query_text or product_name)
    tokens = tokenize(source)
    marker_source = f"{product_name} {source}".strip()
    required_markers = detect_required_markers(marker_source)

    clean: list[str] = []
    for token in tokens:
        if token in STOPWORDS:
            continue
        # Keep single-char tokens (like "l") because they are significant
        # for some Funko searches.
        if len(token) < 1:
            continue
        clean.append(token)
    anchor_token: str | None = None
    for token in clean:
        if len(token) >= 3:
            anchor_token = token
            break
    if anchor_token is None and clean:
        anchor_token = clean[0]

    return QuerySpec(
        phrase=source,
        tokens=clean,
        anchor_token=anchor_token,
        required_number=required_number,
        required_markers=required_markers,
        allowed_title_numbers=allowed_title_numbers,
    )


def has_token(text: str, token: str) -> bool:
    text_norm = normalize_text(text)
    token_norm = normalize_text(token)
    if not token_norm:
        return False
    if len(token_norm) == 1:
        return re.search(rf"(?<![a-z0-9]){re.escape(token_norm)}(?![a-z0-9])", text_norm) is not None
    return has_keyword(text_norm, token_norm)


def is_probable_bundle(title: str) -> bool:
    title_norm = normalize_text(title)

    if any(has_keyword(title_norm, marker) for marker in BUNDLE_KEYWORDS):
        return True

    if re.search(r"\b[2-9]\s*(x|pcs|pieces|pezzi|funko|pop|figure)\b", title_norm):
        return True

    return False


def is_relevant_title(title: str, query_spec: QuerySpec) -> bool:
    title_norm = normalize_text(title)

    if "funko" not in title_norm or "pop" not in title_norm:
        return False

    if not query_spec.tokens:
        return True

    if query_spec.anchor_token and not has_token(title_norm, query_spec.anchor_token):
        return False

    # Keep premium/sticker variants separated: if query asks for one of these
    # markers, the listing title must contain it too.
    for marker in query_spec.required_markers or []:
        marker_aliases = next((aliases for canonical, aliases in REQUIRED_MARKER_GROUPS if canonical == marker), [])
        if marker_aliases and not any(has_keyword(title_norm, alias) for alias in marker_aliases):
            return False

    # If a specific Funko Pop number is required, reject titles that contain
    # explicit numbers but not the required one. Titles with no numbers are
    # still allowed (some sellers omit the #NNN in title text).
    if query_spec.required_number:
        num = re.escape(query_spec.required_number)
        has_required = re.search(rf"(?<![0-9]){num}(?![0-9])", title_norm) is not None
        if not has_required:
            any_numbers = re.search(r"\b[0-9]+\b", title_norm) is not None
            if any_numbers:
                return False
    elif query_spec.allowed_title_numbers is not None:
        # For products with no required number, allow only explicitly whitelisted
        # numbers in title (e.g. "2" for 2-pack products).
        title_numbers_raw = re.findall(r"\b[0-9]+\b", title_norm)
        if title_numbers_raw:
            title_numbers = {str(int(n)) for n in title_numbers_raw}
            allowed_numbers = {str(int(n)) for n in query_spec.allowed_title_numbers if str(n).isdigit()}
            if any(number not in allowed_numbers for number in title_numbers):
                return False

    full_hits = sum(1 for token in query_spec.tokens if has_token(title_norm, token))

    # If the normalized query phrase appears as-is, trust the result.
    if query_spec.phrase and has_keyword(title_norm, query_spec.phrase):
        return True

    # Require strong overlap with the original query to avoid unrelated sold items.
    token_count = len(query_spec.tokens)
    if token_count <= 2:
        needed = token_count
    else:
        needed = min(token_count, max(2, int(round(token_count * 0.6))))
    return full_hits >= needed


def _safe_page_title(page: Any, retries: int = 3) -> str:
    for _ in range(max(1, retries)):
        try:
            return page.title()
        except PlaywrightError as exc:
            if "Execution context was destroyed" not in str(exc):
                return ""
            page.wait_for_timeout(200)
    return ""


def _safe_page_body_text(page: Any, retries: int = 3) -> str:
    for _ in range(max(1, retries)):
        try:
            return page.inner_text("body")
        except PlaywrightError as exc:
            if "Execution context was destroyed" not in str(exc):
                return ""
            page.wait_for_timeout(200)
    return ""


def is_access_denied(page: Any) -> bool:
    title = normalize_text(_safe_page_title(page))
    body = normalize_text(_safe_page_body_text(page))
    return (
        "access denied" in title
        or "you don't have permission" in body
        or "errors.edgesuite.net" in body
    )


def has_no_exact_match(page: Any) -> bool:
    body = normalize_text(_safe_page_body_text(page))
    return (
        "nessuna corrispondenza esatta trovata" in body
        or "no exact matches found" in body
    )


def exact_result_count(page: Any, query_phrase: str) -> int | None:
    body = normalize_text(_safe_page_body_text(page))
    phrase = normalize_text(query_phrase)
    if not phrase:
        return None

    pattern = re.compile(
        rf"([0-9][0-9\.]*)\s+risultat(?:o|i)\s+per\s+{re.escape(phrase)}(?:\b|\s)",
        re.IGNORECASE,
    )
    match = pattern.search(body)
    if not match:
        return None

    try:
        return int(match.group(1).replace(".", ""))
    except ValueError:
        return None


def is_transient_wait_page(page: Any) -> bool:
    body = normalize_text(_safe_page_body_text(page))
    return (
        "ci scusiamo per l'attesa" in body
        or "ci scusiamo per l attesa" in body
        or "sorry for the wait" in body
        or "sorry for the delay" in body
        or "controllo del browser prima dell'accesso a ebay" in body
        or "controllo del browser prima dell accesso a ebay" in body
        or "a breve verrai reindirizzato ai contenuti richiesti" in body
        or "attendi qualche istante" in body
        or "browser check before accessing ebay" in body
    )


def wait_for_ebay_results(page: Any, timeout_ms: int) -> None:
    wait_step_ms = 1000
    max_wait_ms = min(timeout_ms, 15000)
    elapsed_ms = 0

    while elapsed_ms < max_wait_ms:
        denied = is_access_denied(page)
        waiting = is_transient_wait_page(page)

        if denied and not waiting:
            return

        if not denied and not waiting:
            rows = listing_rows(page).count()
            if rows > 0 or has_no_exact_match(page):
                return

        page.wait_for_timeout(wait_step_ms)
        elapsed_ms += wait_step_ms


def navigate_with_retries(
    page: Any,
    *,
    url: str,
    timeout_ms: int,
    max_denied_retries: int,
    manual_challenge: bool,
    name: str,
    phase_label: str,
) -> bool:
    for attempt in range(1, max_denied_retries + 1):
        page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        wait_for_ebay_results(page, timeout_ms)
        if not is_access_denied(page):
            return True

        if manual_challenge:
            print(
                f"[INFO] Access Denied per '{name}' ({phase_label}, tentativo {attempt}/{max_denied_retries})."
            )
            print("[INFO] Risolvi eventuale challenge nel browser aperto, poi premi INVIO qui.")
            input()
            wait_for_ebay_results(page, timeout_ms)
            if not is_access_denied(page):
                return True

        if attempt < max_denied_retries:
            print(
                f"[WARN] Access Denied per '{name}' ({phase_label}, tentativo {attempt}/{max_denied_retries}), riprovo subito..."
            )
    return False


def active_listings_url(sold_url: str) -> str:
    """Return a copy of a sold-filter eBay URL with sold/completed params removed."""
    parsed = urlparse(sold_url)
    params = parse_qs(parsed.query, keep_blank_values=True)
    for key in ("LH_Sold", "LH_Complete", "rt"):
        params.pop(key, None)
    # Only fixed-price listings (buy-it-now), exclude auctions.
    params["LH_BIN"] = ["1"]
    new_query = urlencode(params, doseq=True)
    return parsed._replace(query=new_query).geturl()


def with_pref_loc(search_url: str, pref_loc: int) -> str:
    parsed = urlparse(search_url)
    params = parse_qs(parsed.query, keep_blank_values=True)
    params["LH_PrefLoc"] = [str(pref_loc)]
    new_query = urlencode(params, doseq=True)
    return parsed._replace(query=new_query).geturl()


def listing_rows(page: Any) -> Any:
    primary = page.locator("li.s-item")
    if primary.count() > 0:
        return primary

    # eBay occasionally serves alternate SRP markup.
    fallback = page.locator("ul.srp-results > li")
    if fallback.count() > 0:
        return fallback

    return page.locator("li[data-view*='iid:']")


def collect_valid_sales_from_page(
    page: Any,
    *,
    query_spec: QuerySpec,
    normalized_banned: list[str],
    max_valid_sales: int,
    max_allowed_price: float | None = None,
    stop_on_related_sections: bool = True,
    adjust_international_costs: bool = False,
    force_international_rows: bool = False,
) -> tuple[list[SaleCandidate], int, int, int, int]:
    items = listing_rows(page)
    count = items.count()

    scanned_rows = 0
    excluded_banned = 0
    excluded_irrelevant = 0
    excluded_bundle = 0
    valid_sales: list[SaleCandidate] = []

    for i in range(count):
        row = items.nth(i)

        row_text_raw = row.inner_text().strip()
        if not row_text_raw:
            continue

        if stop_on_related_sections and is_less_words_section_row(row_text_raw):
            break

        title_loc = row.locator("h3.s-item__title, h3")
        title = ""
        if title_loc.count() > 0:
            title = title_loc.first.inner_text().strip()
        if not title:
            title = extract_title_from_row_text(row_text_raw)
        if not title:
            continue

        title_norm = normalize_text(title)
        if "risultati corrispondenti" in title_norm:
            continue
        if title_norm.startswith("nuovo annuncio"):
            title_norm = title_norm.replace("nuovo annuncio", "", 1).strip()
            title = re.sub(r"(?i)^nuovo annuncio\s*", "", title).strip()

        # Prefer eBay primary listing price node first.
        price_text = ""
        primary_price_loc = row.locator("span.s-item__price")
        if primary_price_loc.count() > 0:
            price_text = primary_price_loc.first.inner_text().strip()

        if not price_text:
            fallback_price_loc = row.locator("span[class*='price'], span:has-text('EUR'), span:has-text('€')")
            if fallback_price_loc.count() > 0:
                price_text = fallback_price_loc.first.inner_text().strip()

        if not price_text:
            price_text = row_text_raw

        price_info = extract_price(price_text)
        if price_info is None:
            continue

        row_text = normalize_text(row_text_raw)
        scanned_rows += 1

        if any(has_banned_match(row_text, banned) for banned in normalized_banned):
            excluded_banned += 1
            continue

        if is_probable_bundle(title_norm):
            excluded_bundle += 1
            continue

        if not is_relevant_title(title_norm, query_spec):
            excluded_irrelevant += 1
            continue

        value, display = price_info

        is_international = force_international_rows or is_international_row(row_text_raw)
        if adjust_international_costs and is_international:
            shipping = extract_shipping_cost(row_text_raw) or 0.0
            import_fees = extract_import_fees(row_text_raw)
            base_value = pick_international_base_price(
                row_text=row_text_raw,
                listed_price=value,
                shipping=shipping,
                import_fees=import_fees,
            )
            if import_fees is None:
                # Use VAT-like fallback when explicit import/tax fees are absent.
                import_fees = base_value * ESTIMATED_IMPORT_RATE
            extra_shipping = max(0.0, shipping - 10.0)
            value = base_value + import_fees + extra_shipping
            display = format_euro(value)

        if max_allowed_price is not None and value > max_allowed_price:
            excluded_irrelevant += 1
            continue

        valid_sales.append(SaleCandidate(title=title, price=value, price_display=display))

        if len(valid_sales) >= max_valid_sales:
            break

    return valid_sales, scanned_rows, excluded_banned, excluded_irrelevant, excluded_bundle


def scrape_product(
    page: Any,
    *,
    name: str,
    url: str,
    quantity: int,
    banned_words: list[str],
    max_valid_sales: int,
    timeout_ms: int,
    manual_challenge: bool,
    required_number: str | None = None,
    allowed_title_numbers: list[str] | None = None,
) -> ProductResult:
    max_denied_retries = 3
    max_allowed_price = 200.0

    if not navigate_with_retries(
        page,
        url=url,
        timeout_ms=timeout_ms,
        max_denied_retries=max_denied_retries,
        manual_challenge=manual_challenge,
        name=name,
        phase_label="venduti",
    ):
        return ProductResult(
            name=name,
            url=url,
            quantity=quantity,
            query_tokens=build_query_spec(name, url).tokens,
            scanned_rows=0,
            valid_rows=0,
            excluded_banned=0,
            excluded_irrelevant=0,
            excluded_bundle=0,
            used_sales=[],
            average_price=None,
            average_price_display=None,
            note="Access Denied da eBay dopo 3 tentativi (fase venduti).",
        )

    query_spec = build_query_spec(
        name,
        url,
        required_number=required_number,
        allowed_title_numbers=allowed_title_numbers,
    )
    sold_page_no_exact_match = has_no_exact_match(page)
    sold_exact_results = exact_result_count(page, query_spec.phrase)

    items = listing_rows(page)
    count = items.count()

    scanned_rows = 0
    valid_rows = 0
    excluded_banned = 0
    excluded_irrelevant = 0
    excluded_bundle = 0

    normalized_banned = [normalize_text(word) for word in banned_words]

    valid_sales: list[SaleCandidate] = []
    sold_international_used = False
    sold_it_found = 0
    sold_intl_found = 0

    if not sold_page_no_exact_match:
        (
            valid_sales,
            scanned_rows,
            excluded_banned,
            excluded_irrelevant,
            excluded_bundle,
        ) = collect_valid_sales_from_page(
            page,
            query_spec=query_spec,
            normalized_banned=normalized_banned,
            max_valid_sales=max_valid_sales,
            max_allowed_price=max_allowed_price,
            adjust_international_costs=True,
        )

        if not valid_sales:
            wait_for_ebay_results(page, timeout_ms)
            sold_page_no_exact_match = has_no_exact_match(page)
            sold_exact_results = exact_result_count(page, query_spec.phrase)
            items = listing_rows(page)
            count = items.count()
            if not sold_page_no_exact_match:
                (
                    valid_sales,
                    scanned_rows,
                    excluded_banned,
                    excluded_irrelevant,
                    excluded_bundle,
                ) = collect_valid_sales_from_page(
                    page,
                    query_spec=query_spec,
                    normalized_banned=normalized_banned,
                    max_valid_sales=max_valid_sales,
                    max_allowed_price=max_allowed_price,
                    adjust_international_costs=True,
                )

    sold_it_found = len(valid_sales)
    print(f"[FLOW] 1) cerco tra venduti italiani ({sold_it_found} trovati) ({'ok' if sold_it_found > 0 else 'ko'})")

    # ── Fallback before active listings: sold international (LH_PrefLoc=98) ──
    if len(valid_sales) <= 2:
        sold_international_url = with_pref_loc(url, 98)
        if sold_international_url != url:
            sold_intl_ok = navigate_with_retries(
                page,
                url=sold_international_url,
                timeout_ms=timeout_ms,
                max_denied_retries=max_denied_retries,
                manual_challenge=manual_challenge,
                name=name,
                phase_label="venduti internazionali",
            )

            if sold_intl_ok:
                (
                    sold_intl_valid,
                    sold_intl_scanned,
                    sold_intl_excluded_banned,
                    sold_intl_excluded_irrelevant,
                    sold_intl_excluded_bundle,
                ) = collect_valid_sales_from_page(
                    page,
                    query_spec=query_spec,
                    normalized_banned=normalized_banned,
                    max_valid_sales=max_valid_sales,
                    max_allowed_price=max_allowed_price,
                    stop_on_related_sections=False,
                    adjust_international_costs=True,
                    force_international_rows=True,
                )

                # Merge with existing valid_sales (both local and international)
                sold_intl_found = len(sold_intl_valid)
                print(
                    f"[FLOW] 2) cerco tra venduti internazionali ({sold_intl_found} trovati) "
                    f"({'ok' if sold_intl_found > 0 else 'ko'})"
                )
                if sold_intl_valid:
                    valid_sales = dedupe_sales(valid_sales + sold_intl_valid)
                    sold_international_used = True
                
                # Update diagnostics only if we had no prior scanned data
                if scanned_rows == 0 and sold_intl_scanned > 0:
                    scanned_rows = sold_intl_scanned
                    excluded_banned = sold_intl_excluded_banned
                    excluded_irrelevant = sold_intl_excluded_irrelevant
                    excluded_bundle = sold_intl_excluded_bundle

    # ── Outlier filter on sold results ────────────────────────────────────
    valid_sales, _ = filter_outliers(valid_sales)
    valid_rows = len(valid_sales)

    avg_price: float | None = None
    avg_price_display: str | None = None

    if valid_sales:
        avg_price = sum(item.price for item in valid_sales) / len(valid_sales)
        avg_price_display = format_euro(avg_price)

    note: str | None = None
    if valid_sales and sold_international_used:
        note = "Prezzi da venduti: merge di locale + internazionali."
    elif sold_page_no_exact_match:
        note = "Nessuna corrispondenza esatta trovata; uso annunci attivi."
    elif sold_exact_results and not valid_sales and scanned_rows == 0:
        note = f"eBay indica {sold_exact_results} risultato/i esatti nei venduti, ma i record non sono stati letti correttamente; fallback ai listati attivi non usato."
    elif count == 0:
        note = "Nessuna riga risultati trovata. Possibile blocco anti-bot o markup eBay cambiato."
    elif not valid_sales:
        note = "Nessuna vendita valida trovata con i filtri correnti."

    sold_sales = list(valid_sales)
    sold_avg = avg_price
    sold_avg_display = avg_price_display
    sold_scanned_rows = scanned_rows
    sold_valid_rows = valid_rows
    sold_excluded_banned = excluded_banned
    sold_excluded_irrelevant = excluded_irrelevant
    sold_excluded_bundle = excluded_bundle
    sold_note = note

    # ── Active listings (comparison market) ───────────────────────────────
    can_fallback_to_active = (
        sold_exact_results in (None, 0)
        or (sold_exact_results and scanned_rows > 0)
    )
    fallback_attempted = False
    fallback_scanned_rows = 0
    active_valid: list[SaleCandidate] = []
    active_avg: float | None = None
    active_avg_display: str | None = None
    active_scanned_rows = 0
    active_excluded_banned = 0
    active_excluded_irrelevant = 0
    active_excluded_bundle = 0
    active_note: str | None = None
    active_it_found = 0
    active_intl_found = 0

    if can_fallback_to_active:
        fallback_url = active_listings_url(url)
        if fallback_url != url:
            fallback_attempted = True
            fallback_ok = navigate_with_retries(
                page,
                url=fallback_url,
                timeout_ms=timeout_ms,
                max_denied_retries=max_denied_retries,
                manual_challenge=manual_challenge,
                name=name,
                phase_label="annunci attivi",
            )

            if fallback_ok:
                (
                    fb_valid,
                    fb_scanned,
                    fb_excluded_banned,
                    fb_excluded_irrelevant,
                    fb_excluded_bundle,
                ) = collect_valid_sales_from_page(
                    page,
                    query_spec=query_spec,
                    normalized_banned=normalized_banned,
                    max_valid_sales=max_valid_sales,
                    max_allowed_price=max_allowed_price,
                    adjust_international_costs=True,
                )
                active_it_found = len(fb_valid)
                print(
                    f"[FLOW] 3) cerco tra in vendita italiani ({active_it_found} trovati) "
                    f"({'ok' if active_it_found > 0 else 'ko'})"
                )
                fallback_scanned_rows = fb_scanned

                # If fallback has few local/strict results, broaden to all sections
                # in current pref location (related + international blocks shown in page).
                if len(fb_valid) <= 3:
                    (
                        fb_expanded,
                        fb_expanded_scanned,
                        fb_exp_excluded_banned,
                        fb_exp_excluded_irrelevant,
                        fb_exp_excluded_bundle,
                    ) = collect_valid_sales_from_page(
                        page,
                        query_spec=query_spec,
                        normalized_banned=normalized_banned,
                        max_valid_sales=max_valid_sales,
                        max_allowed_price=max_allowed_price,
                        stop_on_related_sections=False,
                        adjust_international_costs=True,
                    )
                    if len(fb_expanded) >= len(fb_valid):
                        fb_valid = dedupe_sales(fb_expanded)
                        fb_scanned = fb_expanded_scanned
                        fb_excluded_banned = fb_exp_excluded_banned
                        fb_excluded_irrelevant = fb_exp_excluded_irrelevant
                        fb_excluded_bundle = fb_exp_excluded_bundle
                    active_expanded_found = len(fb_valid)
                    print(
                        f"[FLOW] 3b) amplio a tutte le sezioni pagina italiana ({active_expanded_found} trovati) "
                        f"({'ok' if active_expanded_found > 0 else 'ko'})"
                    )

                # User rule: if <=3 valid listings after scanning all sections,
                # remove Italy preference and rerun with international pref loc 98.
                if len(fb_valid) <= 3:
                    intl_url = with_pref_loc(fallback_url, 98)
                    if intl_url != fallback_url:
                        intl_ok = navigate_with_retries(
                            page,
                            url=intl_url,
                            timeout_ms=timeout_ms,
                            max_denied_retries=max_denied_retries,
                            manual_challenge=manual_challenge,
                            name=name,
                            phase_label="annunci attivi internazionali",
                        )
                        if intl_ok:
                            (
                                intl_valid,
                                intl_scanned,
                                intl_excluded_banned,
                                intl_excluded_irrelevant,
                                intl_excluded_bundle,
                            ) = collect_valid_sales_from_page(
                                page,
                                query_spec=query_spec,
                                normalized_banned=normalized_banned,
                                max_valid_sales=max_valid_sales,
                                max_allowed_price=max_allowed_price,
                                stop_on_related_sections=False,
                                adjust_international_costs=True,
                                force_international_rows=True,
                            )
                            active_intl_found = len(intl_valid)
                            print(
                                f"[FLOW] 4) cerco tra in vendita internazionali ({active_intl_found} trovati) "
                                f"({'ok' if active_intl_found > 0 else 'ko'})"
                            )
                            fb_valid = dedupe_sales(fb_valid + intl_valid)
                            fb_scanned = max(fb_scanned, intl_scanned)
                            fb_excluded_banned = max(fb_excluded_banned, intl_excluded_banned)
                            fb_excluded_irrelevant = max(fb_excluded_irrelevant, intl_excluded_irrelevant)
                            fb_excluded_bundle = max(fb_excluded_bundle, intl_excluded_bundle)

                # Keep fallback diagnostics even when no valid sale survives filters.
                if fb_scanned > 0:
                    active_scanned_rows = fb_scanned
                    active_excluded_banned = fb_excluded_banned
                    active_excluded_irrelevant = fb_excluded_irrelevant
                    active_excluded_bundle = fb_excluded_bundle

                # ── Outlier filter on fallback results ────────────────────
                fb_valid, _ = filter_outliers(fb_valid)

                if fb_valid:
                    active_valid = fb_valid
                    active_avg = sum(item.price for item in fb_valid) / len(fb_valid)
                    active_avg_display = format_euro(active_avg)
                    active_note = "Prezzi da annunci attivi (nessuna vendita recente trovata)."
            else:
                active_note = "Access Denied su eBay anche nel fallback annunci attivi (3 tentativi)."

    # Compare markets: if active market is cheaper than sold market, prefer active.
    if sold_sales and active_valid and sold_avg is not None and active_avg is not None:
        sold_avg_flow = format_euro(sold_avg)
        active_avg_flow = format_euro(active_avg)
        if active_avg < sold_avg:
            valid_sales = active_valid
            avg_price = active_avg
            avg_price_display = active_avg_display
            scanned_rows = active_scanned_rows or sold_scanned_rows
            valid_rows = len(active_valid)
            excluded_banned = active_excluded_banned
            excluded_irrelevant = active_excluded_irrelevant
            excluded_bundle = active_excluded_bundle
            note = (
                "Prezzi da annunci attivi: media attivi inferiore ai venduti; "
                "uso il mercato corrente."
            )
            print(
                f"[FLOW] 5) medie -> venduti: {sold_avg_flow} | listati: {active_avg_flow} | uso: listati"
            )
        else:
            valid_sales = sold_sales
            avg_price = sold_avg
            avg_price_display = sold_avg_display
            scanned_rows = sold_scanned_rows
            valid_rows = sold_valid_rows
            excluded_banned = sold_excluded_banned
            excluded_irrelevant = sold_excluded_irrelevant
            excluded_bundle = sold_excluded_bundle
            note = sold_note
            print(
                f"[FLOW] 5) medie -> venduti: {sold_avg_flow} | listati: {active_avg_flow} | uso: venduti"
            )
    elif not sold_sales and active_valid:
        valid_sales = active_valid
        avg_price = active_avg
        avg_price_display = active_avg_display
        scanned_rows = active_scanned_rows
        valid_rows = len(active_valid)
        excluded_banned = active_excluded_banned
        excluded_irrelevant = active_excluded_irrelevant
        excluded_bundle = active_excluded_bundle
        note = active_note
        active_avg_flow = format_euro(active_avg) if active_avg is not None else "n/d"
        print(f"[FLOW] 5) medie -> venduti: n/d | listati: {active_avg_flow} | uso: listati")
    elif sold_sales:
        sold_avg_flow = format_euro(sold_avg) if sold_avg is not None else "n/d"
        print(f"[FLOW] 5) medie -> venduti: {sold_avg_flow} | listati: n/d | uso: venduti")

    if not valid_sales and fallback_attempted and note != "Access Denied su eBay anche nel fallback annunci attivi (3 tentativi).":
        if fallback_scanned_rows == 0:
            note = "Fallback annunci attivi eseguito ma nessuna riga risultati leggibile (pagina bloccata o markup non compatibile)."
        else:
            note = "Fallback annunci attivi eseguito, ma nessun annuncio ha superato i filtri correnti."

    return ProductResult(
        name=name,
        url=url,
        quantity=quantity,
        query_tokens=query_spec.tokens,
        scanned_rows=scanned_rows,
        valid_rows=valid_rows,
        excluded_banned=excluded_banned,
        excluded_irrelevant=excluded_irrelevant,
        excluded_bundle=excluded_bundle,
        used_sales=valid_sales,
        average_price=avg_price,
        average_price_display=avg_price_display,
        note=note,
    )


def load_products(config_path: Path) -> list[dict[str, Any]]:
    data = json.loads(config_path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("Il file di configurazione deve contenere una lista JSON.")
    return data


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Estrae prezzi medi dei Funko Pop venduti su eBay filtrando annunci non pertinenti e set/lotti."
    )
    parser.add_argument("--config", default="products.json", help="File JSON con i prodotti da analizzare.")
    parser.add_argument("--output-json", default="results.json", help="File JSON di output.")
    parser.add_argument("--headless", action="store_true", help="Esegue il browser in headless.")
    parser.add_argument(
        "--manual-challenge",
        action="store_true",
        help="In caso di challenge/access denied, consente risoluzione manuale nel browser prima del retry.",
    )
    parser.add_argument(
        "--browser-executable-path",
        default="",
        help="Percorso eseguibile browser (es. Brave). Se vuoto usa Chromium di Playwright.",
    )
    parser.add_argument(
        "--max-valid-sales",
        type=int,
        default=10,
        help="Numero massimo di vendite valide da usare per la media (default: 10).",
    )
    parser.add_argument(
        "--timeout-ms",
        type=int,
        default=120000,
        help="Timeout pagina in millisecondi.",
    )
    parser.add_argument(
        "--banned",
        nargs="*",
        default=DEFAULT_BANNED,
        help="Parole da escludere nel titolo annuncio (es. danneggiato, rovinato).",
    )

    args = parser.parse_args()

    if args.max_valid_sales < 1:
        raise ValueError("--max-valid-sales deve essere >= 1")

    products = load_products(Path(args.config))

    results: list[ProductResult] = []

    with sync_playwright() as p:
        launch_options: dict[str, Any] = {"headless": args.headless}
        if args.browser_executable_path.strip():
            launch_options["executable_path"] = args.browser_executable_path.strip()

        browser = p.chromium.launch(**launch_options)
        context = browser.new_context(locale="it-IT")
        page = context.new_page()

        for product in products:
            name = str(product.get("name") or product.get("url"))
            url = str(product["url"])
            quantity = max(1, int(product.get("quantity", 1)))
            required_number: str | None = str(product["required_number"]) if product.get("required_number") else None
            allowed_title_numbers: list[str] | None = None
            if isinstance(product.get("allowed_title_numbers"), list):
                normalized_allowed: list[str] = []
                for item in product["allowed_title_numbers"]:
                    value = str(item).strip()
                    if value.isdigit():
                        normalized_allowed.append(str(int(value)))
                allowed_title_numbers = normalized_allowed

            try:
                result = scrape_product(
                    page,
                    name=name,
                    url=url,
                    quantity=quantity,
                    banned_words=args.banned,
                    max_valid_sales=args.max_valid_sales,
                    timeout_ms=args.timeout_ms,
                    manual_challenge=args.manual_challenge,
                    required_number=required_number,
                    allowed_title_numbers=allowed_title_numbers,
                )
            except PlaywrightTimeoutError:
                result = ProductResult(
                    name=name,
                    url=url,
                    quantity=quantity,
                    query_tokens=[],
                    scanned_rows=0,
                    valid_rows=0,
                    excluded_banned=0,
                    excluded_irrelevant=0,
                    excluded_bundle=0,
                    used_sales=[],
                    average_price=None,
                    average_price_display=None,
                    note="Timeout durante il caricamento della pagina.",
                )

            results.append(result)

            if result.average_price is None:
                print(f"[MEDIA] {result.name}: nessun prezzo valido")
            else:
                line_total = result.average_price * result.quantity
                source_label = "listati" if (result.note and "annunci attivi" in result.note.lower()) else "vendite"
                print(
                    f"[MEDIA] {result.name}: {result.average_price_display} x {result.quantity} = {format_euro(line_total)} "
                    f"(su {len(result.used_sales)} {source_label})"
                )

            # Blank line between one POP flow and the next for readability.
            print()

        context.close()
        browser.close()

    payload: list[dict[str, Any]] = []
    values_list: list[tuple[str, int, float, float, str]] = []

    for result in results:
        print(f"\n=== {result.name} ===")
        print(f"URL: {result.url}")
        print(f"Query tokens: {', '.join(result.query_tokens) if result.query_tokens else '-'}")
        print(f"Righe analizzate: {result.scanned_rows}")
        print(f"Vendite valide: {result.valid_rows}")
        print(f"Escluse per parole vietate: {result.excluded_banned}")
        print(f"Escluse per set/lotto: {result.excluded_bundle}")
        print(f"Escluse per non pertinenza: {result.excluded_irrelevant}")

        row_data: dict[str, Any] = {
            "name": result.name,
            "url": result.url,
            "quantity": result.quantity,
            "query_tokens": result.query_tokens,
            "scanned_rows": result.scanned_rows,
            "valid_rows": result.valid_rows,
            "excluded_banned": result.excluded_banned,
            "excluded_bundle": result.excluded_bundle,
            "excluded_irrelevant": result.excluded_irrelevant,
            "used_sales": [
                {
                    "title": sale.title,
                    "price": sale.price,
                    "price_display": sale.price_display,
                }
                for sale in result.used_sales
            ],
            "average_price": result.average_price,
            "average_price_display": result.average_price_display,
            "note": result.note,
        }

        if result.average_price is None:
            if result.note:
                print(f"Nota: {result.note}")
        else:
            line_total = result.average_price * result.quantity
            print(f"Prezzo medio valido: {result.average_price_display}")
            print(f"Subtotale ({result.quantity}x): {format_euro(line_total)}")
            detail_label = "Listati" if (result.note and "annunci attivi" in result.note.lower()) else "Vendite"
            print(f"{detail_label} usati per la media:")
            for idx, sale in enumerate(result.used_sales, start=1):
                print(f"  {idx}. {sale.price_display} | {sale.title[:120]}")

            values_list.append(
                (
                    result.name,
                    result.quantity,
                    result.average_price,
                    line_total,
                    result.average_price_display,
                )
            )
            row_data["line_total"] = line_total
            row_data["line_total_display"] = format_euro(line_total)

        payload.append(row_data)

    total_sum = sum(item[3] for item in values_list)
    total_examined_objects = sum(result.quantity for result in results)
    total_with_valid_price = sum(result.quantity for result in results if result.average_price is not None)

    # Show final value list from highest to lowest unit price.
    values_list.sort(key=lambda item: (-item[2], item[0]))

    print("\n=== ELENCO VALORI ===")
    if not values_list:
        print("Nessun valore valido disponibile.")
    else:
        for idx, (name, quantity, _unit_price, line_total, price_display) in enumerate(values_list, start=1):
            print(f"{idx}. {name}: {price_display} x {quantity} = {format_euro(line_total)}")
        print(f"Somma totale: {format_euro(total_sum)}")

    print(f"Numero oggetti esaminati (con quantita): {total_examined_objects}")
    print(f"Numero oggetti con prezzo valido: {total_with_valid_price}")

    output_data: dict[str, Any] = {
        "results": payload,
        "values": [
            {
                "name": name,
                "quantity": quantity,
                "unit_price": unit_price,
                "price_display": price_display,
                "line_total": line_total,
                "line_total_display": format_euro(line_total),
            }
            for name, quantity, unit_price, line_total, price_display in values_list
        ],
        "sum_total": total_sum,
        "sum_total_display": format_euro(total_sum),
        "objects_examined_total_quantity": total_examined_objects,
        "objects_with_valid_price_total_quantity": total_with_valid_price,
    }

    output_path = Path(args.output_json)
    output_path.write_text(json.dumps(output_data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nRisultati salvati in: {output_path}")


if __name__ == "__main__":
    main()

"""SEC EDGAR client: submissions, XBRL companyfacts and filing documents (spec §4).

- Every request carries `SEC_USER_AGENT` (SEC's fair-access policy). The header is only ever sent
  to SEC hosts: URLs are built here from CIK + accession, never taken from payloads.
- Requests are throttled to `MIN_INTERVAL_S` (5 req/s; SEC allows 10) and retried with backoff
  on 429/5xx.
- Every failure is a `ProviderError`, like the price providers.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode, urlsplit

import httpx

from aether.providers.prices import ProviderError

log = logging.getLogger(__name__)

SEC_HOSTS = frozenset({"www.sec.gov", "data.sec.gov", "efts.sec.gov"})
# M12: EDGAR full-text search (the endpoint behind efts.sec.gov/LATEST/search-index, checked
# 2026-10-08) and the ticker → CIK/exchange map.
FTS_URL = "https://efts.sec.gov/LATEST/search-index"
TICKERS_EXCHANGE_URL = "https://www.sec.gov/files/company_tickers_exchange.json"
FTS_PAGE_SIZE = 100
FTS_MAX_PAGES = 5
ARCHIVES = "https://www.sec.gov/Archives/edgar/data"
MIN_INTERVAL_S = 0.2
MAX_ATTEMPTS = 4
BACKOFF_S = 2.0

CIK_RE = re.compile(r"^\d{1,10}$")
ACCESSION_RE = re.compile(r"^\d{10}-\d{2}-\d{6}$")
# Primary documents are plain file names, optionally under an XSL rendering directory.
DOC_RE = re.compile(r"^(?:xsl[A-Za-z0-9]+/)?[A-Za-z0-9][A-Za-z0-9._\-]{0,199}$")
UA_RE = re.compile(r"\S+@\S+\.\S+")


def cik10(cik: str | int) -> str:
    s = str(cik).strip()
    if not CIK_RE.fullmatch(s):
        raise ValueError(f"bad CIK {cik!r}")
    return s.zfill(10)


def check_accession(accession: str) -> str:
    if not ACCESSION_RE.fullmatch(accession):
        raise ValueError(f"bad accession {accession!r}")
    return accession


def filing_folder(cik: str, accession: str) -> str:
    return f"{ARCHIVES}/{int(cik10(cik))}/{check_accession(accession).replace('-', '')}"


def index_url(cik: str, accession: str) -> str:
    return f"{filing_folder(cik, accession)}/{accession}-index.htm"


def raw_doc_name(primary_doc: str) -> str:
    """`xslF345X06/form4.xml` is SEC's HTML rendering; the raw XML is `form4.xml`."""
    if not DOC_RE.fullmatch(primary_doc) or ".." in primary_doc:
        raise ValueError(f"unexpected primary document name {primary_doc!r}")
    return primary_doc.rsplit("/", 1)[-1]


def document_url(cik: str, accession: str, primary_doc: str, *, raw: bool = False) -> str:
    name = raw_doc_name(primary_doc)  # also validates the name
    return f"{filing_folder(cik, accession)}/{name if raw else primary_doc}"


@dataclass
class EdgarClient:
    user_agent: str
    client: httpx.Client = field(default_factory=lambda: httpx.Client(timeout=60))
    min_interval_s: float = MIN_INTERVAL_S
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic
    requests_made: int = 0
    _last: float = field(default=-1e9, repr=False)

    def __post_init__(self) -> None:
        # SEC asks for "Company Name admin@example.com"; refuse to run without a contact.
        if not self.user_agent or not UA_RE.search(self.user_agent):
            raise ValueError("SEC_USER_AGENT must be set to 'Name email@domain'")

    def _throttle(self) -> None:
        wait = self._last + self.min_interval_s - self.clock()
        if wait > 0:
            self.sleep(wait)
        self._last = self.clock()

    def get(self, url: str) -> httpx.Response:
        host = urlsplit(url).hostname
        if urlsplit(url).scheme != "https" or host not in SEC_HOSTS:
            raise ProviderError(f"refusing non-SEC URL {url!r}")
        last_exc: Exception | None = None
        for attempt in range(MAX_ATTEMPTS):
            self._throttle()
            self.requests_made += 1
            try:
                resp = self.client.get(
                    url,
                    headers={"User-Agent": self.user_agent, "Accept-Encoding": "gzip, deflate"},
                    follow_redirects=False,
                )
            except httpx.HTTPError as exc:
                last_exc = exc
            else:
                if resp.status_code == 200:
                    return resp
                if resp.status_code not in (429, 500, 502, 503, 504):
                    raise ProviderError(f"SEC {resp.status_code} for {url}")
                last_exc = ProviderError(f"SEC {resp.status_code} for {url}")
            if attempt + 1 < MAX_ATTEMPTS:
                self.sleep(BACKOFF_S * 2**attempt)
        raise ProviderError(f"SEC request failed after {MAX_ATTEMPTS} attempts: {last_exc}")

    def get_json(self, url: str) -> dict[str, Any]:
        try:
            data = self.get(url).json()
        except ValueError as exc:
            raise ProviderError(f"SEC returned invalid JSON for {url}") from exc
        if not isinstance(data, dict):
            raise ProviderError(f"SEC JSON for {url} is not an object")
        return data

    # ------------------------------------------------------------------ endpoints

    def submissions(self, cik: str) -> dict[str, Any]:
        return self.get_json(f"https://data.sec.gov/submissions/CIK{cik10(cik)}.json")

    def submissions_page(self, name: str) -> dict[str, Any]:
        """Older filings pages listed in `filings.files[].name` (CIK..-submissions-001.json)."""
        if not re.fullmatch(r"CIK\d{10}-submissions-\d{3}\.json", name):
            raise ProviderError(f"unexpected submissions page name {name!r}")
        return self.get_json(f"https://data.sec.gov/submissions/{name}")

    def companyfacts(self, cik: str) -> dict[str, Any]:
        return self.get_json(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik10(cik)}.json")

    def document(self, cik: str, accession: str, primary_doc: str, *, raw: bool = False) -> str:
        return self.get(document_url(cik, accession, primary_doc, raw=raw)).text

    def full_text_search(
        self, query: str, forms: tuple[str, ...], start: str, end: str
    ) -> list[dict[str, Any]]:
        """Every hit (`_id`, `_source`) for an exact-phrase query in `forms` filed `start..end`
        (YYYY-MM-DD), up to FTS_MAX_PAGES pages of FTS_PAGE_SIZE."""
        hits: list[dict[str, Any]] = []
        for page in range(FTS_MAX_PAGES):
            params = {
                "q": query,
                "forms": ",".join(forms),
                "dateRange": "custom",
                "startdt": start,
                "enddt": end,
            }
            if page:
                params["from"] = str(page * FTS_PAGE_SIZE)
            data = self.get_json(f"{FTS_URL}?{urlencode(params)}")
            block = data.get("hits")
            if not isinstance(block, dict) or not isinstance(block.get("hits"), list):
                raise ProviderError("EDGAR full-text search: unexpected response shape")
            batch = [h for h in block["hits"] if isinstance(h, dict)]
            hits += batch
            total = block.get("total", {})
            n = total.get("value") if isinstance(total, dict) else None
            if len(batch) < FTS_PAGE_SIZE or not isinstance(n, int) or len(hits) >= n:
                break
        return hits

    def company_tickers_exchange(self) -> dict[str, Any]:
        return self.get_json(TICKERS_EXCHANGE_URL)

    def close(self) -> None:
        self.client.close()

"""SEC EDGAR filings (official 8-K, 10-Q, 10-K, ... ) for watchlist companies.

SEC fair-access policy: identify yourself with a User-Agent like "Your Name you@example.com"
and stay under 10 requests per second.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from .httpclient import HttpClient, RateLimiter

log = logging.getLogger(__name__)

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{doc}"
INDEX_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{accession}-index.htm"

FORMS = {"8-K", "8-K/A", "10-Q", "10-K", "10-K/A", "6-K", "S-1", "S-4", "425", "SC 13D", "SC 13D/A", "DEF 14A"}

FORM_LABELS = {
    "8-K": "current report (8-K)",
    "8-K/A": "amended current report (8-K/A)",
    "10-Q": "quarterly report (10-Q)",
    "10-K": "annual report (10-K)",
    "10-K/A": "amended annual report (10-K/A)",
    "6-K": "foreign issuer report (6-K)",
    "S-1": "registration statement (S-1)",
    "S-4": "merger registration statement (S-4)",
    "425": "merger communication (425)",
    "SC 13D": "activist stake disclosure (13D)",
    "SC 13D/A": "amended stake disclosure (13D/A)",
    "DEF 14A": "proxy statement (DEF 14A)",
}

ITEM_LABELS = {
    "1.01": "material definitive agreement",
    "1.02": "termination of a material agreement",
    "1.05": "material cybersecurity incident",
    "2.01": "completed acquisition or disposition",
    "2.02": "results of operations",
    "2.03": "new direct financial obligation",
    "2.05": "exit or restructuring costs",
    "2.06": "material impairment",
    "3.01": "delisting notice",
    "4.01": "change of auditor",
    "4.02": "non-reliance on prior financials",
    "5.02": "director or officer change",
    "5.03": "bylaws amendment",
    "5.07": "shareholder vote results",
    "7.01": "Regulation FD disclosure",
    "8.01": "other events",
    "9.01": "financial statements and exhibits",
}


class SecEdgar:
    def __init__(self, user_agent: str, http: HttpClient | None = None):
        if not user_agent or "@" not in user_agent:
            raise ValueError('SEC_USER_AGENT must look like "Your Name you@example.com"')
        self.user_agent = user_agent
        self.http = http or HttpClient()
        self.limiter = RateLimiter(0.15)
        self._ticker_map: dict[str, int] | None = None

    def _get_json(self, url: str):
        return self.http.get_json(url, headers={"User-Agent": self.user_agent}, limiter=self.limiter)

    def resolve_cik(self, symbol: str) -> int | None:
        if self._ticker_map is None:
            data = self._get_json(TICKERS_URL)
            mapping: dict[str, int] = {}
            if isinstance(data, dict):
                for row in data.values():
                    try:
                        mapping[str(row["ticker"]).upper()] = int(row["cik_str"])
                    except (KeyError, TypeError, ValueError):
                        continue
            self._ticker_map = mapping
        return self._ticker_map.get(symbol.upper()) or self._ticker_map.get(symbol.upper().replace(".", "-"))

    def recent_filings(self, symbol: str, cik: int | None, since: datetime, limit: int = 3) -> list[dict]:
        cik = cik or self.resolve_cik(symbol)
        if not cik:
            return []
        data = self._get_json(SUBMISSIONS_URL.format(cik=int(cik)))
        if not isinstance(data, dict):
            return []
        tickers = [str(x).upper() for x in (data.get("tickers") or [])]
        if tickers and symbol.upper() not in tickers:
            log.warning("CIK %s does not list %s (tickers=%s); resolving again", cik, symbol, tickers)
            fixed = self.resolve_cik(symbol)
            if fixed and fixed != cik:
                return self.recent_filings(symbol, fixed, since, limit)
        company = data.get("name") or symbol
        recent = (data.get("filings") or {}).get("recent") or {}
        forms = recent.get("form") or []
        out: list[dict] = []
        for i, form in enumerate(forms):
            if form not in FORMS:
                continue
            filed = _parse_time(_at(recent, "acceptanceDateTime", i)) or _parse_date(_at(recent, "filingDate", i))
            if not filed or filed < since:
                continue
            accession = _at(recent, "accessionNumber", i) or ""
            acc = accession.replace("-", "")
            doc = _at(recent, "primaryDocument", i)
            url = (ARCHIVE_URL.format(cik=int(cik), acc=acc, doc=doc) if doc
                   else INDEX_URL.format(cik=int(cik), acc=acc, accession=accession))
            items = [x.strip() for x in str(_at(recent, "items", i) or "").split(",") if x.strip()]
            described = [ITEM_LABELS[x] for x in items if x in ITEM_LABELS and x != "9.01"]
            title = f"{company} files {FORM_LABELS.get(form, form)}"
            if described:
                title += ": " + "; ".join(described[:3])
            desc = _at(recent, "primaryDocDescription", i) or ""
            out.append({
                "title": title,
                "summary": f"SEC EDGAR filing {form} ({accession}). {desc}".strip(),
                "url": url,
                "published": filed,
                "form": form,
            })
            if len(out) >= limit:
                break
        return out


def _at(recent: dict, key: str, i: int):
    values = recent.get(key) or []
    return values[i] if i < len(values) else None


def _parse_time(value) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _parse_date(value) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(str(value), "%Y-%m-%d").replace(hour=21, tzinfo=timezone.utc)
    except ValueError:
        return None

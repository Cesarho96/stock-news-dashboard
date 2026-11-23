"""Deterministic quality checks on a validated edition, and the runtime quality gate.

Used online (every scheduled run, before publishing) and offline (eval runs in CI):

* units()               - the checkable pieces of an edition (top cards, top-20 notes, stories...)
* unverified_numbers()  - % moves, prices and $ figures that match neither MARKET DATA nor the
                          cited evidence (a cheap hallucination detector)
* advice_flags()        - buy/sell/hold language aimed at the reader (not allowed)
* decide()              - publish / degrade / withhold, from these checks and the LLM judge
* withhold()            - corrective action: drop the units the gate rejected so the composer
                          shows source headlines in their place
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .collect import ticker_matcher
from .evidence import EvidencePool
from .market import MarketSnapshot
from .settings import company_info

# ---- units --------------------------------------------------------------------------------


@dataclass
class Unit:
    id: str  # top.whatHappened | top20:NVDA | latest:2 | watchlist:NFLX | sectors:0
    section: str  # top | top20 | latest | watchlist | sectors
    text: str
    evidence: list[str]
    tickers: list[str]


def _join(*parts) -> str:
    return " ".join(p.strip() for p in parts if isinstance(p, str) and p.strip())


def units(a: dict) -> list[Unit]:
    out: list[Unit] = []
    top = a.get("top") or {}
    for key in ("whatHappened", "keyAlert", "marketMover"):
        b = top.get(key)
        if not isinstance(b, dict) or not b:
            continue
        tickers = list(b.get("tickers") or []) + ([b["ticker"]] if b.get("ticker") else [])
        out.append(Unit(f"top.{key}", "top", _join(b.get("title"), b.get("paragraph"), b.get("whatHappened"),
                                                     b.get("whyItMatters")), list(b.get("evidence") or []), tickers))
    for n in a.get("top20") or []:
        out.append(Unit(f"top20:{n.get('ticker')}", "top20", _join(n.get("note")), list(n.get("evidence") or []),
                        [n.get("ticker", "")]))
    for i, s in enumerate(a.get("latest") or []):
        out.append(Unit(f"latest:{i}", "latest", _join(s.get("title"), s.get("whatHappened"), s.get("whyItMatters")),
                        list(s.get("evidence") or []), list(s.get("tickers") or [])))
    for n in a.get("watchlist") or []:
        out.append(Unit(f"watchlist:{n.get('ticker')}", "watchlist", _join(n.get("summary"), n.get("whatToWatch")),
                        list(n.get("evidence") or []), [n.get("ticker", "")]))
    for i, s in enumerate(a.get("sectors") or []):
        out.append(Unit(f"sectors:{i}", "sectors", _join(s.get("title"), s.get("whatHappened"), s.get("whyItMatters")),
                        list(s.get("evidence") or []), list(s.get("tickers") or [])))
    return out


# ---- numbers --------------------------------------------------------------------------------
_PCT = re.compile(r"(?<![\w.,])[+\-−]?(\d{1,4}(?:[.,]\d{1,2})?)\s?(?:%|percent\b|por ciento\b)")
_USD = re.compile(r"\$\s?(\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)\s*(trillion|billion|million|tn|bn|mn|"
                  r"billones|mil millones|millones|[TBM])?(?![\w])", re.IGNORECASE)
_SCALE = {"trillion": 1e12, "tn": 1e12, "t": 1e12, "billones": 1e12, "billion": 1e9, "bn": 1e9, "b": 1e9,
          "mil millones": 1e9, "million": 1e6, "mn": 1e6, "m": 1e6, "millones": 1e6}
_INDEX_WORDS = re.compile(r"S&P|Nasdaq|Dow|Russell|index|índice|market|mercado", re.IGNORECASE)


def _num(raw: str) -> tuple[float, int]:
    """'3,2' -> (3.2, 1); '1,234.5' -> (1234.5, 1); returns (value, decimals)."""
    raw = raw.replace("−", "-")
    if re.fullmatch(r"\d+,\d{1,2}", raw):  # Spanish decimal comma
        raw = raw.replace(",", ".")
    raw = raw.replace(",", "")
    decimals = len(raw.split(".")[1]) if "." in raw else 0
    return float(raw), decimals


def _norm(text: str) -> str:
    return re.sub(r"(?<=\d),(?=\d{3}\b)", "", text or "")


def _in_text(value: float, decimals: int, haystack: str) -> bool:
    candidates = {f"{value:.{decimals}f}"}
    if decimals:
        candidates.add(f"{value:.{decimals}f}".rstrip("0").rstrip("."))
        candidates.add(f"{value:.{decimals}f}".replace(".", ","))
    return any(re.search(r"(?<![\d.])" + re.escape(c) + r"(?![\d])", haystack) for c in candidates)


class _Symbols:
    """Finds which known symbols (companies, index/sector ETFs) a text talks about."""

    def __init__(self, cfg: dict):
        companies = list(company_info(cfg).values())
        companies += [{"symbol": s["symbol"], "name": s.get("name") or "", "aliases": []} for s in cfg.get("strip", [])]
        companies += [{"symbol": s["etf"], "name": "", "aliases": []} for s in cfg.get("sectors", [])]
        self.match = ticker_matcher(companies)
        self.strip = [s["symbol"] for s in cfg.get("strip", [])]

    def __call__(self, unit: Unit) -> list[str]:
        found = list(dict.fromkeys(list(unit.tickers) + self.match(unit.text)))
        if _INDEX_WORDS.search(unit.text):
            found += [s for s in self.strip if s not in found]
        return found


def unverified_numbers(a: dict, snapshot: MarketSnapshot, pool: EvidencePool, cfg: dict) -> list[dict]:
    """Figures that match neither the quotes of the companies mentioned nor the cited evidence."""
    symbols_of = _Symbols(cfg)
    caps = {row["symbol"]: row.get("marketCap") for row in snapshot.universe}
    estimates: dict[str, list[float]] = {}
    for row in snapshot.earnings:  # the EARNINGS calendar is in the briefing too
        for key in ("epsEstimate", "revenueEstimate"):
            try:
                estimates.setdefault(str(row.get("symbol", "")).upper(), []).append(float(row[key]))
            except (KeyError, TypeError, ValueError):
                continue
    out: list[dict] = []
    for unit in units(a):
        evidence_text = _norm(" ".join(f"{e.title} {e.summary}" for e in (pool.get(i) for i in unit.evidence) if e))
        symbols = symbols_of(unit)
        quotes = [q for q in (snapshot.quote(s) for s in symbols) if q]
        known = [v for s in symbols for v in estimates.get(s, [])]
        text = _norm(unit.text)
        for m in _PCT.finditer(text):
            value, decimals = _num(m.group(1))
            tol = 0.5 if decimals == 0 else 0.06 if decimals == 1 else 0.006
            if any(abs(abs(q["changePct"]) - value) <= tol for q in quotes):
                continue
            if _in_text(value, decimals, evidence_text):
                continue
            out.append({"unit": unit.id, "value": m.group(0).strip()})
        for m in _USD.finditer(text):
            value, decimals = _num(m.group(1))
            scale = _SCALE.get((m.group(2) or "").lower(), 1.0)
            amount = value * scale
            if scale == 1.0:  # a price, a $ move or an EPS estimate
                fields = [q.get(k) for q in quotes for k in ("price", "high", "low", "open", "prevClose")]
                fields += [abs(q.get("change") or 0) for q in quotes] + known
                if any(f and abs(f - amount) <= max(0.011, abs(f) * 0.005) for f in fields):
                    continue
            else:  # market caps, revenue estimates and other large figures
                big = [caps.get(s) for s in symbols] + known
                if any(f and abs(f - amount) <= f * 0.03 for f in big):
                    continue
            if _in_text(value, decimals, evidence_text):
                continue
            out.append({"unit": unit.id, "value": m.group(0).strip()})
    return out


# ---- advice language ------------------------------------------------------------------------
_ADVICE = [re.compile(p, re.IGNORECASE) for p in (
    r"\b(you|investors|readers|traders)\s+(should|must|ought to)\s+(buy|sell|hold|add|trim|accumulate|dump|short)\b",
    r"\bwe\s+(recommend|suggest|advise)\b",
    r"\b(now is|it is|it's)\s+(a good|the right|the best)\s+time\s+to\s+(buy|sell)\b",
    r"\bbuy\s+the\s+dip\b",
    r"\b(deber[ií]as|deben|debe usted|conviene)\s+(comprar|vender|mantener)\b",
    r"\b(recomendamos|sugerimos|aconsejamos)\b",
    r"\b(es|buen|el mejor)\s+momento\s+(de|para)\s+(comprar|vender)\b",
)]


def advice_hits(text: str) -> list[str]:
    return [m.group(0) for rx in _ADVICE for m in rx.finditer(text or "")]


def advice_flags(a: dict) -> dict[str, list[str]]:
    """{unit id or 'intro': [phrases]} for reader-directed investment advice."""
    flags: dict[str, list[str]] = {}
    intro = advice_hits(_join(a.get("headline"), a.get("summary")))
    if intro:
        flags["intro"] = intro
    for unit in units(a):
        hits = advice_hits(unit.text)
        if hits:
            flags[unit.id] = hits
    for i, r in enumerate(a.get("radar") or []):
        hits = advice_hits(r.get("text", ""))
        if hits:
            flags[f"radar:{i}"] = hits
    return flags


# ---- checks, gate and corrective actions ---------------------------------------------------
@dataclass
class Checks:
    unverified: list[dict] = field(default_factory=list)
    advice: dict[str, list[str]] = field(default_factory=dict)
    citation_errors: int = 0  # evidence IDs the model invented (removed by the validator)
    first_pass_valid: bool = True
    final_valid: bool = True
    top20_coverage: float = 1.0  # share of top-20 rows with a validated AI note
    hard_issues: int = 0


def run_checks(a: dict, snapshot: MarketSnapshot, pool: EvidencePool, cfg: dict, report=None,
               first_report=None) -> Checks:
    top20 = snapshot.top20
    noted = {n.get("ticker") for n in a.get("top20") or [] if isinstance(n, dict)}
    fixes = list(getattr(first_report, "fixes", []) or []) + list(getattr(report, "fixes", []) or [])
    return Checks(
        unverified=unverified_numbers(a, snapshot, pool, cfg),
        advice=advice_flags(a),
        citation_errors=sum(1 for f in fixes if "removed unknown evidence id" in f),
        first_pass_valid=bool(first_report is None or first_report.ok),
        final_valid=bool(report is None or report.ok),
        top20_coverage=round(len(noted & set(top20)) / len(top20), 3) if top20 else 1.0,
        hard_issues=len(report.hard) if report is not None else 0,
    )


@dataclass
class Decision:
    action: str  # publish | degrade | withhold
    reason: str = ""
    withheld: list[str] = field(default_factory=list)  # unit ids removed before publishing


def decide(checks: Checks, judge=None, floor: float = 0.6, target: float = 0.8) -> Decision:
    """The runtime quality gate.

    withhold (fallback edition, no AI text): advice language in the headline/summary, or judge
    groundedness under the floor. degrade (publish with those units replaced by headlines):
    units with advice language, unverified figures or an 'unsupported' judge verdict, judge
    groundedness under the target, or validation issues left after the repair rounds.
    """
    if "intro" in checks.advice:
        return Decision("withhold", "quality gate: investment-advice language in the headline or summary")
    score = getattr(judge, "groundedness", None)
    if score is not None and score < floor:
        return Decision("withhold", f"quality gate: groundedness {score:.2f} below the floor {floor:.2f}")
    rejected = set(k for k in checks.advice if k != "intro")
    rejected |= {u["unit"] for u in checks.unverified}
    rejected |= set(getattr(judge, "unsupported", []) or [])
    reasons = []
    if rejected:
        reasons.append(f"{len(rejected)} section(s) withheld")
    if score is not None and score < target:
        reasons.append(f"groundedness {score:.2f} below the target {target:.2f}")
    if not checks.final_valid:
        reasons.append(f"{checks.hard_issues} validation issue(s) left")
    if reasons:
        return Decision("degrade", "quality gate: " + "; ".join(reasons), sorted(rejected))
    return Decision("publish")


def withhold(a: dict, unit_ids: list[str]) -> list[str]:
    """Remove rejected units from the analysis (in place). Returns the ids actually removed."""
    removed: list[str] = []
    wanted = set(unit_ids)
    top = a.get("top") or {}
    for key in ("whatHappened", "keyAlert", "marketMover"):
        if f"top.{key}" in wanted and top.get(key):
            top[key] = {}
            removed.append(f"top.{key}")
    for key in ("top20", "watchlist"):
        kept = []
        for n in a.get(key) or []:
            uid = f"{key}:{n.get('ticker')}"
            if uid in wanted:
                removed.append(uid)
            else:
                kept.append(n)
        if key in a:
            a[key] = kept
    for key in ("latest", "sectors", "radar"):
        items = a.get(key) or []
        kept = [s for i, s in enumerate(items) if f"{key}:{i}" not in wanted]
        removed += [f"{key}:{i}" for i in range(len(items)) if f"{key}:{i}" in wanted]
        if key in a:
            a[key] = kept
    return removed

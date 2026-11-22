"""JSON schemas: the edition analysis the model writes, and the research tools.

Written for Bedrock structured outputs: every object sets additionalProperties=false,
every property is required, and no length/number constraints are used (unsupported).
Length limits are enforced by the validator instead.

Schema version 2: "Top of the day" covers only the S&P 500 top 20 (three cards plus one
note per top-20 company); the watchlist block only exists for tickers outside the top 20.
"""

from __future__ import annotations

SCHEMA_VERSION = 2
THEMES = ["macro", "earnings", "ai-tech", "deals", "policy", "company"]
ALERT_CATEGORIES = ["alert", "risk", "opportunity", "macro", "earnings", "data"]
SENTIMENTS = ["bullish", "bearish", "neutral", "mixed"]
SIGNAL_KINDS = ["catalyst", "risk", "opportunity", "data"]
TICKER_PATTERN = "^[A-Z][A-Z0-9.\\-]{0,9}$"
EVIDENCE_PATTERN = "^E[0-9]{3,4}$"


def _obj(properties: dict, description: str | None = None) -> dict:
    out = {
        "type": "object",
        "additionalProperties": False,
        "required": list(properties.keys()),
        "properties": properties,
    }
    if description:
        out["description"] = description
    return out


def _str(description: str) -> dict:
    return {"type": "string", "description": description}


def analysis_schema(top20: list[str], extras: list[str], sector_ids: list[str]) -> dict:
    """Schema for one edition. Enums are sorted so a re-ranking alone does not change the
    schema (Bedrock caches compiled schemas)."""
    top_enum = sorted(top20)
    # No ranking this run (market data unavailable): accept any ticker; the validator still checks.
    top_ticker = {"type": "string", "enum": top_enum} if top_enum else {"type": "string", "pattern": TICKER_PATTERN}
    evidence = {
        "type": "array",
        "items": {"type": "string", "pattern": EVIDENCE_PATTERN},
        "description": "IDs of the evidence items that support this entry, most important first.",
    }
    tickers = {
        "type": "array",
        "items": {"type": "string", "pattern": TICKER_PATTERN},
        "description": "Tickers this entry is about (may be empty).",
    }
    top_tickers = {
        "type": "array",
        "items": {"type": "string", "pattern": TICKER_PATTERN},
        "description": "TOP 20 tickers this entry is about (at least one).",
    }
    title = _str("Headline, at most about 110 characters, no trailing period.")
    what = _str("1-3 sentences of verified facts (who, what, figures) taken from the cited evidence.")
    why = _str("1-2 sentences on the investor angle: earnings, guidance, sector, rates or sentiment.")
    sentiment = {"type": "string", "enum": SENTIMENTS,
                 "description": "Tone of today's news flow and price action (not a recommendation)."}

    lead = _obj({
        "title": title,
        "paragraph": _str("One paragraph, max 5 lines (~500 characters): how the top-20 companies traded "
                          "and why."),
        "tickers": top_tickers,
        "evidence": evidence,
    })
    alert = _obj({
        "category": {"type": "string", "enum": ALERT_CATEGORIES},
        "title": title,
        "whatHappened": what,
        "whyItMatters": why,
        "tickers": top_tickers,
        "evidence": evidence,
    })
    mover = _obj({
        "ticker": dict(top_ticker, description="The top-20 stock whose move matters most today."),
        "title": title,
        "whatHappened": what,
        "whyItMatters": why,
        "evidence": evidence,
    })
    top_note = _obj({
        "ticker": dict(top_ticker),
        "sentiment": sentiment,
        "note": _str("One sentence: why the stock moved today, citing the evidence; or say plainly that "
                     "there was no company-specific news."),
        "evidence": evidence,
    })
    story = _obj({
        "theme": {"type": "string", "enum": THEMES},
        "title": title,
        "whatHappened": what,
        "whyItMatters": why,
        "tickers": tickers,
        "evidence": evidence,
    })
    sector = _obj({
        "sector": {"type": "string", "enum": sector_ids},
        "title": title,
        "whatHappened": what,
        "whyItMatters": why,
        "tickers": tickers,
        "evidence": evidence,
    })
    signal = _obj({
        "kind": {"type": "string", "enum": SIGNAL_KINDS},
        "text": _str("One sentence: what to watch next and why (dates if known)."),
        "evidence": evidence,
    })
    props = {
        "headline": _str("The session in one line (max ~110 characters)."),
        "summary": _str("2-4 sentences: market tone, the dominant driver, and the open risk."),
        "top": _obj({"whatHappened": lead, "keyAlert": alert, "marketMover": mover}),
        "top20": {"type": "array", "items": top_note,
                  "description": "Exactly one note per TOP 20 company, in TOP 20 rank order."},
        "latest": {"type": "array", "items": story,
                   "description": "4-10 distinct stories from the news window (any company or macro), "
                                  "most important first."},
    }
    if extras:
        note = _obj({
            "ticker": {"type": "string", "enum": sorted(extras)},
            "sentiment": sentiment,
            "summary": _str("1-2 sentences: what drove the stock in this window, or say there was no "
                            "company news."),
            "whatToWatch": _str("One concrete upcoming item: earnings date, product event, ruling, data release."),
            "evidence": evidence,
        })
        props["watchlist"] = {"type": "array", "items": note,
                              "description": "Exactly one note per EXTRA WATCHLIST ticker."}
    props["sectors"] = {"type": "array", "items": sector,
                        "description": "0-2 sector stories per sector, only when the evidence supports them."}
    props["radar"] = {"type": "array", "items": signal, "description": "3-6 forward-looking items."}
    return _obj(props, description="One edition of the stock news dashboard.")


def tool_definitions() -> list[dict]:
    return [
        {
            "name": "get_company_news",
            "description": (
                "Fetch recent news for one US-listed company (any ticker). Returned items are added to "
                "the evidence list with new IDs you can cite."
            ),
            "input_schema": _obj({
                "symbol": {"type": "string", "pattern": TICKER_PATTERN, "description": "Ticker, e.g. SMCI."},
                "days": {"type": "integer", "description": "Days to look back, 1 to 7."},
            }),
        },
        {
            "name": "get_quotes",
            "description": (
                "Get the latest price and daily % change for up to 10 US tickers. Call this before "
                "mentioning a price move that is not in MARKET DATA."
            ),
            "input_schema": _obj({
                "symbols": {"type": "array", "items": {"type": "string", "pattern": TICKER_PATTERN},
                            "description": "Tickers, at most 10."},
            }),
        },
    ]

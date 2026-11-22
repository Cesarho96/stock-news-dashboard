"""JSON schemas: the edition analysis the model writes, and the research tools.

Written for Bedrock structured outputs: every object sets additionalProperties=false,
every property is required, and no length/number constraints are used (unsupported).
Length limits are enforced by the validator instead.
"""

from __future__ import annotations

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


def analysis_schema(watchlist: list[str], sector_ids: list[str]) -> dict:
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
    title = _str("Headline, at most about 110 characters, no trailing period.")
    what = _str("1-3 sentences of verified facts (who, what, figures) taken from the cited evidence.")
    why = _str("1-2 sentences on the investor angle: earnings, guidance, sector, rates or sentiment.")

    lead = _obj({
        "title": title,
        "paragraph": _str("One paragraph, max 5 lines (~500 characters): the session in one read."),
        "evidence": evidence,
    })
    alert = _obj({
        "category": {"type": "string", "enum": ALERT_CATEGORIES},
        "title": title,
        "whatHappened": what,
        "whyItMatters": why,
        "tickers": tickers,
        "evidence": evidence,
    })
    mover = _obj({
        "ticker": {"type": "string", "pattern": TICKER_PATTERN,
                   "description": "The stock that moved the most or matters most today; must appear in MARKET DATA or get_quotes results."},
        "title": title,
        "whatHappened": what,
        "whyItMatters": why,
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
    note = _obj({
        "ticker": {"type": "string", "enum": watchlist},
        "sentiment": {"type": "string", "enum": SENTIMENTS,
                      "description": "Tone of today's news flow and price action (not a recommendation)."},
        "summary": _str("1-2 sentences: what drove the stock in this window, or say there was no company news."),
        "whatToWatch": _str("One concrete upcoming item: earnings date, product event, ruling, data release."),
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
    return _obj({
        "headline": _str("The session in one line (max ~110 characters)."),
        "summary": _str("2-4 sentences: market tone, the dominant driver, and the open risk."),
        "top": _obj({"whatHappened": lead, "keyAlert": alert, "marketMover": mover}),
        "latest": {"type": "array", "items": story,
                   "description": "4-10 distinct stories from the news window, most important first."},
        "watchlist": {"type": "array", "items": note,
                      "description": "Exactly one note per watchlist ticker, in watchlist order."},
        "sectors": {"type": "array", "items": sector,
                    "description": "0-2 sector stories per sector, only when the evidence supports them."},
        "radar": {"type": "array", "items": signal, "description": "3-6 forward-looking items."},
    }, description="One edition of the stock news dashboard.")


def tool_definitions() -> list[dict]:
    return [
        {
            "name": "get_company_news",
            "description": (
                "Fetch recent news for one US-listed company (any ticker, including tickers outside the "
                "watchlist). Returned items are added to the evidence list with new IDs you can cite."
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
                "featuring a company that is not in MARKET DATA."
            ),
            "input_schema": _obj({
                "symbols": {"type": "array", "items": {"type": "string", "pattern": TICKER_PATTERN},
                            "description": "Tickers, at most 10."},
            }),
        },
    ]

"""Claude on Amazon Bedrock through InvokeModel (Anthropic Messages format).

InvokeModel takes an opaque JSON body, so the Lambda only needs the boto3 that ships
with the runtime. If a model/region rejects an optional feature (effort, structured
output, strict tools, adaptive thinking) the client drops that feature and retries.
That includes schemas too complex for constrained decoding ("The compiled grammar is
too large"): strict tools go first, then the JSON output schema. Without structured
output, the client describes the schema in the last user turn of every request that
has one, so callers never need to know which mode was used.
"""

from __future__ import annotations

import json
import logging
from typing import Any

log = logging.getLogger(__name__)


class LLMError(Exception):
    pass


SCHEMA_IN_PROMPT = "Return ONLY the JSON object, with no prose and no code fences. It must match this JSON schema:\n"
# Bedrock/Anthropic could not compile the schemas for constrained decoding (size or complexity).
TOO_COMPLEX = ("grammar", "too complex")


def with_schema_text(messages: list, schema: dict) -> list:
    """Copy of `messages` whose last user turn ends with the schema (structured output is off)."""
    block = {"type": "text", "text": SCHEMA_IN_PROMPT + json.dumps(schema, separators=(",", ":"))}
    out = list(messages)
    if out and out[-1].get("role") == "user":
        last = dict(out[-1])
        content = last.get("content")
        blocks = list(content) if isinstance(content, list) else [{"type": "text", "text": str(content or "")}]
        last["content"] = blocks + [block]
        out[-1] = last
    else:
        out.append({"role": "user", "content": [block]})
    return out


def _make_client(region: str):
    import boto3
    from botocore.config import Config

    return boto3.client(
        "bedrock-runtime",
        region_name=region,
        config=Config(read_timeout=420, connect_timeout=10, retries={"max_attempts": 3, "mode": "adaptive"}),
    )


def _error_info(exc: Exception) -> tuple[str, str] | None:
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return None
    err = response.get("Error") or {}
    return str(err.get("Code", "")), str(err.get("Message", ""))


class BedrockClaude:
    def __init__(self, model_id: str, region: str, effort: str = "medium", thinking: str = "adaptive",
                 max_tokens: int = 32000, client: Any = None):
        self.model_id = model_id
        self.region = region
        self.effort = effort
        self.max_tokens = max_tokens
        self._client = client
        self.features = {
            "effort": bool(effort),
            "format": True,
            "strict": True,
            "thinking": thinking == "adaptive",
        }
        self.usage = {"calls": 0, "input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0}
        self.downgrades: list[str] = []

    @property
    def client(self):
        if self._client is None:
            self._client = _make_client(self.region)
        return self._client

    @property
    def structured_output(self) -> bool:
        return self.features["format"]

    def _body(self, system: str, messages: list, tools, tool_choice, output_schema) -> dict:
        if output_schema is not None and not self.features["format"]:
            messages = with_schema_text(messages, output_schema)
        body: dict[str, Any] = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": self.max_tokens,
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            "messages": messages,
        }
        if tools:
            body["tools"] = [dict(t, strict=True) if self.features["strict"] else dict(t) for t in tools]
            body["tool_choice"] = tool_choice or {"type": "auto"}
        output_config: dict[str, Any] = {}
        if self.features["effort"] and self.effort:
            output_config["effort"] = self.effort
        if output_schema is not None and self.features["format"]:
            output_config["format"] = {"type": "json_schema", "schema": output_schema}
        if output_config:
            body["output_config"] = output_config
        if self.features["thinking"]:
            body["thinking"] = {"type": "adaptive"}
        return body

    def _downgrade(self, message: str, *, tools: bool = False, schema: bool = False) -> bool:
        msg = message.lower()
        order = []
        if any(k in msg for k in TOO_COMPLEX):
            # The schemas did not compile for constrained decoding. Strict tools add to the grammar, so
            # they go first; then the JSON output schema (described in the prompt from then on).
            order += (["strict"] if tools else []) + (["format"] if schema else [])
        if "effort" in msg:
            order.append("effort")
        if "strict" in msg:
            order.append("strict")
        if "thinking" in msg:
            order.append("thinking")
        if any(k in msg for k in ("output_config", "json_schema", "format", "structured")):
            order += ["effort", "format"]  # drop effort first, keep the schema if possible
        for feature in order:
            if self.features.get(feature):
                self.features[feature] = False
                self.downgrades.append(feature)
                log.warning("Bedrock rejected '%s'; retrying without it (%s)", feature, message[:300])
                return True
        return False

    def create(self, *, system: str, messages: list, tools: list | None = None,
               tool_choice: dict | None = None, output_schema: dict | None = None) -> dict:
        for _ in range(5):
            body = self._body(system, messages, tools, tool_choice, output_schema)
            try:
                resp = self.client.invoke_model(
                    modelId=self.model_id,
                    body=json.dumps(body),
                    contentType="application/json",
                    accept="application/json",
                )
            except Exception as exc:  # botocore ClientError and friends
                info = _error_info(exc)
                if info and info[0] == "ValidationException" and self._downgrade(
                        info[1], tools=bool(tools), schema=output_schema is not None):
                    continue
                if info:
                    raise LLMError(f"Bedrock {info[0]}: {info[1]}") from exc
                raise LLMError(f"Bedrock call failed: {exc}") from exc
            raw = resp["body"].read() if hasattr(resp.get("body"), "read") else resp.get("body")
            data = json.loads(raw)
            self._track(data.get("usage") or {})
            return data
        raise LLMError("Bedrock kept rejecting the request after removing optional features")

    def _track(self, usage: dict) -> None:
        self.usage["calls"] += 1
        self.usage["input"] += int(usage.get("input_tokens") or 0)
        self.usage["output"] += int(usage.get("output_tokens") or 0)
        self.usage["cacheRead"] += int(usage.get("cache_read_input_tokens") or 0)
        self.usage["cacheWrite"] += int(usage.get("cache_creation_input_tokens") or 0)

    def estimated_cost(self, price_in: float, price_out: float) -> float:
        u = self.usage
        billed_in = u["input"] + u["cacheWrite"] * 1.25 + u["cacheRead"] * 0.1
        return round((billed_in * price_in + u["output"] * price_out) / 1_000_000, 4)

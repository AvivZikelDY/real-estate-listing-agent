"""LLMod.ai client (real network implementation).

LLMod.ai is accessed as an OpenAI-compatible Chat Completions REST API
(confirmed via LLMod.ai's own `langchain_openai.ChatOpenAI` usage example:
`base_url="https://api.llmod.ai/v1"`, `model="<provider-model-name>"`).
Concretely this client assumes:

- `LLMOD_BASE_URL` already includes any version prefix (e.g. the `/v1` in
  `https://api.llmod.ai/v1`); this client only appends `/chat/completions`.
- `POST {LLMOD_BASE_URL}/chat/completions` with header
  `Authorization: Bearer <LLMOD_API_KEY>`.
- Request body: `{"model": ..., "messages": [...], "response_format":
  {"type": "json_schema", "json_schema": {...}}}`, i.e. the standard
  OpenAI structured-output ("strict" JSON schema) mechanism. No
  `temperature` override is sent: some models proxied through LLMod.ai
  (e.g. gpt-5-mini) reject `temperature=0` and only support the
  provider's default, so this client relies on that default rather than
  forcing a value. Response models with an open-ended field (e.g.
  `dict[str, Any]`, which strict mode cannot express) fall back to plain
  `{"type": "json_object"}` mode with the schema embedded as guidance
  text instead — still officially-supported structured JSON output, just
  without provider-side schema enforcement; this client's own Pydantic
  validation (plus its one repair attempt) enforces correctness in that
  case.
- Response body: the standard OpenAI shape, with the generated JSON text
  at `choices[0]["message"]["content"]`.
- Errors: the standard OpenAI-compatible error body
  `{"error": {"message": ..., "type": ..., "code": ...}}`.

None of this is guessed beyond what was explicitly confirmed to be an
OpenAI-compatible proxy; if LLMod.ai's actual behavior differs, only this
module needs to change (the rest of the app depends only on the abstract
`LLMClient` interface).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import httpx
from pydantic import BaseModel, ValidationError

from src.listing_agent.llm.client.base import LLMClient

if TYPE_CHECKING:
    from src.listing_agent.agent.step_tracer import StepTracer

# httpx.AsyncClient timeouts. Connect must fail fast; read is generous
# because LLM generations can be slow, but is still bounded (never hangs
# forever) per the "no more than 5 minutes to get an answer" requirement.
CONNECT_TIMEOUT_SECONDS = 10.0
READ_TIMEOUT_SECONDS = 300.0
WRITE_TIMEOUT_SECONDS = 30.0
POOL_TIMEOUT_SECONDS = 10.0

CHAT_COMPLETIONS_PATH = "/chat/completions"


def _make_strict_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Recursively rewrite a Pydantic JSON schema for OpenAI's strict mode.

    OpenAI-compatible structured output (`response_format.json_schema`
    with `"strict": True`) requires every object node to set
    `"additionalProperties": false` and list every property as
    `"required"` (nullable fields are expressed via a `type` union
    instead of by omitting them from `required`). Pydantic's
    `model_json_schema()` does not do this by default, so this function
    adjusts the schema to satisfy that requirement without changing what
    it validates.
    """
    schema = dict(schema)

    if schema.get("type") == "object" or "properties" in schema:
        properties = schema.get("properties")
        if properties is not None:
            schema["properties"] = {
                key: _make_strict_schema(value) for key, value in properties.items()
            }
            schema["required"] = list(properties.keys())
        schema["additionalProperties"] = False

    if "items" in schema:
        schema["items"] = _make_strict_schema(schema["items"])

    for combinator in ("anyOf", "oneOf", "allOf"):
        if combinator in schema:
            schema[combinator] = [_make_strict_schema(sub) for sub in schema[combinator]]

    defs = schema.get("$defs")
    if defs is not None:
        schema["$defs"] = {key: _make_strict_schema(value) for key, value in defs.items()}

    return schema


def _has_open_dict(schema: dict[str, Any], defs: dict[str, Any] | None = None) -> bool:
    """Detect a schema node representing an open dict (e.g. `dict[str, Any]`).

    OpenAI's strict JSON-schema mode requires every object to declare
    fixed, fully-required `properties` with `additionalProperties: false`.
    A field typed `dict[str, Any]` has no fixed keys, so it cannot be
    expressed there at all; this function is used to detect that case so
    the caller can fall back to non-strict `json_object` mode instead.
    """
    defs = defs if defs is not None else schema.get("$defs", {})

    if "$ref" in schema:
        ref_name = schema["$ref"].rsplit("/", 1)[-1]
        target = defs.get(ref_name)
        return bool(target) and _has_open_dict(target, defs)

    if schema.get("type") == "object" and "properties" not in schema:
        return True

    if "properties" in schema:
        if any(_has_open_dict(value, defs) for value in schema["properties"].values()):
            return True

    if "items" in schema and _has_open_dict(schema["items"], defs):
        return True

    for combinator in ("anyOf", "oneOf", "allOf"):
        if combinator in schema and any(_has_open_dict(sub, defs) for sub in schema[combinator]):
            return True

    return False

    defs = schema.get("$defs")
    if defs is not None:
        schema["$defs"] = {key: _make_strict_schema(value) for key, value in defs.items()}

    return schema


class LLModError(Exception):
    """Human-readable application error for any LLMod.ai request failure."""


class LLModClient(LLMClient):
    """Client for LLMod.ai's OpenAI-compatible Chat Completions API."""

    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        *,
        step_tracer: StepTracer | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        missing = [
            name
            for name, value in (
                ("LLMOD_API_KEY", api_key),
                ("LLMOD_BASE_URL", base_url),
                ("LLMOD_MODEL", model),
            )
            if not value or not value.strip()
        ]
        if missing:
            raise ValueError(
                "LLModClient is missing required configuration: "
                f"{', '.join(missing)}. Set these environment variables "
                "(see .env.example) before selecting LLM_MODE=llmod."
            )

        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._model = model
        # Only used to record a bounded structured-output repair attempt as
        # its own step. Never given secrets, auth headers, or env vars.
        self._step_tracer = step_tracer
        # Injectable for tests (httpx.MockTransport); None means real network.
        self._transport = transport

    async def generate_json(
        self,
        module: str,
        system_prompt: str,
        user_payload: dict,
        response_model: type[BaseModel],
    ) -> BaseModel:
        messages: list[dict[str, str]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(user_payload)},
        ]

        content = await self._request_completion(module, messages, response_model)
        parsed, parse_error = self._try_parse(content, response_model)
        if parsed is not None:
            return parsed

        # Exactly one repair attempt: ask the model to fix its own invalid
        # output, and record the attempt as its own "llm" step. No further
        # retries are made regardless of the outcome (bounded, not a loop).
        repair_messages = [
            *messages,
            {"role": "assistant", "content": content},
            {
                "role": "user",
                "content": (
                    "Your previous response was not valid JSON matching the "
                    f"required schema. Error: {parse_error}. Return ONLY the "
                    "corrected JSON object, with no extra text."
                ),
            },
        ]
        try:
            repaired_content = await self._request_completion(
                module, repair_messages, response_model
            )
        except LLModError as exc:
            self._record_repair(module, "error", messages, content, str(exc))
            raise

        repaired, repair_error = self._try_parse(repaired_content, response_model)
        if repaired is not None:
            self._record_repair(module, "ok", messages, content, repaired_content)
            return repaired

        self._record_repair(module, "error", messages, content, repair_error)
        raise LLModError(
            f"LLMod.ai returned structured output for module '{module}' that could not "
            f"be validated as {response_model.__name__}, even after one repair attempt. "
            f"Last validation error: {repair_error}"
        )

    async def _request_completion(
        self,
        module: str,
        messages: list[dict[str, str]],
        response_model: type[BaseModel],
    ) -> str:
        """Send one chat-completions request and return the raw message content."""
        raw_schema = response_model.model_json_schema()

        if _has_open_dict(raw_schema):
            # This response model has a field with no fixed keys (e.g.
            # `dict[str, Any]`), which OpenAI's strict json_schema mode
            # cannot express. Fall back to plain `json_object` mode (still
            # officially-supported structured JSON output) and include the
            # schema as guidance text so the model still knows the shape;
            # `generate_json`'s own Pydantic validation (plus its one
            # repair attempt) is what actually enforces correctness here.
            request_messages = [
                {
                    **messages[0],
                    "content": (
                        f"{messages[0]['content']}\n\nRespond with a single JSON object "
                        f"matching this JSON Schema exactly:\n{json.dumps(raw_schema)}"
                    ),
                },
                *messages[1:],
            ]
            response_format = {"type": "json_object"}
        else:
            request_messages = messages
            response_format = {
                "type": "json_schema",
                "json_schema": {
                    "name": response_model.__name__,
                    "schema": _make_strict_schema(raw_schema),
                    "strict": True,
                },
            }

        body = {
            "model": self._model,
            "messages": request_messages,
            "response_format": response_format,
        }
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        timeout = httpx.Timeout(
            connect=CONNECT_TIMEOUT_SECONDS,
            read=READ_TIMEOUT_SECONDS,
            write=WRITE_TIMEOUT_SECONDS,
            pool=POOL_TIMEOUT_SECONDS,
        )

        try:
            async with httpx.AsyncClient(timeout=timeout, transport=self._transport) as client:
                response = await client.post(
                    f"{self._base_url}{CHAT_COMPLETIONS_PATH}",
                    headers=headers,
                    json=body,
                )
        except httpx.TimeoutException as exc:
            raise LLModError(
                f"LLMod.ai request for module '{module}' timed out: {exc}"
            ) from exc
        except httpx.RequestError as exc:
            raise LLModError(
                f"LLMod.ai request for module '{module}' failed: {exc}"
            ) from exc

        if response.status_code >= 400:
            raise LLModError(self._describe_error(module, response))

        try:
            payload = response.json()
            return payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, ValueError) as exc:
            raise LLModError(
                f"LLMod.ai returned an unexpected response shape for module "
                f"'{module}': {exc}"
            ) from exc

    def _describe_error(self, module: str, response: httpx.Response) -> str:
        """Convert an OpenAI-compatible error body into a human-readable message."""
        message: str | None = None
        try:
            body: dict[str, Any] = response.json()
            error = body.get("error")
            if isinstance(error, dict):
                message = error.get("message")
        except ValueError:
            pass

        if message:
            return (
                f"LLMod.ai request for module '{module}' failed (HTTP "
                f"{response.status_code}): {message}"
            )
        return (
            f"LLMod.ai request for module '{module}' failed (HTTP {response.status_code}): "
            f"{response.text[:500]}"
        )

    @staticmethod
    def _try_parse(
        content: str, response_model: type[BaseModel]
    ) -> tuple[BaseModel | None, str | None]:
        try:
            data = json.loads(content)
        except json.JSONDecodeError as exc:
            return None, f"invalid JSON: {exc}"

        try:
            return response_model.model_validate(data), None
        except ValidationError as exc:
            return None, f"schema validation failed: {exc}"

    def _record_repair(
        self,
        module: str,
        status: str,
        original_messages: list[dict[str, str]],
        original_content: str,
        outcome: str,
    ) -> None:
        if self._step_tracer is None:
            return
        self._step_tracer.record_llm_repair_step(
            module=module,
            status="ok" if status == "ok" else "error",
            detail={
                "system_prompt": original_messages[0]["content"],
                "invalid_response": original_content,
                "outcome": outcome,
            },
        )

"""Deterministic, offline fake LLM client for tests and local development.

Makes no network calls. Callers configure exactly which response(s) each
module should receive, so tests are fully deterministic.
"""

from collections import defaultdict

from pydantic import BaseModel

from src.listing_agent.llm.client.base import LLMClient


class FakeLLMClient(LLMClient):
    """Returns pre-configured Pydantic objects instead of calling an LLM."""

    def __init__(self, responses: dict[str, BaseModel] | None = None) -> None:
        self._queues: dict[str, list[BaseModel]] = defaultdict(list)
        if responses:
            for module, response in responses.items():
                self._queues[module].append(response)

    def set_response(self, module: str, response: BaseModel) -> None:
        """Configure the (single, repeatable) response for `module`."""
        self._queues[module] = [response]

    def queue_response(self, module: str, response: BaseModel) -> None:
        """Append another response to `module`'s queue.

        Useful when a test needs a module to be called more than once and
        return a different value each time.
        """
        self._queues[module].append(response)

    async def generate_json(
        self,
        module: str,
        system_prompt: str,
        user_payload: dict,
        response_model: type[BaseModel],
    ) -> BaseModel:
        queue = self._queues.get(module)
        if not queue:
            raise LookupError(
                f"FakeLLMClient has no configured response for module '{module}'. "
                "Call set_response()/queue_response() before invoking generate_json()."
            )

        # Keep the last remaining response so repeated calls stay deterministic
        # instead of failing once the queue is exhausted.
        response = queue.pop(0) if len(queue) > 1 else queue[0]

        if not isinstance(response, response_model):
            raise TypeError(
                f"FakeLLMClient response configured for module '{module}' is a "
                f"{type(response).__name__}, but {response_model.__name__} was requested."
            )

        return response

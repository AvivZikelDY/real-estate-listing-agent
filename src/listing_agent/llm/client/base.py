"""Abstract LLM client interface.

Both LLM modules (`triage_planner`, `report_writer`, added in a later
phase) must call an LLM exclusively through this interface, never directly.
"""

from abc import ABC, abstractmethod

from pydantic import BaseModel


class LLMClient(ABC):
    """Abstract base class for all LLM client implementations."""

    @abstractmethod
    async def generate_json(
        self,
        module: str,
        system_prompt: str,
        user_payload: dict,
        response_model: type[BaseModel],
    ) -> BaseModel:
        """Ask the LLM to produce output conforming to `response_model`.

        Args:
            module: Name of the calling LLM module (e.g. "triage_planner").
            system_prompt: The system-level instructions for this call.
            user_payload: The structured user-turn data for this call.
            response_model: The Pydantic model the response must conform to.

        Returns:
            An instance of `response_model`.
        """
        raise NotImplementedError

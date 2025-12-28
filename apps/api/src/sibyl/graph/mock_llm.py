"""Mock LLM client for testing without API keys.

This module provides a mock LLM client that returns valid but empty responses,
allowing the full Graphiti workflow to run without actual LLM calls.

Usage:
    Set SIBYL_MOCK_LLM=true to enable mock mode in tests/CI.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog
from graphiti_core.llm_client.client import LLMClient
from graphiti_core.llm_client.config import LLMConfig
from pydantic import BaseModel

if TYPE_CHECKING:
    from graphiti_core.llm_client.config import ModelSize
    from graphiti_core.prompts.models import Message

log = structlog.get_logger()


class MockLLMClient(LLMClient):
    """Mock LLM client that returns empty extraction results.

    This client inherits from Graphiti's LLMClient to pass Pydantic validation,
    but returns valid empty responses without making API calls.

    Used for:
    - CI/CD testing without API keys
    - Local development/debugging
    - Integration tests
    """

    def __init__(self) -> None:
        """Initialize mock client with minimal config."""
        # Initialize parent with a dummy config
        config = LLMConfig(api_key="mock-key", model="mock-model")
        super().__init__(config, cache=False)

        # Override parent's model attributes
        self.model = "mock-model"
        self.small_model = "mock-small-model"

    async def _generate_response(
        self,
        messages: list[Message],
        response_model: type[BaseModel] | None = None,
        max_tokens: int = 1000,
        model_size: ModelSize | None = None,
    ) -> dict[str, Any]:
        """Return mock response matching expected schema.

        This is the abstract method from LLMClient that we must implement.
        Returns empty/default responses without making actual LLM API calls.

        Args:
            messages: Chat messages (ignored in mock)
            response_model: Pydantic model for structured output
            max_tokens: Token limit (ignored)
            model_size: Model size preference (ignored)

        Returns:
            Dict matching response_model schema with empty/default values
        """
        model_name = response_model.__name__ if response_model else "unknown"
        log.debug("Mock LLM called", response_model=model_name)

        # Return appropriate empty response based on response model
        if response_model is None:
            return {"content": ""}

        # Handle Graphiti's extraction models by returning empty lists
        # This allows the workflow to complete without extracting entities/edges
        response = self._create_empty_response(response_model)

        log.debug("Mock LLM response", model=model_name, response=response)
        return response

    def _create_empty_response(self, response_model: type[BaseModel]) -> dict[str, Any]:
        """Create an empty/default response for a Pydantic model.

        Inspects model fields and returns appropriate empty values:
        - Lists -> []
        - Strings -> ""
        - Booleans -> False
        - Optional -> None

        Args:
            response_model: Pydantic model class

        Returns:
            Dict with empty/default values for all fields
        """
        result: dict[str, Any] = {}

        for field_name, field_info in response_model.model_fields.items():
            annotation = field_info.annotation

            # Get the origin type (List, Optional, etc.)
            origin = getattr(annotation, "__origin__", None)

            if origin is list:
                result[field_name] = []
            elif annotation is str:
                result[field_name] = ""
            elif annotation is bool:
                result[field_name] = False
            elif annotation is int:
                result[field_name] = 0
            elif annotation is float:
                result[field_name] = 0.0
            else:
                # For Optional types and complex types, use None
                result[field_name] = None

        return result

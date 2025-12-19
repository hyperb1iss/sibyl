"""Custom exceptions for the Conventions MCP Server."""


class ConventionsMCPError(Exception):
    """Base exception for all Conventions MCP errors."""

    def __init__(self, message: str, *, details: dict[str, object] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class GraphConnectionError(ConventionsMCPError):
    """Raised when unable to connect to the graph database."""


class EntityNotFoundError(ConventionsMCPError):
    """Raised when a requested entity is not found in the graph."""

    def __init__(self, entity_type: str, identifier: str) -> None:
        super().__init__(
            f"{entity_type} not found: {identifier}",
            details={"entity_type": entity_type, "identifier": identifier},
        )


class IngestionError(ConventionsMCPError):
    """Raised when content ingestion fails."""


class ValidationError(ConventionsMCPError):
    """Raised when input validation fails."""


class SearchError(ConventionsMCPError):
    """Raised when a search operation fails."""

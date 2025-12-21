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


class EntityCreationError(ConventionsMCPError):
    """Raised when entity creation fails or cannot be verified."""

    def __init__(self, message: str, *, entity_id: str | None = None) -> None:
        super().__init__(message, details={"entity_id": entity_id})


class IngestionError(ConventionsMCPError):
    """Raised when content ingestion fails."""


class ValidationError(ConventionsMCPError):
    """Raised when input validation fails."""


class SearchError(ConventionsMCPError):
    """Raised when a search operation fails."""


class InvalidTransitionError(ConventionsMCPError):
    """Raised when an invalid task status transition is attempted."""

    def __init__(
        self,
        from_status: str,
        to_status: str,
        allowed: list[str] | None = None,
    ) -> None:
        allowed_str = f" Allowed: {allowed}" if allowed else ""
        super().__init__(
            f"Invalid transition: {from_status} -> {to_status}.{allowed_str}",
            details={
                "from_status": from_status,
                "to_status": to_status,
                "allowed_transitions": allowed or [],
            },
        )

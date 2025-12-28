"""Core exceptions for Sibyl operations."""


class SibylError(Exception):
    """Base exception for all Sibyl errors."""

    def __init__(self, message: str, *, details: dict[str, object] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class GraphError(SibylError):
    """Raised when a graph operation fails."""


class GraphConnectionError(GraphError):
    """Raised when unable to connect to the graph database."""


class EntityNotFoundError(SibylError):
    """Raised when a requested entity is not found in the graph."""

    def __init__(self, entity_type: str, identifier: str) -> None:
        super().__init__(
            f"{entity_type} not found: {identifier}",
            details={"entity_type": entity_type, "identifier": identifier},
        )


class EntityCreationError(SibylError):
    """Raised when entity creation fails or cannot be verified."""

    def __init__(self, message: str, *, entity_id: str | None = None) -> None:
        super().__init__(message, details={"entity_id": entity_id})


class ValidationError(SibylError):
    """Raised when input validation fails."""


class SearchError(SibylError):
    """Raised when a search operation fails."""


class IngestionError(SibylError):
    """Raised when content ingestion fails."""


class InvalidTransitionError(SibylError):
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

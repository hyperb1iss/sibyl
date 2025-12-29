"""
sibyl-core: Core library for Sibyl knowledge graph operations.

This package provides:
- Domain models (Entity, Task, Project, etc.)
- Graph client and entity management (FalkorDB/Graphiti)
- Hybrid retrieval (semantic + keyword search)
- Core tools (search, explore, add)
- Task workflow engine
- Auth primitives (JWT, password hashing)
"""

from sibyl_core.config import CoreConfig, core_config
from sibyl_core.errors import (
    ConventionsMCPError,
    EntityCreationError,
    EntityNotFoundError,
    GraphConnectionError,
    GraphError,
    IngestionError,
    InvalidTransitionError,
    SearchError,
    SibylError,
    ValidationError,
)

__version__ = "0.1.0"

__all__ = [
    # Errors
    "ConventionsMCPError",
    # Config
    "CoreConfig",
    "EntityCreationError",
    "EntityNotFoundError",
    "GraphConnectionError",
    "GraphError",
    "IngestionError",
    "InvalidTransitionError",
    "SearchError",
    "SibylError",
    "ValidationError",
    "core_config",
]

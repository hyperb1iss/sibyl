"""Memory API router composition."""

from fastapi import APIRouter

from sibyl.api.routes import (
    memory_promotion,
    memory_raw,
    memory_review,
    memory_sharing,
    memory_sources,
    memory_spaces,
)

router = APIRouter(prefix="/memory", tags=["memory"])
router.routes.extend(memory_spaces.router.routes)
router.routes.extend(memory_raw.router.routes)
router.routes.extend(memory_sources.router.routes)
router.routes.extend(memory_sharing.router.routes)
router.routes.extend(memory_promotion.preview_router.routes)
router.routes.extend(memory_review.router.routes)
router.routes.extend(memory_promotion.mutation_router.routes)

__all__ = ["router"]

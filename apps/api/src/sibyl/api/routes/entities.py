"""Public entity router composed from canonical domain modules."""

from fastapi import APIRouter

from sibyl.api.routes import entity_bulk, entity_captures, entity_mutations, entity_reads

router = APIRouter(prefix="/entities", tags=["entities"])
router.routes.extend(entity_captures.router.routes)
router.routes.extend(entity_reads.router.routes)
router.routes.extend(entity_bulk.router.routes)
router.routes.extend(entity_mutations.router.routes)

__all__ = ["router"]

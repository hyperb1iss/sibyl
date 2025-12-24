"""Tests for RAG organization filtering.

Verifies that all RAG endpoints properly scope queries to the user's organization,
preventing cross-org data access.
"""

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from sibyl.auth.context import AuthContext
from sibyl.db.models import CrawlSource, Organization, OrganizationRole, User


def make_mock_user(user_id=None) -> MagicMock:
    """Create a mock User object."""
    user = MagicMock(spec=User)
    user.id = user_id or uuid4()
    return user


def make_mock_org(org_id=None) -> MagicMock:
    """Create a mock Organization object."""
    org = MagicMock(spec=Organization)
    org.id = org_id or uuid4()
    return org


def make_auth_context(with_org: bool = True, org_id=None) -> AuthContext:
    """Create an AuthContext for testing."""
    user = make_mock_user()
    org = make_mock_org(org_id) if with_org else None
    role = OrganizationRole.MEMBER if with_org else None
    return AuthContext(user=user, organization=org, org_role=role)


class TestRAGOrgFiltering:
    """Tests for organization filtering in RAG endpoints."""

    @pytest.mark.asyncio
    async def test_rag_search_requires_org_context(self) -> None:
        """RAG search should reject requests without organization context."""
        from sibyl.api.routes.rag import rag_search
        from sibyl.api.schemas import RAGSearchRequest

        # Auth context without org
        auth = make_auth_context(with_org=False)
        request = RAGSearchRequest(query="test query")

        # Should raise 403 when no org context
        with pytest.raises(HTTPException) as exc_info:
            await rag_search(request, auth)

        assert exc_info.value.status_code == 403
        assert "Organization context required" in str(exc_info.value.detail)

    @pytest.mark.asyncio
    async def test_code_examples_requires_org_context(self) -> None:
        """Code examples search should reject requests without organization context."""
        from sibyl.api.routes.rag import search_code_examples
        from sibyl.api.schemas import CodeExampleRequest

        auth = make_auth_context(with_org=False)
        request = CodeExampleRequest(query="test query")

        with pytest.raises(HTTPException) as exc_info:
            await search_code_examples(request, auth)

        assert exc_info.value.status_code == 403
        assert "Organization context required" in str(exc_info.value.detail)

    @pytest.mark.asyncio
    async def test_hybrid_search_requires_org_context(self) -> None:
        """Hybrid search should reject requests without organization context."""
        from sibyl.api.routes.rag import hybrid_search
        from sibyl.api.schemas import RAGSearchRequest

        auth = make_auth_context(with_org=False)
        request = RAGSearchRequest(query="test query")

        with pytest.raises(HTTPException) as exc_info:
            await hybrid_search(request, auth)

        assert exc_info.value.status_code == 403
        assert "Organization context required" in str(exc_info.value.detail)


class TestSourcePagesOrgVerification:
    """Tests for source ownership verification in page listing."""

    @pytest.mark.asyncio
    async def test_list_pages_verifies_source_ownership(self) -> None:
        """list_source_pages should verify the source belongs to the user's org."""
        from sibyl.api.routes.rag import list_source_pages

        user_org_id = uuid4()
        other_org_id = uuid4()
        source_id = str(uuid4())  # String ID

        # Create a source owned by a different org
        mock_source = MagicMock(spec=CrawlSource)
        mock_source.organization_id = other_org_id  # Different org!

        # Mock session
        mock_session = AsyncMock(spec=AsyncSession)
        mock_session.get.return_value = mock_source

        # User belongs to user_org_id, but source is in other_org_id
        auth = make_auth_context(with_org=True, org_id=user_org_id)

        # Patch get_session to return our mock
        with patch("sibyl.api.routes.rag.get_session") as mock_get_session:
            mock_get_session.return_value.__aenter__.return_value = mock_session

            with pytest.raises(HTTPException) as exc_info:
                await list_source_pages(source_id=source_id, auth=auth)

            # Should return 404 (not 403) to prevent org enumeration
            assert exc_info.value.status_code == 404
            assert "Source not found" in str(exc_info.value.detail)


class TestDocumentOrgVerification:
    """Tests for document ownership verification."""

    @pytest.mark.asyncio
    async def test_get_full_page_verifies_org_ownership(self) -> None:
        """get_full_page should verify the document's source belongs to user's org."""
        from sibyl.api.routes.rag import get_full_page

        user_org_id = uuid4()
        other_org_id = uuid4()
        document_id = str(uuid4())  # String ID
        source_id = uuid4()

        # Create a document with source owned by different org
        mock_doc = MagicMock()
        mock_doc.source_id = source_id

        mock_source = MagicMock(spec=CrawlSource)
        mock_source.organization_id = other_org_id  # Different org!

        mock_session = AsyncMock(spec=AsyncSession)
        mock_session.get.side_effect = lambda _model, entity_id: (
            mock_doc
            if str(entity_id) == document_id
            else mock_source
            if entity_id == source_id
            else None
        )

        auth = make_auth_context(with_org=True, org_id=user_org_id)

        with patch("sibyl.api.routes.rag.get_session") as mock_get_session:
            mock_get_session.return_value.__aenter__.return_value = mock_session

            with pytest.raises(HTTPException) as exc_info:
                await get_full_page(document_id=document_id, auth=auth)

            assert exc_info.value.status_code == 404
            assert "not found" in str(exc_info.value.detail).lower()

    @pytest.mark.asyncio
    async def test_update_document_verifies_org_ownership(self) -> None:
        """update_document should verify the document's source belongs to user's org."""
        from sibyl.api.routes.rag import update_document
        from sibyl.api.schemas import DocumentUpdateRequest

        user_org_id = uuid4()
        other_org_id = uuid4()
        document_id = str(uuid4())  # String ID
        source_id = uuid4()

        mock_doc = MagicMock()
        mock_doc.source_id = source_id

        mock_source = MagicMock(spec=CrawlSource)
        mock_source.organization_id = other_org_id

        mock_session = AsyncMock(spec=AsyncSession)
        mock_session.get.side_effect = lambda _model, entity_id: (
            mock_doc
            if str(entity_id) == document_id
            else mock_source
            if entity_id == source_id
            else None
        )

        auth = make_auth_context(with_org=True, org_id=user_org_id)

        # Use title (required field for update)
        request = DocumentUpdateRequest(title="Updated title")

        with patch("sibyl.api.routes.rag.get_session") as mock_get_session:
            mock_get_session.return_value.__aenter__.return_value = mock_session

            with pytest.raises(HTTPException) as exc_info:
                await update_document(document_id=document_id, request=request, auth=auth)

            assert exc_info.value.status_code == 404
            assert "Document not found" in str(exc_info.value.detail)

    @pytest.mark.asyncio
    async def test_get_related_entities_verifies_org_ownership(self) -> None:
        """get_document_related_entities should verify org ownership."""
        from sibyl.api.routes.rag import get_document_related_entities

        user_org_id = uuid4()
        other_org_id = uuid4()
        document_id = str(uuid4())  # String ID
        source_id = uuid4()

        mock_doc = MagicMock()
        mock_doc.source_id = source_id

        mock_source = MagicMock(spec=CrawlSource)
        mock_source.organization_id = other_org_id

        mock_session = AsyncMock(spec=AsyncSession)
        mock_session.get.side_effect = lambda _model, entity_id: (
            mock_doc
            if str(entity_id) == document_id
            else mock_source
            if entity_id == source_id
            else None
        )

        auth = make_auth_context(with_org=True, org_id=user_org_id)

        with patch("sibyl.api.routes.rag.get_session") as mock_get_session:
            mock_get_session.return_value.__aenter__.return_value = mock_session

            with pytest.raises(HTTPException) as exc_info:
                await get_document_related_entities(document_id=document_id, auth=auth)

            assert exc_info.value.status_code == 404
            assert "Document not found" in str(exc_info.value.detail)


class TestCrawlSourceModel:
    """Tests for CrawlSource model organization field."""

    def test_crawl_source_has_organization_id_field(self) -> None:
        """CrawlSource should have organization_id field."""
        from sibyl.db.models import CrawlSource

        # Check that organization_id is in the model fields
        field_info = CrawlSource.model_fields.get("organization_id")
        assert field_info is not None, "organization_id field should exist"

    def test_crawl_source_organization_id_is_uuid(self) -> None:
        """CrawlSource.organization_id should be a UUID type."""
        from uuid import UUID

        from sibyl.db.models import CrawlSource

        field_info = CrawlSource.model_fields.get("organization_id")
        assert field_info is not None
        # The annotation should include UUID
        assert field_info.annotation is UUID or "UUID" in str(field_info.annotation)


class TestAuthContextProperties:
    """Tests for AuthContext convenience properties."""

    def test_organization_id_property_returns_string(self) -> None:
        """AuthContext.organization_id should return org ID as string."""
        org_id = uuid4()
        auth = make_auth_context(with_org=True, org_id=org_id)

        assert auth.organization_id == str(org_id)

    def test_organization_id_property_returns_none_when_no_org(self) -> None:
        """AuthContext.organization_id should return None when no org."""
        auth = make_auth_context(with_org=False)

        assert auth.organization_id is None

    def test_user_id_property_returns_string(self) -> None:
        """AuthContext.user_id should return user ID as string."""
        auth = make_auth_context(with_org=True)

        assert auth.user_id is not None
        assert isinstance(auth.user_id, str)

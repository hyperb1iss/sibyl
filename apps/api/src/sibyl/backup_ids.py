from datetime import UTC, datetime
from uuid import uuid4


def generate_backup_id(organization_id: str | None = None) -> str:
    """Generate a unique backup ID."""
    org_fragment = (organization_id or "global").replace("-", "")[:8]
    timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    nonce = uuid4().hex[:10]
    return f"backup_{org_fragment}_{timestamp}_{nonce}"

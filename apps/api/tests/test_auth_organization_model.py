from sqlalchemy.dialects.postgresql import JSONB

from sibyl.db.models import Organization


def test_organization_table_shape() -> None:
    table = Organization.__table__

    assert set(table.columns.keys()) == {
        "id",
        "name",
        "slug",
        "is_personal",
        "settings",
        "created_at",
        "updated_at",
    }

    assert table.columns["slug"].unique is True
    assert table.columns["slug"].index is True

    assert isinstance(table.columns["settings"].type, JSONB)

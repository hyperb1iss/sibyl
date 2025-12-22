from sqlalchemy import Enum, Index

from sibyl.db.models import OrganizationMember, OrganizationRole


def test_org_membership_table_shape() -> None:
    table = OrganizationMember.__table__

    assert set(table.columns.keys()) == {
        "id",
        "organization_id",
        "user_id",
        "role",
        "created_at",
        "updated_at",
    }

    assert isinstance(table.columns["role"].type, Enum)
    assert set(table.columns["role"].type.enums) == {
        OrganizationRole.OWNER.value,
        OrganizationRole.ADMIN.value,
        OrganizationRole.MEMBER.value,
        OrganizationRole.VIEWER.value,
    }

    indexes = {idx.name: idx for idx in table.indexes}
    assert "ix_organization_members_org_user_unique" in indexes
    assert isinstance(indexes["ix_organization_members_org_user_unique"], Index)
    assert indexes["ix_organization_members_org_user_unique"].unique is True


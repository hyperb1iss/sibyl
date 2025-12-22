from sibyl.db.models import User


def test_user_table_shape() -> None:
    table = User.__table__

    assert set(table.columns.keys()) == {
        "id",
        "github_id",
        "email",
        "name",
        "avatar_url",
        "password_salt",
        "password_hash",
        "password_iterations",
        "created_at",
        "updated_at",
    }

    assert table.columns["github_id"].unique is True
    assert table.columns["github_id"].nullable is True
    assert table.columns["email"].unique is True

    index_cols = {tuple(idx.columns.keys()) for idx in table.indexes}
    assert ("github_id",) in index_cols
    assert ("email",) in index_cols

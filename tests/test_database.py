
import sqlite3

import pytest

from src.storage.database import Database


@pytest.fixture
def db(tmp_path):
    """Create a fresh temporary database for each test."""
    return Database(tmp_path / "test.db")


def test_database_initialization(db):
    assert db.db_path.exists()

    with db.connection() as conn:
        rows = conn.execute("""
            SELECT name
            FROM sqlite_master
            WHERE type = 'table'
        """).fetchall()

    table_names = {row["name"] for row in rows}

    expected = {
        "files",
        "classifications",
        "folders",
        "operations",
        "settings"
    }

    assert expected.issubset(table_names)


def test_add_and_get_file(db, tmp_path):
    path = tmp_path / "lecture.pdf"

    file_id = db.add_file(
        file_path=path,
        size_bytes=2048
    )

    record = db.get_file(file_id)

    assert record is not None
    assert record["filename"] == "lecture.pdf"
    assert record["size_bytes"] == 2048
    assert record["status"] == "detected"


def test_pending_review_persists(db, tmp_path):
    path = tmp_path / "notes.pdf"

    file_id = db.add_file(path, 1024)

    updated = db.update_file_status(
        file_id,
        "pending_review"
    )

    assert updated is True

    reopened = Database(db.db_path)
    record = reopened.get_file(file_id)

    assert record["status"] == "pending_review"


def test_classification(db, tmp_path):
    file_id = db.add_file(
        tmp_path / "notes.pdf",
        1000
    )

    db.add_classification(
        file_id=file_id,
        category="Education",
        subject="DBMS",
        topics=["Normalization", "SQL"],
        confidence=0.9,
        proposed_path=str(tmp_path / "DBMS"),
        model_name="test-model"
    )

    result = db.get_latest_classification(file_id)

    assert result["category"] == "Education"
    assert result["subject"] == "DBMS"
    assert result["topics"] == [
        "Normalization",
        "SQL"
    ]


def test_folder_registration_does_not_create_folder(
    db, tmp_path
):
    folder = tmp_path / "DBMS"

    folder_id = db.add_folder(
        folder,
        description="Database notes"
    )

    assert folder_id > 0
    assert not folder.exists()


def test_operation_is_recorded_without_moving_file(
    db, tmp_path
):
    source = tmp_path / "source.pdf"
    destination = tmp_path / "organized.pdf"

    source.write_text("Test content")

    file_id = db.add_file(
        source,
        source.stat().st_size
    )

    operation_id = db.add_operation(
        file_id=file_id,
        operation_type="move",
        source_path=source,
        destination_path=destination
    )

    operations = db.get_operations_for_file(file_id)

    assert operations[0]["id"] == operation_id
    assert operations[0]["status"] == "pending"

    assert source.exists()
    assert not destination.exists()


def test_settings_persist(db):
    db.set_setting(
        "monitoring_enabled",
        True
    )

    reopened = Database(db.db_path)

    assert reopened.get_setting(
        "monitoring_enabled"
    ) is True


def test_invalid_file_size_is_rejected(db, tmp_path):
    with pytest.raises(sqlite3.IntegrityError):
        db.add_file(
            tmp_path / "invalid.pdf",
            -10
        )


def test_invalid_status_is_rejected(db, tmp_path):
    file_id = db.add_file(
        tmp_path / "test.pdf",
        100
    )

    with pytest.raises(sqlite3.IntegrityError):
        db.update_file_status(
            file_id,
            "unknown_status"
        )

    assert db.get_file(file_id)["status"] == "detected"


def test_foreign_key_is_enforced(db):
    with pytest.raises(sqlite3.IntegrityError):
        db.add_classification(
            file_id=999999,
            category="Education"
        )

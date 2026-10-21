
import sqlite3

import pytest

from src.storage.database import Database


@pytest.fixture
def db(tmp_path):
    """Create an isolated temporary database for each test."""
    return Database(tmp_path / "test_smartdownload.db")


def create_review_operation(db, tmp_path):
    """Create a file and pending move operation ready for review."""
    source = tmp_path / "notes.pdf"
    destination = tmp_path / "Documents" / "notes.pdf"

    file_id = db.add_file(source, 100)
    db.update_file_status(file_id, "processing")
    db.update_file_status(file_id, "pending_review")

    operation_id = db.add_operation(
        file_id=file_id,
        operation_type="move",
        source_path=source,
        destination_path=destination,
    )

    return file_id, operation_id, source, destination


def test_database_initialization(db):
    with db.connection() as conn:
        rows = conn.execute("""
            SELECT name
            FROM sqlite_master
            WHERE type = 'table'
        """).fetchall()

    table_names = {row["name"] for row in rows}

    assert {
        "files",
        "folders",
        "classifications",
        "operations",
        "settings",
    }.issubset(table_names)


def test_add_and_get_file(db, tmp_path):
    path = tmp_path / "notes.pdf"

    file_id = db.add_file(path, 1024)
    record = db.get_file(file_id)

    assert record is not None
    assert record["filename"] == "notes.pdf"
    assert record["extension"] == ".pdf"
    assert record["size_bytes"] == 1024
    assert record["status"] == "detected"
    assert record["original_path"] == str(path)
    assert record["current_path"] == str(path)


def test_pending_review_persists(db, tmp_path):
    file_id = db.add_file(tmp_path / "notes.pdf", 1024)

    db.update_file_status(file_id, "processing")
    db.update_file_status(file_id, "pending_review")

    assert db.get_file(file_id)["status"] == "pending_review"


def test_classification(db, tmp_path):
    file_id = db.add_file(tmp_path / "report.pdf", 2048)

    classification_id = db.add_classification(
        file_id=file_id,
        category="Education",
        subject="Computer Science",
        topics=["DBMS", "SQL"],
        summary="Database study notes",
        confidence=0.95,
        model_name="test-model",
    )

    result = db.get_latest_classification(file_id)

    assert classification_id is not None
    assert result["category"] == "Education"
    assert result["subject"] == "Computer Science"
    assert result["topics"] == ["DBMS", "SQL"]
    assert result["summary"] == "Database study notes"
    assert result["confidence"] == 0.95
    assert result["model_name"] == "test-model"


def test_folder_registration_does_not_create_folder(db, tmp_path):
    folder_path = tmp_path / "Documents" / "Study"

    folder_id = db.add_folder(
        folder_path,
        description="Study material"
    )

    assert folder_id is not None
    assert not folder_path.exists()

    with db.connection() as conn:
        row = conn.execute(
            "SELECT * FROM folders WHERE id = ?",
            (folder_id,)
        ).fetchone()

    assert row["path"] == str(folder_path)
    assert row["display_name"] == "Study"
    assert row["description"] == "Study material"


def test_operation_is_recorded_without_moving_file(db, tmp_path):
    source = tmp_path / "source.pdf"
    destination = tmp_path / "destination" / "source.pdf"

    source.write_text("test content", encoding="utf-8")
    file_id = db.add_file(source, source.stat().st_size)

    operation_id = db.add_operation(
        file_id=file_id,
        operation_type="move",
        source_path=source,
        destination_path=destination,
    )

    operations = db.get_operations_for_file(file_id)

    assert operation_id is not None
    assert len(operations) == 1
    assert operations[0]["status"] == "pending"
    assert source.exists()
    assert not destination.exists()


def test_settings_persist(db):
    db.set_setting(
        "monitored_extensions",
        [".pdf", ".docx", ".txt"]
    )

    assert db.get_setting("monitored_extensions") == [
        ".pdf",
        ".docx",
        ".txt",
    ]

    assert db.get_setting("missing_setting", "default") == "default"


def test_invalid_file_size_is_rejected(db, tmp_path):
    with pytest.raises(sqlite3.IntegrityError):
        db.add_file(tmp_path / "invalid.pdf", -1)


def test_invalid_status_is_rejected(db, tmp_path):
    file_id = db.add_file(tmp_path / "test.pdf", 100)

    with pytest.raises(ValueError, match="Unknown file status"):
        db.update_file_status(file_id, "unknown_status")


def test_foreign_key_is_enforced(db):
    with pytest.raises(sqlite3.IntegrityError):
        db.add_classification(
            file_id=999999,
            category="Education"
        )


def test_cannot_skip_processing(db, tmp_path):
    file_id = db.add_file(tmp_path / "skip.pdf", 100)

    with pytest.raises(ValueError, match="Invalid status transition"):
        db.update_file_status(file_id, "pending_review")

    assert db.get_file(file_id)["status"] == "detected"


def test_cannot_organize_without_approval(db, tmp_path):
    file_id = db.add_file(tmp_path / "review.pdf", 100)

    db.update_file_status(file_id, "processing")
    db.update_file_status(file_id, "pending_review")

    with pytest.raises(ValueError, match="Invalid status transition"):
        db.update_file_status(file_id, "organized")

    assert db.get_file(file_id)["status"] == "pending_review"


def test_approved_file_can_be_marked_organized(db, tmp_path):
    file_id = db.add_file(tmp_path / "approved.pdf", 100)

    db.update_file_status(file_id, "processing")
    db.update_file_status(file_id, "pending_review")
    db.update_file_status(file_id, "approved")
    db.update_file_status(file_id, "organized")

    assert db.get_file(file_id)["status"] == "organized"


def test_nonexistent_file_returns_false(db):
    assert db.update_file_status(999999, "processing") is False


def test_explicit_approval_records_timestamp(db, tmp_path):
    file_id, operation_id, source, destination = (
        create_review_operation(db, tmp_path)
    )

    assert db.approve_operation(
        operation_id,
        source,
        destination
    )

    record = db.get_file(file_id)
    operation = db.get_operations_for_file(file_id)[0]

    assert record["status"] == "approved"
    assert operation["approved_at"] is not None
    assert db.is_operation_approved(
        operation_id,
        source,
        destination
    )


def test_approval_rejects_wrong_destination(db, tmp_path):
    _, operation_id, source, destination = (
        create_review_operation(db, tmp_path)
    )

    with pytest.raises(ValueError, match="destination"):
        db.approve_operation(
            operation_id,
            source,
            destination.parent / "different.pdf"
        )


def test_approval_rejects_wrong_source(db, tmp_path):
    _, operation_id, source, destination = (
        create_review_operation(db, tmp_path)
    )

    with pytest.raises(ValueError, match="source"):
        db.approve_operation(
            operation_id,
            source.parent / "other.pdf",
            destination
        )


def test_unapproved_operation_fails_verification(db, tmp_path):
    _, operation_id, source, destination = (
        create_review_operation(db, tmp_path)
    )

    assert not db.is_operation_approved(
        operation_id,
        source,
        destination
    )


def test_approval_requires_pending_review(db, tmp_path):
    source = tmp_path / "notes.pdf"
    destination = tmp_path / "Documents" / "notes.pdf"

    file_id = db.add_file(source, 100)
    operation_id = db.add_operation(
        file_id,
        "move",
        source,
        destination
    )

    with pytest.raises(ValueError, match="pending review"):
        db.approve_operation(
            operation_id,
            source,
            destination
        )


def test_approval_does_not_move_file(db, tmp_path):
    file_id, operation_id, source, destination = (
        create_review_operation(db, tmp_path)
    )

    source.write_text("test content", encoding="utf-8")

    assert db.approve_operation(
        operation_id,
        source,
        destination
    )

    assert source.exists()
    assert not destination.exists()
    assert db.get_file(file_id)["status"] == "approved"

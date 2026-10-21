
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


def utc_now():
    """Return the current UTC time as an ISO string."""
    return datetime.now(timezone.utc).isoformat()


def get_default_database_path():
    """Return the database path in Windows local AppData."""
    local_app_data = os.getenv("LOCALAPPDATA")

    if not local_app_data:
        raise RuntimeError(
            "LOCALAPPDATA is not available. "
            "Provide an explicit database path."
        )

    return (
        Path(local_app_data)
        / "SmartDownloadAI"
        / "smartdownload.db"
    )


class Database:
    """Manage SmartDownload AI's SQLite database."""

    ALLOWED_TRANSITIONS = {
        "detected": {
            "processing",
            "unsupported",
            "cancelled",
        },
        "processing": {
            "pending_review",
            "error",
            "unsupported",
            "cancelled",
        },
        "pending_review": {
            "approved",
            "rejected",
            "error",
            "cancelled",
        },
        "approved": {
            "organized",
            "error",
            "cancelled",
        },
        "organized": {"processing"},
        "rejected": {"processing"},
        "error": {"processing", "cancelled"},
        "unsupported": {"processing", "cancelled"},
        "cancelled": {"processing"},
    }

    VALID_STATUSES = {
        "detected",
        "processing",
        "pending_review",
        "approved",
        "organized",
        "rejected",
        "error",
        "unsupported",
        "cancelled",
    }

    def __init__(self, db_path=None):
        self.db_path = Path(
            db_path
            if db_path is not None
            else get_default_database_path()
        ).expanduser()

        self.db_path.parent.mkdir(
            parents=True,
            exist_ok=True
        )

        self.initialize_database()

    @contextmanager
    def connection(self):
        """Open a connection with transaction handling."""
        conn = sqlite3.connect(
            str(self.db_path),
            timeout=10
        )

        conn.row_factory = sqlite3.Row

        try:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA busy_timeout = 10000")

            yield conn
            conn.commit()

        except Exception:
            conn.rollback()
            raise

        finally:
            conn.close()

    def initialize_database(self):
        """Create the required tables if absent."""
        with self.connection() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS files (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    original_path TEXT NOT NULL,
                    current_path TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    extension TEXT,
                    size_bytes INTEGER NOT NULL
                        CHECK(size_bytes >= 0),
                    content_hash TEXT,
                    status TEXT NOT NULL DEFAULT 'detected'
                        CHECK(status IN (
                            'detected',
                            'processing',
                            'pending_review',
                            'approved',
                            'organized',
                            'rejected',
                            'error',
                            'unsupported',
                            'cancelled'
                        )),
                    detected_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_error TEXT
                )
            """)

            conn.execute("""
                CREATE TABLE IF NOT EXISTS folders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    path TEXT NOT NULL UNIQUE,
                    display_name TEXT NOT NULL,
                    description TEXT,
                    parent_folder_id INTEGER,
                    last_scanned_at TEXT,
                    is_active INTEGER NOT NULL DEFAULT 1
                        CHECK(is_active IN (0, 1)),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (parent_folder_id)
                        REFERENCES folders(id)
                )
            """)

            conn.execute("""
                CREATE TABLE IF NOT EXISTS classifications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    file_id INTEGER NOT NULL,
                    model_name TEXT,
                    category TEXT,
                    subject TEXT,
                    topics TEXT,
                    summary TEXT,
                    confidence REAL
                        CHECK(
                            confidence IS NULL OR
                            confidence BETWEEN 0 AND 1
                        ),
                    proposed_folder_id INTEGER,
                    proposed_path TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (file_id)
                        REFERENCES files(id),
                    FOREIGN KEY (proposed_folder_id)
                        REFERENCES folders(id)
                )
            """)

            conn.execute("""
                CREATE TABLE IF NOT EXISTS operations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    file_id INTEGER NOT NULL,
                    operation_type TEXT NOT NULL
                        CHECK(operation_type IN (
                            'move',
                            'undo_move'
                        )),
                    source_path TEXT NOT NULL,
                    destination_path TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending'
                        CHECK(status IN (
                            'pending',
                            'in_progress',
                            'completed',
                            'failed',
                            'cancelled'
                        )),
                    approved_at TEXT,
                    started_at TEXT,
                    completed_at TEXT,
                    error_message TEXT,
                    undo_of_operation_id INTEGER,
                    FOREIGN KEY (file_id)
                        REFERENCES files(id),
                    FOREIGN KEY (undo_of_operation_id)
                        REFERENCES operations(id)
                )
            """)

            conn.execute("""
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)

            conn.execute("""
                CREATE INDEX IF NOT EXISTS
                    idx_files_status
                ON files(status)
            """)

            conn.execute("""
                CREATE INDEX IF NOT EXISTS
                    idx_classifications_file
                ON classifications(file_id)
            """)

            conn.execute("""
                CREATE INDEX IF NOT EXISTS
                    idx_operations_file
                ON operations(file_id)
            """)

            conn.execute("""
                CREATE INDEX IF NOT EXISTS
                    idx_operations_status
                ON operations(status)
            """)

            conn.execute("""
                CREATE INDEX IF NOT EXISTS
                    idx_folders_parent
                ON folders(parent_folder_id)
            """)

            conn.execute("PRAGMA user_version = 1")

    def add_file(self, file_path, size_bytes, content_hash=None):
        """Register a file without modifying it."""
        path = Path(file_path).expanduser()
        now = utc_now()

        with self.connection() as conn:
            cursor = conn.execute("""
                INSERT INTO files (
                    original_path, current_path, filename,
                    extension, size_bytes, content_hash,
                    status, detected_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                str(path), str(path), path.name,
                path.suffix.lower(), size_bytes, content_hash,
                "detected", now, now
            ))

            return cursor.lastrowid

    def get_file(self, file_id):
        """Retrieve one tracked file."""
        with self.connection() as conn:
            row = conn.execute(
                "SELECT * FROM files WHERE id = ?",
                (file_id,)
            ).fetchone()

            return dict(row) if row else None

    def get_files_by_status(self, status):
        """Retrieve files matching a workflow status."""
        with self.connection() as conn:
            rows = conn.execute("""
                SELECT *
                FROM files
                WHERE status = ?
                ORDER BY detected_at DESC
            """, (status,)).fetchall()

            return [dict(row) for row in rows]

    def update_file_status(self, file_id, status, error=None):
        """Update a file status only when the transition is allowed."""
        if status not in self.VALID_STATUSES:
            raise ValueError(f"Unknown file status: {status}")

        with self.connection() as conn:
            row = conn.execute(
                "SELECT status FROM files WHERE id = ?",
                (file_id,)
            ).fetchone()

            if row is None:
                return False

            current_status = row["status"]

            if status == current_status:
                return True

            if status not in self.ALLOWED_TRANSITIONS.get(
                current_status, set()
            ):
                raise ValueError(
                    f"Invalid status transition: "
                    f"{current_status} -> {status}"
                )

            conn.execute("""
                UPDATE files
                SET status = ?, last_error = ?, updated_at = ?
                WHERE id = ?
            """, (status, error, utc_now(), file_id))

            return True

    def add_classification(
        self,
        file_id,
        category,
        subject=None,
        topics=None,
        summary=None,
        confidence=None,
        proposed_folder_id=None,
        proposed_path=None,
        model_name=None
    ):
        """Save an AI classification result."""
        with self.connection() as conn:
            cursor = conn.execute("""
                INSERT INTO classifications (
                    file_id, model_name, category, subject,
                    topics, summary, confidence,
                    proposed_folder_id, proposed_path, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                file_id,
                model_name,
                category,
                subject,
                json.dumps(topics or []),
                summary,
                confidence,
                proposed_folder_id,
                proposed_path,
                utc_now()
            ))

            return cursor.lastrowid

    def get_latest_classification(self, file_id):
        """Retrieve the most recent classification."""
        with self.connection() as conn:
            row = conn.execute("""
                SELECT *
                FROM classifications
                WHERE file_id = ?
                ORDER BY id DESC
                LIMIT 1
            """, (file_id,)).fetchone()

            if row is None:
                return None

            result = dict(row)
            result["topics"] = json.loads(result["topics"] or "[]")
            return result

    def add_folder(
        self,
        folder_path,
        description=None,
        parent_folder_id=None
    ):
        """Register folder metadata without creating a real folder."""
        path = Path(folder_path).expanduser()
        now = utc_now()

        with self.connection() as conn:
            cursor = conn.execute("""
                INSERT INTO folders (
                    path, display_name, description,
                    parent_folder_id, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                str(path), path.name, description,
                parent_folder_id, now, now
            ))

            return cursor.lastrowid

    def add_operation(
        self,
        file_id,
        operation_type,
        source_path,
        destination_path,
        approved_at=None,
        undo_of_operation_id=None
    ):
        """Record a planned operation; never execute it."""
        with self.connection() as conn:
            cursor = conn.execute("""
                INSERT INTO operations (
                    file_id, operation_type, source_path,
                    destination_path, status, approved_at,
                    undo_of_operation_id
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                file_id,
                operation_type,
                str(source_path),
                str(destination_path),
                "pending",
                approved_at,
                undo_of_operation_id
            ))

            return cursor.lastrowid

    def approve_operation(
        self,
        operation_id,
        approved_source,
        approved_destination
    ):
        """
        Record explicit user approval for one exact move.

        This records approval only; it does not move a file.
        """
        with self.connection() as conn:
            operation = conn.execute("""
                SELECT o.*, f.status AS file_status,
                       f.current_path AS current_file_path
                FROM operations AS o
                JOIN files AS f ON f.id = o.file_id
                WHERE o.id = ?
            """, (operation_id,)).fetchone()

            if operation is None:
                raise ValueError("Operation does not exist.")

            if operation["operation_type"] != "move":
                raise ValueError(
                    "Only move operations can be approved here."
                )

            if operation["status"] != "pending":
                raise ValueError(
                    "Only pending operations can be approved."
                )

            if operation["file_status"] != "pending_review":
                raise ValueError(
                    "The file must be pending review before approval."
                )

            source = str(Path(approved_source).expanduser())
            destination = str(
                Path(approved_destination).expanduser()
            )

            if source != operation["source_path"]:
                raise ValueError(
                    "Approved source does not match the operation."
                )

            if destination != operation["destination_path"]:
                raise ValueError(
                    "Approved destination does not match the operation."
                )

            if source != operation["current_file_path"]:
                raise ValueError(
                    "Operation source does not match the file's current path."
                )

            if source == destination:
                raise ValueError(
                    "Source and destination must be different."
                )

            now = utc_now()

            conn.execute("""
                UPDATE operations
                SET approved_at = ?
                WHERE id = ?
            """, (now, operation_id))

            conn.execute("""
                UPDATE files
                SET status = 'approved', updated_at = ?
                WHERE id = ?
            """, (now, operation["file_id"]))

            return True

    def is_operation_approved(
        self,
        operation_id,
        intended_source,
        intended_destination
    ):
        """
        Verify approval for the exact recorded move.

        This check does not perform a file operation.
        """
        source = str(Path(intended_source).expanduser())
        destination = str(
            Path(intended_destination).expanduser()
        )

        with self.connection() as conn:
            row = conn.execute("""
                SELECT o.approved_at,
                       o.operation_type,
                       o.status,
                       o.source_path,
                       o.destination_path,
                       f.status AS file_status,
                       f.current_path AS current_file_path
                FROM operations AS o
                JOIN files AS f ON f.id = o.file_id
                WHERE o.id = ?
            """, (operation_id,)).fetchone()

            if row is None:
                return False

            return (
                row["operation_type"] == "move"
                and row["approved_at"] is not None
                and row["status"] == "pending"
                and row["file_status"] == "approved"
                and row["source_path"] == source
                and row["destination_path"] == destination
                and row["current_file_path"] == source
                and source != destination
            )

    def get_operations_for_file(self, file_id):
        """Retrieve the operation history of one file."""
        with self.connection() as conn:
            rows = conn.execute("""
                SELECT *
                FROM operations
                WHERE file_id = ?
                ORDER BY id DESC
            """, (file_id,)).fetchall()

            return [dict(row) for row in rows]

    def set_setting(self, key, value):
        """Create or update a JSON-serializable setting."""
        serialized = json.dumps(value)

        with self.connection() as conn:
            conn.execute("""
                INSERT INTO settings (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key)
                DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
            """, (key, serialized, utc_now()))

    def get_setting(self, key, default=None):
        """Retrieve a setting and decode its JSON value."""
        with self.connection() as conn:
            row = conn.execute("""
                SELECT value
                FROM settings
                WHERE key = ?
            """, (key,)).fetchone()

            if row is None:
                return default

            return json.loads(row["value"])

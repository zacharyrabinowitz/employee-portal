"""Apply additive schema changes to an existing portal.db without touching data.

Safe to run any number of times. Never drops or recreates tables.
"""

import mimetypes
import os
import sqlite3

DB_PATH = "portal.db"
TRAINING_SLIDES_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "uploads", "training_slides")


def column_exists(conn, table, column):
    cols = [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]
    return column in cols


def table_exists(conn, table):
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone()
    return row is not None


def migrate():
    conn = sqlite3.connect(DB_PATH)

    if not column_exists(conn, "employee_uploads", "onboarding_step_id"):
        conn.execute(
            "ALTER TABLE employee_uploads ADD COLUMN onboarding_step_id INTEGER "
            "REFERENCES onboarding_steps(id)"
        )
        print("Added employee_uploads.onboarding_step_id")

    if not table_exists(conn, "training_slides"):
        conn.execute(
            """CREATE TABLE training_slides (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                module_id INTEGER NOT NULL REFERENCES training_modules(id),
                image_path TEXT NOT NULL,
                caption TEXT,
                sort_order INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )"""
        )
        print("Created training_slides table")

    if not column_exists(conn, "training_slides", "background_color"):
        conn.execute(
            "ALTER TABLE training_slides ADD COLUMN background_color TEXT NOT NULL DEFAULT '#ffffff'"
        )
        print("Added training_slides.background_color")

    if not column_exists(conn, "training_modules", "is_onboarding"):
        conn.execute(
            "ALTER TABLE training_modules ADD COLUMN is_onboarding INTEGER NOT NULL DEFAULT 0"
        )
        print("Added training_modules.is_onboarding")

    if not table_exists(conn, "slide_elements"):
        conn.execute(
            """CREATE TABLE slide_elements (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                slide_id INTEGER NOT NULL REFERENCES training_slides(id),
                element_type TEXT NOT NULL,
                content TEXT,
                pos_x REAL NOT NULL DEFAULT 10,
                pos_y REAL NOT NULL DEFAULT 10,
                width REAL NOT NULL DEFAULT 30,
                height REAL NOT NULL DEFAULT 20,
                z_index INTEGER NOT NULL DEFAULT 1,
                font_size INTEGER NOT NULL DEFAULT 18,
                color TEXT NOT NULL DEFAULT '#1f2430',
                bold INTEGER NOT NULL DEFAULT 0,
                align TEXT NOT NULL DEFAULT 'left',
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )"""
        )
        print("Created slide_elements table")

    if not table_exists(conn, "onboarding_templates"):
        conn.execute(
            """CREATE TABLE onboarding_templates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                description TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )"""
        )
        print("Created onboarding_templates table")

    if not table_exists(conn, "onboarding_template_items"):
        conn.execute(
            """CREATE TABLE onboarding_template_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                template_id INTEGER NOT NULL REFERENCES onboarding_templates(id),
                step_name TEXT NOT NULL,
                step_type TEXT NOT NULL,
                related_id INTEGER,
                sort_order INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )"""
        )
        print("Created onboarding_template_items table")

    if not column_exists(conn, "employees", "onboarding_template_id"):
        conn.execute(
            "ALTER TABLE employees ADD COLUMN onboarding_template_id INTEGER "
            "REFERENCES onboarding_templates(id)"
        )
        print("Added employees.onboarding_template_id")

    if not column_exists(conn, "employees", "date_of_birth"):
        conn.execute("ALTER TABLE employees ADD COLUMN date_of_birth TEXT")
        print("Added employees.date_of_birth")

    if not table_exists(conn, "portal_settings"):
        conn.execute(
            """CREATE TABLE portal_settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )"""
        )
        print("Created portal_settings table")

    if not column_exists(conn, "employees", "username"):
        conn.execute("ALTER TABLE employees ADD COLUMN username TEXT")
        print("Added employees.username")

    backfill_usernames(conn)

    if not column_exists(conn, "notes", "updated_at"):
        conn.execute("ALTER TABLE notes ADD COLUMN updated_at TEXT")
        print("Added notes.updated_at")

    if not table_exists(conn, "role_permissions"):
        conn.execute(
            """CREATE TABLE role_permissions (
                role TEXT NOT NULL,
                permission TEXT NOT NULL,
                PRIMARY KEY (role, permission)
            )"""
        )
        print("Created role_permissions table")
        # Grant Manager every permission by default, so existing Manager accounts
        # keep exactly the access they already had before this feature existed.
        for key in (
            "manage_employees",
            "manage_documents",
            "manage_training",
            "manage_onboarding_checklists",
            "manage_settings",
        ):
            conn.execute(
                "INSERT INTO role_permissions (role, permission) VALUES ('Manager', ?)", (key,)
            )
        print("Granted Manager all permissions by default (preserves existing access)")

    if not column_exists(conn, "training_slides", "media_data"):
        conn.execute("ALTER TABLE training_slides ADD COLUMN media_data BLOB")
        conn.execute("ALTER TABLE training_slides ADD COLUMN media_mimetype TEXT")
        conn.execute("ALTER TABLE training_slides ADD COLUMN media_kind TEXT")
        print("Added training_slides media columns (media_data, media_mimetype, media_kind)")

    if not column_exists(conn, "slide_elements", "media_data"):
        conn.execute("ALTER TABLE slide_elements ADD COLUMN media_data BLOB")
        conn.execute("ALTER TABLE slide_elements ADD COLUMN media_mimetype TEXT")
        print("Added slide_elements media columns (media_data, media_mimetype)")

    backfill_slide_media_from_disk(conn)

    if not column_exists(conn, "documents", "requires_upload"):
        conn.execute("ALTER TABLE documents ADD COLUMN requires_upload INTEGER NOT NULL DEFAULT 0")
        print("Added documents.requires_upload")

    conn.commit()
    conn.close()
    print("Migration complete. No existing data was touched.")


def backfill_slide_media_from_disk(conn):
    """Images uploaded before BLOB storage existed are just a filename on disk,
    which doesn't survive on hosts with an ephemeral filesystem. Where the file
    still happens to exist locally, copy its bytes into the new BLOB columns so
    it keeps working regardless of where the app is deployed."""
    slides = conn.execute(
        "SELECT id, image_path FROM training_slides WHERE media_data IS NULL AND image_path != ''"
    ).fetchall()
    for slide_id, image_path in slides:
        full_path = os.path.join(TRAINING_SLIDES_FOLDER, image_path)
        if not os.path.isfile(full_path):
            continue
        mimetype = mimetypes.guess_type(image_path)[0] or "application/octet-stream"
        kind = "video" if mimetype.startswith("video/") else "image"
        with open(full_path, "rb") as f:
            data = f.read()
        conn.execute(
            "UPDATE training_slides SET media_data = ?, media_mimetype = ?, media_kind = ? WHERE id = ?",
            (data, mimetype, kind, slide_id),
        )
        print(f"  Backfilled slide #{slide_id} media from disk ({image_path}, {len(data)} bytes)")

    elements = conn.execute(
        """SELECT id, content FROM slide_elements
           WHERE element_type = 'image' AND media_data IS NULL AND content IS NOT NULL"""
    ).fetchall()
    for element_id, content in elements:
        full_path = os.path.join(TRAINING_SLIDES_FOLDER, content)
        if not os.path.isfile(full_path):
            continue
        mimetype = mimetypes.guess_type(content)[0] or "application/octet-stream"
        with open(full_path, "rb") as f:
            data = f.read()
        conn.execute(
            "UPDATE slide_elements SET media_data = ?, media_mimetype = ? WHERE id = ?",
            (data, mimetype, element_id),
        )
        print(f"  Backfilled slide element #{element_id} media from disk ({content}, {len(data)} bytes)")


def backfill_usernames(conn):
    """Give every employee without a username one, derived from their name
    (first + last name, lowercase, no spaces). Never touches password_hash —
    existing accounts keep logging in with whatever password they already have."""
    rows = conn.execute(
        "SELECT id, name FROM employees WHERE username IS NULL OR username = '' ORDER BY id"
    ).fetchall()
    for emp_id, name in rows:
        parts = (name or "").strip().split()
        first = parts[0] if parts else ""
        last = parts[-1] if len(parts) > 1 else ""
        base = (first + last).lower() or f"employee{emp_id}"
        username = base
        suffix = 2
        while conn.execute(
            "SELECT id FROM employees WHERE username = ? AND id != ?", (username, emp_id)
        ).fetchone():
            username = f"{base}{suffix}"
            suffix += 1
        conn.execute("UPDATE employees SET username = ? WHERE id = ?", (username, emp_id))
        print(f"  Set username for employee #{emp_id} ({name}): {username}")


if __name__ == "__main__":
    migrate()

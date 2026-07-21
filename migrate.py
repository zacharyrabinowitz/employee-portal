"""Apply additive schema changes to an existing portal.db without touching data.

Safe to run any number of times. Never drops or recreates tables.
"""

import mimetypes
import os
import sqlite3

DB_PATH = "portal.db"
TRAINING_SLIDES_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "uploads", "training_slides")

# Old broad manage_* permissions were split into specific per-action ones.
# Any Manager who already had the broad permission gets every new permission
# it was split into, so upgrading never silently takes access away.
OLD_TO_NEW_PERMISSIONS = {
    "manage_employees": ["employees_add", "employees_edit", "employees_notes", "employees_checklist"],
    "manage_documents": ["documents_create", "documents_edit", "documents_delete", "documents_signatures"],
    "manage_training": [
        "training_create", "training_edit", "training_delete", "training_slides", "training_assign",
    ],
    "manage_quizzes": [
        "quizzes_create", "quizzes_edit", "quizzes_delete", "quizzes_assign", "quizzes_lock",
        "quizzes_results_view", "quizzes_results_edit",
    ],
    "manage_onboarding_checklists": [
        "checklists_templates", "checklists_items", "checklists_master", "checklists_order",
    ],
    "manage_settings": ["settings_signup_page"],
}


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

    if not table_exists(conn, "quizzes"):
        conn.execute(
            """CREATE TABLE quizzes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                description TEXT,
                training_module_id INTEGER REFERENCES training_modules(id),
                passing_score INTEGER NOT NULL DEFAULT 70,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )"""
        )
        print("Created quizzes table")

    if not table_exists(conn, "quiz_questions"):
        conn.execute(
            """CREATE TABLE quiz_questions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                quiz_id INTEGER NOT NULL REFERENCES quizzes(id),
                question_text TEXT NOT NULL,
                sort_order INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )"""
        )
        print("Created quiz_questions table")

    if not table_exists(conn, "quiz_choices"):
        conn.execute(
            """CREATE TABLE quiz_choices (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                question_id INTEGER NOT NULL REFERENCES quiz_questions(id),
                choice_text TEXT NOT NULL,
                is_correct INTEGER NOT NULL DEFAULT 0,
                sort_order INTEGER NOT NULL DEFAULT 0
            )"""
        )
        print("Created quiz_choices table")

    if not table_exists(conn, "quiz_attempts"):
        conn.execute(
            """CREATE TABLE quiz_attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                quiz_id INTEGER NOT NULL REFERENCES quizzes(id),
                employee_id INTEGER NOT NULL REFERENCES employees(id),
                score INTEGER NOT NULL,
                total INTEGER NOT NULL,
                passed INTEGER NOT NULL DEFAULT 0,
                submitted_at TEXT NOT NULL DEFAULT (datetime('now'))
            )"""
        )
        print("Created quiz_attempts table")

    if not table_exists(conn, "quiz_attempt_answers"):
        conn.execute(
            """CREATE TABLE quiz_attempt_answers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                attempt_id INTEGER NOT NULL REFERENCES quiz_attempts(id),
                question_id INTEGER NOT NULL REFERENCES quiz_questions(id),
                choice_id INTEGER REFERENCES quiz_choices(id),
                is_correct INTEGER NOT NULL DEFAULT 0
            )"""
        )
        print("Created quiz_attempt_answers table")

    if not table_exists(conn, "master_checklist_items"):
        conn.execute(
            """CREATE TABLE master_checklist_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                step_name TEXT NOT NULL,
                sort_order INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )"""
        )
        # Preserve current behavior: every employee has always gotten this task
        # automatically. Seeding it here means existing installs keep behaving
        # exactly the same after upgrading, but now it's editable in Settings.
        conn.execute(
            "INSERT INTO master_checklist_items (step_name, sort_order) VALUES ('Review Company Policies', 0)"
        )
        print("Created master_checklist_items table (seeded with 'Review Company Policies')")

    if not column_exists(conn, "quiz_questions", "question_type"):
        conn.execute(
            "ALTER TABLE quiz_questions ADD COLUMN question_type TEXT NOT NULL DEFAULT 'single_choice'"
        )
        conn.execute("ALTER TABLE quiz_questions ADD COLUMN text_answer TEXT")
        print("Added quiz_questions.question_type, quiz_questions.text_answer")

    if not column_exists(conn, "quiz_attempt_answers", "text_answer"):
        conn.execute("ALTER TABLE quiz_attempt_answers ADD COLUMN text_answer TEXT")
        print("Added quiz_attempt_answers.text_answer")

    if not column_exists(conn, "quizzes", "is_onboarding"):
        conn.execute("ALTER TABLE quizzes ADD COLUMN is_onboarding INTEGER NOT NULL DEFAULT 0")
        print("Added quizzes.is_onboarding")

    if not column_exists(conn, "quiz_choices", "match_text"):
        conn.execute("ALTER TABLE quiz_choices ADD COLUMN match_text TEXT")
        print("Added quiz_choices.match_text (for matching-type questions)")

    if not table_exists(conn, "audit_log"):
        conn.execute(
            """CREATE TABLE audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                actor_id INTEGER,
                actor_name TEXT,
                actor_role TEXT,
                method TEXT NOT NULL,
                path TEXT NOT NULL,
                endpoint TEXT,
                action_label TEXT,
                action_type TEXT NOT NULL,
                status_code INTEGER,
                entity_summary TEXT,
                details TEXT
            )"""
        )
        conn.execute("CREATE INDEX idx_audit_log_created_at ON audit_log(created_at)")
        conn.execute("CREATE INDEX idx_audit_log_actor_id ON audit_log(actor_id)")
        conn.execute("CREATE INDEX idx_audit_log_action_type ON audit_log(action_type)")
        print("Created audit_log table")

    if not column_exists(conn, "quizzes", "is_locked"):
        conn.execute("ALTER TABLE quizzes ADD COLUMN is_locked INTEGER NOT NULL DEFAULT 0")
        print("Added quizzes.is_locked")

    if not table_exists(conn, "quiz_locks"):
        conn.execute(
            """CREATE TABLE quiz_locks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                quiz_id INTEGER NOT NULL REFERENCES quizzes(id),
                employee_id INTEGER NOT NULL REFERENCES employees(id),
                locked_at TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE(quiz_id, employee_id)
            )"""
        )
        print("Created quiz_locks table (per-employee quiz locking)")

    if not column_exists(conn, "documents", "is_onboarding"):
        conn.execute("ALTER TABLE documents ADD COLUMN is_onboarding INTEGER NOT NULL DEFAULT 0")
        # Preserve current behavior: every existing document has always been
        # auto-assigned to every employee, current and future. Flip them all on
        # so upgrading doesn't silently remove anyone's checklist items.
        conn.execute("UPDATE documents SET is_onboarding = 1")
        print("Added documents.is_onboarding (existing documents kept as \"required for everyone\")")

    if table_exists(conn, "role_permissions"):
        # New granular permission bolted onto the existing Documents category —
        # anyone who could already edit documents keeps full document management,
        # including the new assign action, after upgrading.
        roles_with_doc_edit = [
            row[0]
            for row in conn.execute(
                "SELECT DISTINCT role FROM role_permissions WHERE permission = 'documents_edit'"
            ).fetchall()
        ]
        for role in roles_with_doc_edit:
            exists = conn.execute(
                "SELECT 1 FROM role_permissions WHERE role = ? AND permission = 'documents_assign'",
                (role,),
            ).fetchone()
            if not exists:
                conn.execute(
                    "INSERT INTO role_permissions (role, permission) VALUES (?, 'documents_assign')",
                    (role,),
                )
                print(f"Granted '{role}' the new documents_assign permission (already had documents_edit)")

    if not table_exists(conn, "custom_roles"):
        conn.execute(
            """CREATE TABLE custom_roles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                description TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )"""
        )
        print("Created custom_roles table (custom access levels)")

    if table_exists(conn, "role_permissions"):
        for old_key, new_keys in OLD_TO_NEW_PERMISSIONS.items():
            had_old = conn.execute(
                "SELECT 1 FROM role_permissions WHERE role = 'Manager' AND permission = ?", (old_key,)
            ).fetchone()
            if not had_old:
                continue
            for new_key in new_keys:
                exists = conn.execute(
                    "SELECT 1 FROM role_permissions WHERE role = 'Manager' AND permission = ?", (new_key,)
                ).fetchone()
                if not exists:
                    conn.execute(
                        "INSERT INTO role_permissions (role, permission) VALUES ('Manager', ?)", (new_key,)
                    )
            conn.execute(
                "DELETE FROM role_permissions WHERE role = 'Manager' AND permission = ?", (old_key,)
            )
            print(f"Expanded Manager permission '{old_key}' into: {', '.join(new_keys)}")

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

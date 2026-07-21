import csv
import io
import json
import mimetypes
import os
import random
import re
import secrets
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import zipfile
from datetime import datetime, timedelta

import migrate as db_migrate

from flask import (
    Flask,
    g,
    redirect,
    render_template,
    request,
    session,
    url_for,
    flash,
    send_file,
    send_from_directory,
    Response,
)

DB_PATH = "portal.db"
UPLOAD_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "uploads", "documents")
EMPLOYEE_UPLOAD_FOLDER = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "uploads", "employee_uploads"
)
TRAINING_SLIDES_FOLDER = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "uploads", "training_slides"
)
ALLOWED_EXTENSIONS = {"pdf", "doc", "docx", "txt", "png", "jpg", "jpeg"}
ALLOWED_IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}
ALLOWED_VIDEO_EXTENSIONS = {"mp4", "webm", "ogg", "mov"}
ALLOWED_MEDIA_EXTENSIONS = ALLOWED_IMAGE_EXTENSIONS | ALLOWED_VIDEO_EXTENSIONS
MAX_MEDIA_BYTES = 50 * 1024 * 1024  # 50 MB per image/video

UPLOADS_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "uploads")
BACKUP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backups")
BACKUP_FILENAME_RE = re.compile(r"^employee-portal-backup-(\d{8})-(\d{6})\.zip$")

app = Flask(__name__)
app.secret_key = "dev-secret-key-change-me"
app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024  # 200 MB (backup imports can be large)

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(EMPLOYEE_UPLOAD_FOLDER, exist_ok=True)
os.makedirs(TRAINING_SLIDES_FOLDER, exist_ok=True)

ADMIN_ROLES = ("Admin", "Manager")


def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def allowed_media(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_MEDIA_EXTENSIONS


def media_kind_for(filename):
    ext = filename.rsplit(".", 1)[1].lower()
    return "video" if ext in ALLOWED_VIDEO_EXTENSIONS else "image"


def guess_mimetype(filename, fallback=None):
    return mimetypes.guess_type(filename)[0] or fallback or "application/octet-stream"


def save_slides(db, module_id, files, captions=None):
    """Save uploaded image/video files as slides for a training module, storing the
    bytes directly in the database (not on disk) so they survive on hosts with an
    ephemeral filesystem. `captions`, if given, is matched to `files` by position.
    Returns count added."""
    captions = captions or []
    max_order = db.execute(
        "SELECT COALESCE(MAX(sort_order), -1) FROM training_slides WHERE module_id = ?",
        (module_id,),
    ).fetchone()[0]
    added = 0
    for i, f in enumerate(files):
        if not f or not f.filename:
            continue
        if not allowed_media(f.filename):
            continue
        data = f.read()
        if not data or len(data) > MAX_MEDIA_BYTES:
            continue
        max_order += 1
        mimetype = f.mimetype or guess_mimetype(f.filename)
        kind = media_kind_for(f.filename)
        caption = captions[i].strip() if i < len(captions) and captions[i].strip() else None
        db.execute(
            """INSERT INTO training_slides
               (module_id, image_path, caption, sort_order, media_data, media_mimetype, media_kind)
               VALUES (?, '', ?, ?, ?, ?, ?)""",
            (module_id, caption, max_order, data, mimetype, kind),
        )
        added += 1
    return added


HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")

DEFAULT_ONBOARDING_HEADING = "Welcome, {name}"
DEFAULT_ONBOARDING_MESSAGE = (
    "Click below to activate your account. Your username and password will be shown "
    "to you afterward — you won't need to choose either."
)
DEFAULT_ONBOARDING_BUTTON = "Activate My Account"


def get_setting(db, key, default=""):
    row = db.execute("SELECT value FROM portal_settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row and row["value"] is not None else default


def set_setting(db, key, value):
    db.execute(
        """INSERT INTO portal_settings (key, value) VALUES (?, ?)
           ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
        (key, value),
    )


def generate_username(db, name, exclude_employee_id=None):
    """first + last name, lowercase, no spaces. Appends 2, 3, ... on collision."""
    parts = (name or "").strip().split()
    first = parts[0] if parts else ""
    last = parts[-1] if len(parts) > 1 else ""
    base = (first + last).lower() or "employee"

    username = base
    suffix = 2
    while True:
        query = "SELECT id FROM employees WHERE username = ?"
        params = [username]
        if exclude_employee_id is not None:
            query += " AND id != ?"
            params.append(exclude_employee_id)
        if not db.execute(query, params).fetchone():
            return username
        username = f"{base}{suffix}"
        suffix += 1


def get_module_slides(db, module_id):
    """Slides for a module, each with its canvas elements attached (as dicts)."""
    slides = db.execute(
        "SELECT * FROM training_slides WHERE module_id = ? ORDER BY sort_order, id",
        (module_id,),
    ).fetchall()
    result = []
    for slide in slides:
        elements = db.execute(
            "SELECT * FROM slide_elements WHERE slide_id = ? ORDER BY z_index, id",
            (slide["id"],),
        ).fetchall()
        row = dict(slide)
        row["elements"] = elements
        result.append(row)
    return result


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


# ---------------------------------------------------------------------------
# Roles — Admin/Manager/Employee are built in; admins can also create custom
# "access levels" (stored in custom_roles) that behave like Manager: they see
# the admin-side portal and get whatever's granted in role_permissions.
# ---------------------------------------------------------------------------

def get_custom_role_names(db):
    return [row["name"] for row in db.execute("SELECT name FROM custom_roles ORDER BY name").fetchall()]


def get_admin_side_roles(db):
    """Roles that see the admin-side portal (nav, dashboard, etc). Employee
    stays on the standard employee-side experience by default, but if it's
    been granted any admin-side permission at all, it gains access to the
    admin portal too (scoped to exactly what it was granted)."""
    roles = ADMIN_ROLES + tuple(get_custom_role_names(db))
    if db.execute("SELECT 1 FROM role_permissions WHERE role = 'Employee' LIMIT 1").fetchone():
        roles = roles + ("Employee",)
    return roles


def get_all_roles(db):
    """Every role an employee can be assigned."""
    return ("Admin", "Manager", "Employee") + tuple(get_custom_role_names(db))


# ---------------------------------------------------------------------------
# Audit log — automatically records every request (page visits and changes
# alike) with no per-route instrumentation needed. Route URL parameters like
# document_id/employee_id/quiz_id are resolved to a human-readable label
# *before* the view runs, so deletions still show what was deleted.
# ---------------------------------------------------------------------------

ENTITY_LOOKUPS = {
    "employee_id": ("employees", "name"),
    "document_id": ("documents", "title"),
    "quiz_id": ("quizzes", "title"),
    "question_id": ("quiz_questions", "question_text"),
    "attempt_id": ("quiz_attempts", None),
    "template_id": ("onboarding_templates", "name"),
    "module_id": ("training_modules", "title"),
    "item_id": ("onboarding_template_items", "step_name"),
    "step_id": ("onboarding_steps", "step_name"),
    "upload_id": ("employee_uploads", "label"),
    "signature_id": ("signatures", "signature_text"),
    "note_id": ("notes", "body"),
    "slide_id": ("training_slides", "caption"),
    "element_id": ("slide_elements", "element_type"),
}

AUDIT_SKIP_PREFIXES = ("/static/",)


@app.before_request
def audit_capture_entities():
    """Resolve any recognized ID URL parameters to a human label before the
    view runs, so that if the view deletes the row, we still logged what it
    was."""
    entities = []
    view_args = request.view_args or {}
    if view_args:
        db = get_db()
        for key, value in view_args.items():
            lookup = ENTITY_LOOKUPS.get(key)
            if not lookup:
                continue
            table, label_col = lookup
            label = None
            if label_col:
                try:
                    row = db.execute(
                        f"SELECT {label_col} FROM {table} WHERE id = ?", (value,)
                    ).fetchone()
                except sqlite3.Error:
                    row = None
                if row is not None:
                    label = row[label_col]
            entities.append({"table": table, "id": value, "label": label})
    g.audit_entities = entities


@app.after_request
def audit_log_request(response):
    """Best-effort request logging — must never break the actual request, so
    any failure here is swallowed rather than surfaced."""
    try:
        if request.path.startswith(AUDIT_SKIP_PREFIXES):
            return response

        db = get_db()
        endpoint = request.endpoint or ""
        action_label = endpoint.replace("_", " ").title() if endpoint else request.path
        action_type = "view" if request.method == "GET" else "change"

        entities = getattr(g, "audit_entities", [])
        summary_parts = []
        for e in entities:
            if e["label"] and str(e["label"]) != str(e["id"]):
                summary_parts.append(f"{e['table']} #{e['id']} ({e['label']})")
            else:
                summary_parts.append(f"{e['table']} #{e['id']}")
        entity_summary = "; ".join(summary_parts) or None

        form_snapshot = {}
        if request.method != "GET" and request.form:
            for key in request.form:
                if "password" in key.lower():
                    continue
                values = request.form.getlist(key)
                values = [v[:300] + "..." if len(v) > 300 else v for v in values]
                form_snapshot[key] = values if len(values) > 1 else values[0]

        details = json.dumps(
            {
                "view_args": {k: v for k, v in (request.view_args or {}).items()},
                "form": form_snapshot,
                "entities": entities,
                "query_string": request.query_string.decode("utf-8", "ignore") or None,
            }
        )

        db.execute(
            """INSERT INTO audit_log
               (actor_id, actor_name, actor_role, method, path, endpoint, action_label,
                action_type, status_code, entity_summary, details)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                session.get("user_id"),
                session.get("name"),
                session.get("role"),
                request.method,
                request.path,
                endpoint,
                action_label,
                action_type,
                response.status_code,
                entity_summary,
                details,
            ),
        )
        db.commit()
    except Exception:
        pass
    return response


# ---------------------------------------------------------------------------
# Auth / session helpers (manual, no auth library or decorators)
# ---------------------------------------------------------------------------

def require_login():
    """Return a redirect response if not logged in (or the session refers to an
    employee that no longer exists), otherwise None."""
    if "user_id" not in session:
        return redirect(url_for("login"))
    db = get_db()
    exists = db.execute(
        "SELECT 1 FROM employees WHERE id = ?", (session["user_id"],)
    ).fetchone()
    if exists is None:
        session.clear()
        flash("Your session has expired. Please log in again.", "error")
        return redirect(url_for("login"))
    return None


def require_admin_or_manager():
    """Return a redirect response if not logged in as an admin-side role
    (Admin, Manager, or a custom access level), otherwise None."""
    resp = require_login()
    if resp:
        return resp
    if session.get("role") not in get_admin_side_roles(get_db()):
        return redirect(url_for("employee_dashboard"))
    return None


def require_admin():
    """Return a redirect response if not logged in as Admin, otherwise None."""
    resp = require_login()
    if resp:
        return resp
    if session.get("role") != "Admin":
        return redirect(url_for("admin_dashboard"))
    return None


# ---------------------------------------------------------------------------
# Role permissions (configurable access for the Manager role)
# ---------------------------------------------------------------------------

PERMISSION_CATEGORIES = [
    ("Employees", [
        ("employees_add", "Add new employees"),
        ("employees_edit", "Edit employee profile details"),
        ("employees_status", "Activate and deactivate employee accounts"),
        ("employees_password", "Change an employee's password"),
        ("employees_notes", "Add, edit, and delete notes on an employee"),
        ("employees_checklist", "Edit an employee's individual checklist (remove steps, request uploads)"),
    ]),
    ("Documents", [
        ("documents_create", "Create new policy documents"),
        ("documents_edit", "Edit existing documents"),
        ("documents_delete", "Delete documents"),
        ("documents_signatures", "Remove individual employee signatures"),
        ("documents_assign", "Assign documents and toggle \"required for everyone\""),
    ]),
    ("Training", [
        ("training_create", "Create training modules"),
        ("training_edit", "Edit training module details"),
        ("training_delete", "Delete training modules"),
        ("training_slides", "Manage slides and the slide editor"),
        ("training_assign", "Assign training and toggle \"required for everyone\""),
    ]),
    ("Quizzes", [
        ("quizzes_create", "Create quizzes"),
        ("quizzes_edit", "Edit quiz details and questions"),
        ("quizzes_delete", "Delete quizzes and questions"),
        ("quizzes_assign", "Assign quizzes and toggle \"required for everyone\""),
        ("quizzes_lock", "Lock and unlock quizzes per employee"),
        ("quizzes_results_view", "View quiz results and attempt detail"),
        ("quizzes_results_edit", "Edit or delete quiz attempts (override scores)"),
    ]),
    ("Onboarding Checklists", [
        ("checklists_templates", "Create, edit, and delete job-specific checklists"),
        ("checklists_items", "Add, edit, and remove items on a checklist"),
        ("checklists_master", "Manage the master checklist"),
        ("checklists_order", "Set checklist priority order"),
    ]),
    ("Settings", [
        ("settings_signup_page", "Customize the signup page"),
    ]),
    ("Reports", [
        ("reports_view", "View and export reports"),
    ]),
]
PERMISSIONS = [item for _, items in PERMISSION_CATEGORIES for item in items]
PERMISSION_KEYS = {key for key, _ in PERMISSIONS}


def has_permission(db, role, permission):
    """Admin always has every permission. Every other role — Manager,
    Employee, or a custom access level — gets exactly whatever's been
    explicitly granted to that role name in role_permissions. Employee
    starts with nothing granted (today's default behavior), but is no
    longer a hardcoded special case — it can be given specific admin-side
    capabilities the same way any other role can."""
    if role == "Admin":
        return True
    row = db.execute(
        "SELECT 1 FROM role_permissions WHERE role = ? AND permission = ?", (role, permission)
    ).fetchone()
    return row is not None


def require_permission(permission):
    """Return a redirect if not logged in, not Admin/Manager, or (for Manager)
    lacking this specific permission. Otherwise None."""
    resp = require_admin_or_manager()
    if resp:
        return resp
    db = get_db()
    if not has_permission(db, session.get("role"), permission):
        flash("You don't have permission to do that. Contact an administrator.", "error")
        return redirect(url_for("admin_dashboard"))
    return None


@app.context_processor
def inject_permission_helper():
    def can(permission):
        if "user_id" not in session:
            return False
        return has_permission(get_db(), session.get("role"), permission)

    def is_admin_side_role(role):
        return role in get_admin_side_roles(get_db())

    return {"can": can, "is_admin_side_role": is_admin_side_role}


def current_employee(db):
    return db.execute(
        "SELECT * FROM employees WHERE id = ?", (session["user_id"],)
    ).fetchone()


# ---------------------------------------------------------------------------
# Progress helpers
# ---------------------------------------------------------------------------

DEFAULT_STEP_TYPE_PRIORITY = ["task", "document", "upload", "training", "quiz"]
STEP_TYPE_LABELS = {
    "task": "Tasks",
    "document": "Documents to Sign",
    "upload": "Documents to Upload",
    "training": "Training Modules",
    "quiz": "Quizzes",
}


def get_step_type_priority(db):
    """Admin-configurable order (Settings → Checklist Order) controlling both
    which incomplete step is surfaced as 'do this next' on the dashboard and
    the order steps appear in on the checklist page."""
    row = db.execute(
        "SELECT value FROM portal_settings WHERE key = 'step_type_priority'"
    ).fetchone()
    if row and row["value"]:
        try:
            order = json.loads(row["value"])
        except (ValueError, TypeError):
            order = None
        if isinstance(order, list) and order:
            for step_type in DEFAULT_STEP_TYPE_PRIORITY:
                if step_type not in order:
                    order.append(step_type)
            return order
    return list(DEFAULT_STEP_TYPE_PRIORITY)


def resolve_checklist_display_name(db, step_type, related_id, fallback_name):
    """Compute a checklist item's display name live from its source
    (document/training module/quiz) instead of trusting a name that was
    copied in at creation time — so renaming the source is reflected
    everywhere instantly instead of leaving a stale copy behind. Falls back
    to the stored name for tasks (which have no live source) or if the
    source row is somehow already gone."""
    if related_id:
        if step_type == "document":
            row = db.execute("SELECT title FROM documents WHERE id = ?", (related_id,)).fetchone()
            if row:
                return f"Sign {row['title']}"
        elif step_type == "upload":
            row = db.execute("SELECT title FROM documents WHERE id = ?", (related_id,)).fetchone()
            if row:
                return f"Upload {row['title']}"
        elif step_type == "training":
            row = db.execute(
                "SELECT title FROM training_modules WHERE id = ?", (related_id,)
            ).fetchone()
            if row:
                return f"Complete {row['title']}"
        elif step_type == "quiz":
            row = db.execute("SELECT title FROM quizzes WHERE id = ?", (related_id,)).fetchone()
            if row:
                return f"Take Quiz: {row['title']}"
    return fallback_name


def onboarding_progress(db, employee_id):
    raw_steps = db.execute(
        "SELECT * FROM onboarding_steps WHERE employee_id = ? ORDER BY id", (employee_id,)
    ).fetchall()
    priority = get_step_type_priority(db)

    steps = []
    for step in raw_steps:
        step_dict = dict(step)
        step_dict["step_name"] = resolve_checklist_display_name(
            db, step["step_type"], step["related_id"], step["step_name"]
        )
        steps.append(step_dict)

    def sort_key(step):
        try:
            rank = priority.index(step["step_type"])
        except ValueError:
            rank = len(priority)
        return (1 if step["completed_at"] else 0, rank, step["id"])

    steps = sorted(steps, key=sort_key)
    total = len(steps)
    completed = len([s for s in steps if s["completed_at"]])
    pct = round((completed / total) * 100) if total else 0
    return steps, completed, total, pct


def training_progress(db, employee_id):
    assignments = db.execute(
        """SELECT training_assignments.*, training_modules.title, training_modules.description,
                  training_modules.content
           FROM training_assignments
           JOIN training_modules ON training_modules.id = training_assignments.module_id
           WHERE training_assignments.employee_id = ?
           ORDER BY training_assignments.id""",
        (employee_id,),
    ).fetchall()
    total = len(assignments)
    completed = len([a for a in assignments if a["completed_at"]])
    pct = round((completed / total) * 100) if total else 0
    return assignments, completed, total, pct


def get_at_a_glance_alerts(db):
    """Employees stuck mid-onboarding past the configured threshold, and employees
    whose most recent attempt on a quiz was a fail (haven't passed it since)."""
    try:
        threshold_days = int(get_setting(db, "stuck_onboarding_days", "7"))
    except ValueError:
        threshold_days = 7

    stuck_onboarding = []
    active_employees = db.execute(
        "SELECT * FROM employees WHERE status = 'Active' ORDER BY created_at"
    ).fetchall()
    for emp in active_employees:
        try:
            started = datetime.strptime(emp["created_at"][:19], "%Y-%m-%d %H:%M:%S")
        except (ValueError, TypeError):
            continue
        days_in = (datetime.utcnow() - started).days
        if days_in < threshold_days:
            continue
        _, _, _, pct = onboarding_progress(db, emp["id"])
        if pct < 100:
            stuck_onboarding.append({"employee": emp, "days": days_in, "pct": pct})

    failed_quizzes = db.execute(
        """SELECT qa.id, qa.employee_id, e.name AS employee_name, qa.quiz_id,
                  q.title AS quiz_title, qa.submitted_at, qa.score, qa.total
           FROM quiz_attempts qa
           JOIN employees e ON e.id = qa.employee_id
           JOIN quizzes q ON q.id = qa.quiz_id
           WHERE qa.passed = 0
             AND e.status != 'Inactive'
             AND qa.id = (
                 SELECT MAX(qa2.id) FROM quiz_attempts qa2
                 WHERE qa2.employee_id = qa.employee_id AND qa2.quiz_id = qa.quiz_id
             )
           ORDER BY qa.submitted_at DESC"""
    ).fetchall()

    return stuck_onboarding, failed_quizzes, threshold_days


def assign_module_to_employee(db, module_id, module_title, employee_id):
    """Assign a training module to an employee (training_assignments + a matching
    onboarding checklist step), unless they're already assigned. Returns True if newly assigned."""
    already = db.execute(
        "SELECT id FROM training_assignments WHERE employee_id = ? AND module_id = ?",
        (employee_id, module_id),
    ).fetchone()
    if already:
        return False
    db.execute(
        "INSERT INTO training_assignments (employee_id, module_id) VALUES (?, ?)",
        (employee_id, module_id),
    )
    db.execute(
        """INSERT INTO onboarding_steps (employee_id, step_name, step_type, related_id)
           VALUES (?, ?, 'training', ?)""",
        (employee_id, f"Complete {module_title}", module_id),
    )
    return True


def assign_document_to_employee(db, document, employee_id):
    """Assign a document (sign or upload) to an employee via an onboarding checklist
    step, unless they're already assigned. Returns True if newly assigned."""
    step_type = "document" if document["requires_signature"] else "upload"
    already = db.execute(
        "SELECT id FROM onboarding_steps WHERE employee_id = ? AND step_type = ? AND related_id = ?",
        (employee_id, step_type, document["id"]),
    ).fetchone()
    if already:
        return False
    verb = "Sign" if document["requires_signature"] else "Upload"
    db.execute(
        """INSERT INTO onboarding_steps (employee_id, step_name, step_type, related_id)
           VALUES (?, ?, ?, ?)""",
        (employee_id, f"{verb} {document['title']}", step_type, document["id"]),
    )
    return True


def apply_onboarding_template_items(db, employee_id, template_id):
    """Add any of an onboarding checklist template's items the employee doesn't already
    have on their checklist. Safe to call repeatedly (e.g. every time the template
    selection is saved) — never inserts a duplicate step."""
    if not template_id:
        return

    template_items = db.execute(
        "SELECT * FROM onboarding_template_items WHERE template_id = ? ORDER BY sort_order, id",
        (template_id,),
    ).fetchall()
    for item in template_items:
        if item["step_type"] == "training" and item["related_id"]:
            mod = db.execute(
                "SELECT title FROM training_modules WHERE id = ?", (item["related_id"],)
            ).fetchone()
            if mod:
                assign_module_to_employee(db, item["related_id"], mod["title"], employee_id)
            continue

        if item["step_type"] == "document" and item["related_id"]:
            already = db.execute(
                """SELECT 1 FROM onboarding_steps
                   WHERE employee_id = ? AND step_type = 'document' AND related_id = ?""",
                (employee_id, item["related_id"]),
            ).fetchone()
            if already:
                continue

        if item["step_type"] == "upload" and item["related_id"]:
            already = db.execute(
                """SELECT 1 FROM onboarding_steps
                   WHERE employee_id = ? AND step_type = 'upload' AND related_id = ?""",
                (employee_id, item["related_id"]),
            ).fetchone()
            if already:
                continue

        if item["step_type"] == "quiz" and item["related_id"]:
            already = db.execute(
                """SELECT 1 FROM onboarding_steps
                   WHERE employee_id = ? AND step_type = 'quiz' AND related_id = ?""",
                (employee_id, item["related_id"]),
            ).fetchone()
            if already:
                continue

        if item["step_type"] == "task":
            already = db.execute(
                """SELECT 1 FROM onboarding_steps
                   WHERE employee_id = ? AND step_type = 'task' AND step_name = ?""",
                (employee_id, item["step_name"]),
            ).fetchone()
            if already:
                continue

        db.execute(
            """INSERT INTO onboarding_steps (employee_id, step_name, step_type, related_id)
               VALUES (?, ?, ?, ?)""",
            (employee_id, item["step_name"], item["step_type"], item["related_id"]),
        )


def seed_onboarding_steps(db, employee_id):
    """Give a newly created employee a baseline onboarding checklist:
    every master checklist item, one step per existing signature-required
    document, every upload-required document, and every onboarding-required
    training module."""
    master_items = db.execute(
        "SELECT step_name FROM master_checklist_items ORDER BY sort_order, id"
    ).fetchall()
    for item in master_items:
        db.execute(
            """INSERT INTO onboarding_steps (employee_id, step_name, step_type, related_id)
               VALUES (?, ?, 'task', NULL)""",
            (employee_id, item["step_name"]),
        )

    docs = db.execute(
        "SELECT id, title FROM documents WHERE requires_signature = 1 AND is_onboarding = 1"
    ).fetchall()
    for doc in docs:
        db.execute(
            """INSERT INTO onboarding_steps (employee_id, step_name, step_type, related_id)
               VALUES (?, ?, 'document', ?)""",
            (employee_id, f"Sign {doc['title']}", doc["id"]),
        )

    upload_docs = db.execute(
        "SELECT id, title FROM documents WHERE requires_upload = 1 AND is_onboarding = 1"
    ).fetchall()
    for doc in upload_docs:
        db.execute(
            """INSERT INTO onboarding_steps (employee_id, step_name, step_type, related_id)
               VALUES (?, ?, 'upload', ?)""",
            (employee_id, f"Upload {doc['title']}", doc["id"]),
        )

    modules = db.execute(
        "SELECT id, title FROM training_modules WHERE is_onboarding = 1"
    ).fetchall()
    for mod in modules:
        assign_module_to_employee(db, mod["id"], mod["title"], employee_id)

    onboarding_quizzes = db.execute(
        "SELECT id, title FROM quizzes WHERE is_onboarding = 1"
    ).fetchall()
    for quiz in onboarding_quizzes:
        db.execute(
            """INSERT INTO onboarding_steps (employee_id, step_name, step_type, related_id)
               VALUES (?, ?, 'quiz', ?)""",
            (employee_id, f"Take Quiz: {quiz['title']}", quiz["id"]),
        )

    employee = db.execute(
        "SELECT onboarding_template_id FROM employees WHERE id = ?", (employee_id,)
    ).fetchone()
    if employee:
        apply_onboarding_template_items(db, employee_id, employee["onboarding_template_id"])


def mark_document_step_complete(db, employee_id, document_id):
    db.execute(
        """UPDATE onboarding_steps SET completed_at = ?
           WHERE employee_id = ? AND step_type = 'document' AND related_id = ? AND completed_at IS NULL""",
        (datetime.utcnow().isoformat(timespec="seconds"), employee_id, document_id),
    )


def mark_training_step_complete(db, employee_id, module_id):
    db.execute(
        """UPDATE onboarding_steps SET completed_at = ?
           WHERE employee_id = ? AND step_type = 'training' AND related_id = ? AND completed_at IS NULL""",
        (datetime.utcnow().isoformat(timespec="seconds"), employee_id, module_id),
    )


def mark_quiz_step_complete(db, employee_id, quiz_id):
    """Only a passing attempt satisfies a quiz checklist step — a failed
    attempt leaves it pending so the employee has to retake it."""
    db.execute(
        """UPDATE onboarding_steps SET completed_at = ?
           WHERE employee_id = ? AND step_type = 'quiz' AND related_id = ? AND completed_at IS NULL""",
        (datetime.utcnow().isoformat(timespec="seconds"), employee_id, quiz_id),
    )


def is_quiz_locked_for(db, quiz_id, employee_id):
    return (
        db.execute(
            "SELECT 1 FROM quiz_locks WHERE quiz_id = ? AND employee_id = ?",
            (quiz_id, employee_id),
        ).fetchone()
        is not None
    )


def sync_quiz_checklist_step(db, employee_id, quiz_id):
    """Called after an admin edits or deletes an attempt — keeps the checklist
    step's completed state honest: complete if any passing attempt remains,
    pending again if not."""
    has_pass = db.execute(
        "SELECT 1 FROM quiz_attempts WHERE employee_id = ? AND quiz_id = ? AND passed = 1",
        (employee_id, quiz_id),
    ).fetchone()
    if has_pass:
        mark_quiz_step_complete(db, employee_id, quiz_id)
    else:
        db.execute(
            """UPDATE onboarding_steps SET completed_at = NULL
               WHERE employee_id = ? AND step_type = 'quiz' AND related_id = ?""",
            (employee_id, quiz_id),
        )


# ---------------------------------------------------------------------------
# Root / login / logout
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    if "user_id" not in session:
        return redirect(url_for("login"))
    if session.get("role") in get_admin_side_roles(get_db()):
        return redirect(url_for("admin_dashboard"))
    return redirect(url_for("employee_dashboard"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip().lower()
        password = request.form.get("password", "")

        db = get_db()
        employee = db.execute(
            "SELECT * FROM employees WHERE lower(username) = ?", (username,)
        ).fetchone()

        from werkzeug.security import check_password_hash

        if (
            employee is None
            or not employee["password_hash"]
            or not check_password_hash(employee["password_hash"], password)
        ):
            flash("Invalid username or password.", "error")
            return render_template("login.html")

        if employee["status"] == "Inactive":
            flash("This account is inactive. Contact an administrator.", "error")
            return render_template("login.html")

        session["user_id"] = employee["id"]
        session["name"] = employee["name"]
        session["role"] = employee["role"]

        if employee["role"] in get_admin_side_roles(db):
            return redirect(url_for("admin_dashboard"))
        return redirect(url_for("employee_dashboard"))

    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ---------------------------------------------------------------------------
# Admin: dashboard + employee management
# ---------------------------------------------------------------------------

@app.route("/admin/dashboard")
def admin_dashboard():
    resp = require_admin_or_manager()
    if resp:
        return resp

    db = get_db()
    employees = db.execute(
        "SELECT * FROM employees ORDER BY created_at DESC"
    ).fetchall()

    rows = []
    for emp in employees:
        _, _, _, onboarding_pct = onboarding_progress(db, emp["id"])
        _, _, _, training_pct = training_progress(db, emp["id"])
        rows.append(
            {
                "employee": emp,
                "onboarding_pct": onboarding_pct,
                "training_pct": training_pct,
            }
        )

    document_count = db.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    training_count = db.execute("SELECT COUNT(*) FROM training_modules").fetchone()[0]
    quiz_count = db.execute("SELECT COUNT(*) FROM quizzes").fetchone()[0]
    employee_count = len(employees)

    stuck_onboarding, failed_quizzes, stuck_threshold_days = get_at_a_glance_alerts(db)

    return render_template(
        "admin_dashboard.html",
        rows=rows,
        document_count=document_count,
        training_count=training_count,
        quiz_count=quiz_count,
        employee_count=employee_count,
        stuck_onboarding=stuck_onboarding,
        failed_quizzes=failed_quizzes,
        stuck_threshold_days=stuck_threshold_days,
    )


@app.route("/admin/dashboard/alert-threshold", methods=["POST"])
def update_stuck_onboarding_threshold():
    resp = require_admin_or_manager()
    if resp:
        return resp

    db = get_db()
    try:
        days = max(1, int(request.form.get("days", "7")))
    except ValueError:
        days = 7
    set_setting(db, "stuck_onboarding_days", str(days))
    db.commit()
    flash(f"Alert threshold set to {days} day(s).", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/employees/add", methods=["GET", "POST"])
def add_employee():
    resp = require_permission("employees_add")
    if resp:
        return resp

    db = get_db()

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        role = request.form.get("role", "Employee")
        job_title = request.form.get("job_title", "").strip()
        department = request.form.get("department", "").strip()
        hire_date = request.form.get("hire_date", "").strip()
        date_of_birth = request.form.get("date_of_birth", "").strip()
        template_id = request.form.get("onboarding_template_id") or None

        if role not in get_all_roles(db):
            role = "Employee"

        existing = db.execute(
            "SELECT id FROM employees WHERE lower(email) = ?", (email,)
        ).fetchone()
        if existing:
            flash("An employee with that email already exists.", "error")
            templates = db.execute("SELECT * FROM onboarding_templates ORDER BY name").fetchall()
            return render_template("add_employee.html", templates=templates, custom_roles=get_custom_role_names(db))

        try:
            dob_date = datetime.strptime(date_of_birth, "%Y-%m-%d")
        except ValueError:
            flash("Please enter a valid date of birth — it's used to generate their password.", "error")
            templates = db.execute("SELECT * FROM onboarding_templates ORDER BY name").fetchall()
            return render_template("add_employee.html", templates=templates, custom_roles=get_custom_role_names(db))

        from werkzeug.security import generate_password_hash

        username = generate_username(db, name)
        password = dob_date.strftime("%m%d%Y")
        password_hash = generate_password_hash(password)

        token = secrets.token_urlsafe(24)
        cur = db.execute(
            """INSERT INTO employees
               (name, email, username, password_hash, role, job_title, department, hire_date,
                date_of_birth, status, onboarding_token, onboarding_token_used, onboarding_template_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'Pending', ?, 0, ?)""",
            (
                name, email, username, password_hash, role, job_title, department, hire_date,
                date_of_birth, token, template_id,
            ),
        )
        new_id = cur.lastrowid
        seed_onboarding_steps(db, new_id)
        db.commit()

        onboarding_link = url_for("onboarding", token=token, _external=True)
        flash(
            f'Employee added. Username: "{username}" · Password: "{password}" (their date of birth). '
            f"Send them this one-time onboarding link to activate their account: {onboarding_link}",
            "success",
        )
        return redirect(url_for("employee_profile_admin", employee_id=new_id))

    templates = db.execute("SELECT * FROM onboarding_templates ORDER BY name").fetchall()
    return render_template("add_employee.html", templates=templates, custom_roles=get_custom_role_names(db))


@app.route("/admin/employees/<int:employee_id>")
def employee_profile_admin(employee_id):
    resp = require_admin_or_manager()
    if resp:
        return resp

    db = get_db()
    employee = db.execute("SELECT * FROM employees WHERE id = ?", (employee_id,)).fetchone()
    if employee is None:
        flash("Employee not found.", "error")
        return redirect(url_for("admin_dashboard"))

    steps, steps_done, steps_total, onboarding_pct = onboarding_progress(db, employee_id)
    assignments, train_done, train_total, training_pct = training_progress(db, employee_id)
    signatures = db.execute(
        """SELECT signatures.*, documents.title AS document_title
           FROM signatures JOIN documents ON documents.id = signatures.document_id
           WHERE signatures.employee_id = ? ORDER BY signatures.signed_at DESC""",
        (employee_id,),
    ).fetchall()
    notes = db.execute(
        "SELECT * FROM notes WHERE employee_id = ? ORDER BY created_at DESC", (employee_id,)
    ).fetchall()
    uploads = db.execute(
        "SELECT * FROM employee_uploads WHERE employee_id = ? ORDER BY uploaded_at DESC",
        (employee_id,),
    ).fetchall()

    onboarding_link = None
    if employee["onboarding_token"] and not employee["onboarding_token_used"]:
        onboarding_link = url_for("onboarding", token=employee["onboarding_token"], _external=True)

    onboarding_template = None
    if employee["onboarding_template_id"]:
        onboarding_template = db.execute(
            "SELECT * FROM onboarding_templates WHERE id = ?", (employee["onboarding_template_id"],)
        ).fetchone()

    return render_template(
        "employee_profile_admin.html",
        employee=employee,
        steps=steps,
        onboarding_pct=onboarding_pct,
        assignments=assignments,
        training_pct=training_pct,
        signatures=signatures,
        notes=notes,
        uploads=uploads,
        onboarding_link=onboarding_link,
        onboarding_template=onboarding_template,
    )


@app.route("/admin/employees/checklist-steps/<int:step_id>/delete", methods=["POST"])
def delete_employee_onboarding_step(step_id):
    resp = require_permission("employees_checklist")
    if resp:
        return resp

    db = get_db()
    step = db.execute("SELECT * FROM onboarding_steps WHERE id = ?", (step_id,)).fetchone()
    if step is None:
        flash("Checklist step not found.", "error")
        return redirect(url_for("admin_dashboard"))

    employee_id = step["employee_id"]
    # A file the employee already uploaded for this step shouldn't vanish —
    # just detach it from the step being removed; it stays visible under
    # their own "My Documents" list.
    db.execute(
        "UPDATE employee_uploads SET onboarding_step_id = NULL WHERE onboarding_step_id = ?",
        (step_id,),
    )
    db.execute("DELETE FROM onboarding_steps WHERE id = ?", (step_id,))
    db.commit()
    flash(f'Removed "{step["step_name"]}" from the checklist.', "success")
    return redirect(url_for("employee_profile_admin", employee_id=employee_id))


@app.route("/admin/employees/<int:employee_id>/toggle-status", methods=["POST"])
def toggle_employee_status(employee_id):
    resp = require_permission("employees_status")
    if resp:
        return resp

    db = get_db()
    employee = db.execute("SELECT * FROM employees WHERE id = ?", (employee_id,)).fetchone()
    if employee is None:
        flash("Employee not found.", "error")
        return redirect(url_for("admin_dashboard"))

    if employee_id == session["user_id"]:
        flash("You cannot deactivate your own account.", "error")
        return redirect(request.referrer or url_for("admin_dashboard"))

    new_status = "Active" if employee["status"] != "Active" else "Inactive"
    db.execute("UPDATE employees SET status = ? WHERE id = ?", (new_status, employee_id))
    db.commit()
    flash(f'{employee["name"]} is now {new_status.lower()}.', "success")
    return redirect(request.referrer or url_for("admin_dashboard"))


@app.route("/admin/employees/<int:employee_id>/change-password", methods=["POST"])
def change_employee_password(employee_id):
    resp = require_permission("employees_password")
    if resp:
        return resp

    db = get_db()
    employee = db.execute("SELECT * FROM employees WHERE id = ?", (employee_id,)).fetchone()
    if employee is None:
        flash("Employee not found.", "error")
        return redirect(url_for("admin_dashboard"))

    new_password = request.form.get("new_password", "")
    confirm_password = request.form.get("confirm_password", "")
    redirect_url = url_for("employee_profile_admin", employee_id=employee_id) + "#danger"

    if len(new_password) < 6:
        flash("Password must be at least 6 characters.", "error")
        return redirect(redirect_url)
    if new_password != confirm_password:
        flash("Passwords don't match.", "error")
        return redirect(redirect_url)

    from werkzeug.security import generate_password_hash

    db.execute(
        "UPDATE employees SET password_hash = ? WHERE id = ?",
        (generate_password_hash(new_password), employee_id),
    )
    db.commit()
    flash(f"Password changed for {employee['name']}.", "success")
    return redirect(redirect_url)


@app.route("/admin/employees/<int:employee_id>/edit", methods=["GET", "POST"])
def edit_employee(employee_id):
    resp = require_permission("employees_edit")
    if resp:
        return resp

    db = get_db()
    employee = db.execute("SELECT * FROM employees WHERE id = ?", (employee_id,)).fetchone()
    if employee is None:
        flash("Employee not found.", "error")
        return redirect(url_for("admin_dashboard"))

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        role = request.form.get("role", employee["role"])
        job_title = request.form.get("job_title", "").strip()
        department = request.form.get("department", "").strip()
        hire_date = request.form.get("hire_date", "").strip()
        status = request.form.get("status", employee["status"])
        date_of_birth = request.form.get("date_of_birth", "").strip()
        template_id = request.form.get("onboarding_template_id") or None

        if role not in get_all_roles(db):
            role = employee["role"]
        if status not in ("Active", "Pending", "Inactive"):
            status = employee["status"]

        password_hash = employee["password_hash"]
        new_password = None
        if date_of_birth and date_of_birth != (employee["date_of_birth"] or ""):
            try:
                dob_date = datetime.strptime(date_of_birth, "%Y-%m-%d")
            except ValueError:
                flash("Please enter a valid date of birth.", "error")
                templates = db.execute("SELECT * FROM onboarding_templates ORDER BY name").fetchall()
                return render_template("edit_employee.html", employee=employee, templates=templates, custom_roles=get_custom_role_names(db))

            from werkzeug.security import generate_password_hash

            new_password = dob_date.strftime("%m%d%Y")
            password_hash = generate_password_hash(new_password)

        username = employee["username"]
        new_username = generate_username(db, name, exclude_employee_id=employee_id)
        if new_username != username:
            username = new_username

        db.execute(
            """UPDATE employees SET name = ?, email = ?, username = ?, role = ?, job_title = ?,
               department = ?, hire_date = ?, status = ?, date_of_birth = ?,
               password_hash = ?, onboarding_template_id = ? WHERE id = ?""",
            (
                name, email, username, role, job_title, department, hire_date, status,
                date_of_birth or None, password_hash, template_id, employee_id,
            ),
        )

        if template_id:
            apply_onboarding_template_items(db, employee_id, int(template_id))

        db.commit()

        if session["user_id"] == employee_id:
            session["name"] = name
            session["role"] = role

        notices = []
        if username != employee["username"]:
            notices.append(f'username is now "{username}"')
        if new_password:
            notices.append(f'password is now "{new_password}" (date of birth)')
        if notices:
            flash("Employee updated. " + " and ".join(notices).capitalize() + ".", "success")
        else:
            flash("Employee updated.", "success")
        return redirect(url_for("employee_profile_admin", employee_id=employee_id))

    templates = db.execute("SELECT * FROM onboarding_templates ORDER BY name").fetchall()
    return render_template("edit_employee.html", employee=employee, templates=templates, custom_roles=get_custom_role_names(db))


@app.route("/admin/employees/<int:employee_id>/delete", methods=["POST"])
def delete_employee(employee_id):
    resp = require_admin()
    if resp:
        return resp

    confirm = request.form.get("confirm", "").strip().lower()
    if confirm != "delete":
        flash('You must type "delete" to confirm.', "error")
        return redirect(url_for("employee_profile_admin", employee_id=employee_id))

    if employee_id == session["user_id"]:
        flash("You cannot delete your own account.", "error")
        return redirect(url_for("employee_profile_admin", employee_id=employee_id))

    db = get_db()
    uploads = db.execute(
        "SELECT file_path FROM employee_uploads WHERE employee_id = ?", (employee_id,)
    ).fetchall()
    for upload in uploads:
        try:
            os.remove(os.path.join(EMPLOYEE_UPLOAD_FOLDER, upload["file_path"]))
        except OSError:
            pass

    db.execute("DELETE FROM employee_uploads WHERE employee_id = ?", (employee_id,))
    db.execute("DELETE FROM notes WHERE employee_id = ?", (employee_id,))
    db.execute("DELETE FROM signatures WHERE employee_id = ?", (employee_id,))
    db.execute("DELETE FROM training_assignments WHERE employee_id = ?", (employee_id,))
    db.execute("DELETE FROM onboarding_steps WHERE employee_id = ?", (employee_id,))
    db.execute(
        """DELETE FROM quiz_attempt_answers WHERE attempt_id IN (
             SELECT id FROM quiz_attempts WHERE employee_id = ?
           )""",
        (employee_id,),
    )
    db.execute("DELETE FROM quiz_attempts WHERE employee_id = ?", (employee_id,))
    db.execute("DELETE FROM quiz_locks WHERE employee_id = ?", (employee_id,))
    db.execute("DELETE FROM employees WHERE id = ?", (employee_id,))
    db.commit()

    flash("Employee deleted.", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/employees/<int:employee_id>/notes", methods=["POST"])
def add_note(employee_id):
    resp = require_permission("employees_notes")
    if resp:
        return resp

    body = request.form.get("body", "").strip()
    if body:
        db = get_db()
        db.execute(
            "INSERT INTO notes (employee_id, author_name, body) VALUES (?, ?, ?)",
            (employee_id, session["name"], body),
        )
        db.commit()
        flash("Note added.", "success")

    return redirect(url_for("employee_profile_admin", employee_id=employee_id) + "#notes")


@app.route("/admin/employees/<int:employee_id>/notes/<int:note_id>/edit", methods=["POST"])
def edit_note(employee_id, note_id):
    resp = require_permission("employees_notes")
    if resp:
        return resp

    db = get_db()
    note = db.execute(
        "SELECT * FROM notes WHERE id = ? AND employee_id = ?", (note_id, employee_id)
    ).fetchone()
    if note is None:
        flash("Note not found.", "error")
        return redirect(url_for("employee_profile_admin", employee_id=employee_id) + "#notes")

    body = request.form.get("body", "").strip()
    if body:
        db.execute(
            "UPDATE notes SET body = ?, updated_at = ? WHERE id = ?",
            (body, datetime.utcnow().isoformat(timespec="seconds"), note_id),
        )
        db.commit()
        flash("Note updated.", "success")

    return redirect(url_for("employee_profile_admin", employee_id=employee_id) + "#notes")


@app.route("/admin/employees/<int:employee_id>/notes/<int:note_id>/delete", methods=["POST"])
def delete_note(employee_id, note_id):
    resp = require_permission("employees_notes")
    if resp:
        return resp

    db = get_db()
    db.execute(
        "DELETE FROM notes WHERE id = ? AND employee_id = ?", (note_id, employee_id)
    )
    db.commit()
    flash("Note deleted.", "success")

    return redirect(url_for("employee_profile_admin", employee_id=employee_id) + "#notes")


@app.route("/admin/employees/<int:employee_id>/request-upload", methods=["POST"])
def request_employee_upload(employee_id):
    resp = require_permission("employees_checklist")
    if resp:
        return resp

    db = get_db()
    employee = db.execute("SELECT id FROM employees WHERE id = ?", (employee_id,)).fetchone()
    if employee is None:
        flash("Employee not found.", "error")
        return redirect(url_for("admin_dashboard"))

    label = request.form.get("label", "").strip()
    if not label:
        flash("Please describe what document you want uploaded.", "error")
        return redirect(url_for("employee_profile_admin", employee_id=employee_id))

    db.execute(
        """INSERT INTO onboarding_steps (employee_id, step_name, step_type, related_id)
           VALUES (?, ?, 'upload', NULL)""",
        (employee_id, label),
    )
    db.commit()

    flash("Document upload requested — it now appears on their checklist.", "success")
    return redirect(url_for("employee_profile_admin", employee_id=employee_id))


# ---------------------------------------------------------------------------
# Onboarding invite flow
# ---------------------------------------------------------------------------

@app.route("/onboarding/<token>", methods=["GET", "POST"])
def onboarding(token):
    db = get_db()
    employee = db.execute(
        "SELECT * FROM employees WHERE onboarding_token = ?", (token,)
    ).fetchone()

    if employee is None or employee["onboarding_token_used"]:
        return render_template("onboarding_setup_password.html", invalid=True)

    page_heading = get_setting(db, "onboarding_page_heading", DEFAULT_ONBOARDING_HEADING)
    page_message = get_setting(db, "onboarding_page_message", DEFAULT_ONBOARDING_MESSAGE)
    button_text = get_setting(db, "onboarding_page_button_text", DEFAULT_ONBOARDING_BUTTON)

    if request.method == "POST":
        if not employee["date_of_birth"]:
            flash(
                "This account has no date of birth on file, so a password can't be "
                "generated yet. Contact your administrator.",
                "error",
            )
            return render_template(
                "onboarding_setup_password.html",
                employee=employee,
                page_heading=page_heading,
                page_message=page_message,
                button_text=button_text,
            )

        db.execute(
            "UPDATE employees SET onboarding_token_used = 1, status = 'Active' WHERE id = ?",
            (employee["id"],),
        )
        db.commit()

        session["user_id"] = employee["id"]
        session["name"] = employee["name"]
        session["role"] = employee["role"]

        dob_date = datetime.strptime(employee["date_of_birth"], "%Y-%m-%d")
        password = dob_date.strftime("%m%d%Y")
        return render_template(
            "onboarding_complete.html", employee=employee, password=password
        )

    return render_template(
        "onboarding_setup_password.html",
        employee=employee,
        page_heading=page_heading,
        page_message=page_message,
        button_text=button_text,
    )


# ---------------------------------------------------------------------------
# Employee: dashboard, checklist, profile
# ---------------------------------------------------------------------------

@app.route("/employee/dashboard")
def employee_dashboard():
    resp = require_login()
    if resp:
        return resp

    db = get_db()
    employee = current_employee(db)
    steps, steps_done, steps_total, onboarding_pct = onboarding_progress(db, employee["id"])
    assignments, train_done, train_total, training_pct = training_progress(db, employee["id"])

    pending_docs = db.execute(
        """SELECT documents.* FROM documents
           WHERE documents.requires_signature = 1
             AND documents.id NOT IN (
               SELECT document_id FROM signatures WHERE employee_id = ?
             )""",
        (employee["id"],),
    ).fetchall()

    next_step = None
    next_step_url = None
    for step in steps:
        if not step["completed_at"]:
            next_step = step
            break
    if next_step:
        step_type = next_step["step_type"]
        if step_type == "document":
            next_step_url = url_for("document_sign", document_id=next_step["related_id"])
        elif step_type == "training":
            next_step_url = url_for("training_module_view", module_id=next_step["related_id"])
        elif step_type == "quiz":
            next_step_url = url_for("take_quiz", quiz_id=next_step["related_id"])
        else:
            next_step_url = url_for("employee_checklist")

    return render_template(
        "employee_dashboard.html",
        employee=employee,
        onboarding_pct=onboarding_pct,
        steps_done=steps_done,
        steps_total=steps_total,
        assignments=assignments,
        train_done=train_done,
        train_total=train_total,
        training_pct=training_pct,
        pending_docs=pending_docs,
        next_step=next_step,
        next_step_url=next_step_url,
    )


@app.route("/employee/checklist")
def employee_checklist():
    resp = require_login()
    if resp:
        return resp

    db = get_db()
    employee = current_employee(db)
    steps, steps_done, steps_total, onboarding_pct = onboarding_progress(db, employee["id"])
    uploads = db.execute(
        "SELECT * FROM employee_uploads WHERE employee_id = ? ORDER BY uploaded_at DESC",
        (employee["id"],),
    ).fetchall()
    step_uploads = {u["onboarding_step_id"]: u for u in uploads if u["onboarding_step_id"]}

    return render_template(
        "employee_checklist.html",
        employee=employee,
        steps=steps,
        onboarding_pct=onboarding_pct,
        steps_done=steps_done,
        steps_total=steps_total,
        uploads=uploads,
        step_uploads=step_uploads,
    )


@app.route("/employee/uploads", methods=["POST"])
def upload_employee_document():
    resp = require_login()
    if resp:
        return resp

    label = request.form.get("label", "").strip()
    file = request.files.get("upload_file")

    if not label or not file or not file.filename:
        flash("Please provide a label and choose a file.", "error")
        return redirect(url_for("employee_checklist"))

    if not allowed_file(file.filename):
        flash("Unsupported file type. Allowed: PDF, Word, text, PNG, JPG.", "error")
        return redirect(url_for("employee_checklist"))

    ext = file.filename.rsplit(".", 1)[1].lower()
    stored_name = f"{secrets.token_hex(8)}.{ext}"
    file.save(os.path.join(EMPLOYEE_UPLOAD_FOLDER, stored_name))

    db = get_db()
    db.execute(
        "INSERT INTO employee_uploads (employee_id, label, file_path) VALUES (?, ?, ?)",
        (session["user_id"], label, stored_name),
    )
    db.commit()

    flash("Document uploaded.", "success")
    return redirect(url_for("employee_checklist"))


@app.route("/uploads/employee/<int:upload_id>")
def employee_upload_file(upload_id):
    resp = require_login()
    if resp:
        return resp

    db = get_db()
    upload = db.execute("SELECT * FROM employee_uploads WHERE id = ?", (upload_id,)).fetchone()
    if upload is None:
        flash("File not found.", "error")
        return redirect(url_for("employee_dashboard"))

    if session.get("role") not in get_admin_side_roles(db) and upload["employee_id"] != session["user_id"]:
        flash("You do not have permission to view that file.", "error")
        return redirect(url_for("employee_dashboard"))

    return send_from_directory(EMPLOYEE_UPLOAD_FOLDER, upload["file_path"])


@app.route("/employee/uploads/<int:upload_id>/delete", methods=["POST"])
def delete_employee_upload(upload_id):
    resp = require_login()
    if resp:
        return resp

    db = get_db()
    upload = db.execute("SELECT * FROM employee_uploads WHERE id = ?", (upload_id,)).fetchone()
    if upload is None:
        flash("File not found.", "error")
        return redirect(url_for("employee_checklist"))

    if session.get("role") not in get_admin_side_roles(db) and upload["employee_id"] != session["user_id"]:
        flash("You do not have permission to delete that file.", "error")
        return redirect(url_for("employee_checklist"))

    is_admin_view = session.get("role") in get_admin_side_roles(db) and upload["employee_id"] != session["user_id"]

    try:
        os.remove(os.path.join(EMPLOYEE_UPLOAD_FOLDER, upload["file_path"]))
    except OSError:
        pass
    db.execute("DELETE FROM employee_uploads WHERE id = ?", (upload_id,))
    db.commit()

    flash("File removed.", "success")
    if is_admin_view:
        return redirect(
            url_for("employee_profile_admin", employee_id=upload["employee_id"]) + "#documents"
        )
    return redirect(url_for("employee_checklist"))


@app.route("/employee/checklist/<int:step_id>/complete", methods=["POST"])
def complete_task_step(step_id):
    resp = require_login()
    if resp:
        return resp

    db = get_db()
    step = db.execute(
        "SELECT * FROM onboarding_steps WHERE id = ? AND employee_id = ?",
        (step_id, session["user_id"]),
    ).fetchone()

    if step and step["step_type"] == "task" and not step["completed_at"]:
        db.execute(
            "UPDATE onboarding_steps SET completed_at = ? WHERE id = ?",
            (datetime.utcnow().isoformat(timespec="seconds"), step_id),
        )
        db.commit()
        flash("Step marked complete.", "success")

    return redirect(url_for("employee_checklist"))


@app.route("/employee/checklist/<int:step_id>/upload", methods=["POST"])
def fulfill_upload_step(step_id):
    resp = require_login()
    if resp:
        return resp

    db = get_db()
    step = db.execute(
        "SELECT * FROM onboarding_steps WHERE id = ? AND employee_id = ?",
        (step_id, session["user_id"]),
    ).fetchone()

    if step is None or step["step_type"] != "upload" or step["completed_at"]:
        flash("That upload request is no longer available.", "error")
        return redirect(url_for("employee_checklist"))

    file = request.files.get("upload_file")
    if not file or not file.filename:
        flash("Please choose a file to upload.", "error")
        return redirect(url_for("employee_checklist"))
    if not allowed_file(file.filename):
        flash("Unsupported file type. Allowed: PDF, Word, text, PNG, JPG.", "error")
        return redirect(url_for("employee_checklist"))

    ext = file.filename.rsplit(".", 1)[1].lower()
    stored_name = f"{secrets.token_hex(8)}.{ext}"
    file.save(os.path.join(EMPLOYEE_UPLOAD_FOLDER, stored_name))

    db.execute(
        """INSERT INTO employee_uploads (employee_id, label, file_path, onboarding_step_id)
           VALUES (?, ?, ?, ?)""",
        (session["user_id"], step["step_name"], stored_name, step_id),
    )
    db.execute(
        "UPDATE onboarding_steps SET completed_at = ? WHERE id = ?",
        (datetime.utcnow().isoformat(timespec="seconds"), step_id),
    )
    db.commit()

    flash("Document uploaded.", "success")
    return redirect(url_for("employee_checklist"))


@app.route("/employee/profile", methods=["GET", "POST"])
def employee_profile_self():
    resp = require_login()
    if resp:
        return resp

    db = get_db()
    employee = current_employee(db)

    if request.method == "POST":
        phone = request.form.get("phone", "").strip()
        emergency_contact_name = request.form.get("emergency_contact_name", "").strip()
        emergency_contact_phone = request.form.get("emergency_contact_phone", "").strip()

        db.execute(
            """UPDATE employees SET phone = ?, emergency_contact_name = ?, emergency_contact_phone = ?
               WHERE id = ?""",
            (phone, emergency_contact_name, emergency_contact_phone, employee["id"]),
        )
        db.commit()
        flash("Profile updated.", "success")
        return redirect(url_for("employee_profile_self"))

    signatures = db.execute(
        """SELECT signatures.*, documents.title AS document_title
           FROM signatures JOIN documents ON documents.id = signatures.document_id
           WHERE signatures.employee_id = ? ORDER BY signatures.signed_at DESC""",
        (employee["id"],),
    ).fetchall()
    assignments, _, _, _ = training_progress(db, employee["id"])

    return render_template(
        "employee_profile_self.html",
        employee=employee,
        signatures=signatures,
        assignments=assignments,
    )


# ---------------------------------------------------------------------------
# Documents (admin management + employee signing)
# ---------------------------------------------------------------------------

@app.route("/admin/documents", methods=["GET", "POST"])
def admin_documents():
    resp = require_admin_or_manager()
    if resp:
        return resp

    db = get_db()

    if request.method == "POST":
        resp = require_permission("documents_create")
        if resp:
            return resp

        title = request.form.get("title", "").strip()
        content = request.form.get("content", "").strip()
        document_type = request.form.get("document_type", "sign")
        requires_signature = 1 if document_type == "sign" else 0
        requires_upload = 1 if document_type == "upload" else 0
        is_onboarding = 1 if request.form.get("is_onboarding") == "on" else 0

        file = request.files.get("document_file")
        file_path = None
        if file and file.filename:
            if not allowed_file(file.filename):
                flash("Unsupported file type. Allowed: PDF, Word, text, PNG, JPG.", "error")
                return redirect(url_for("admin_documents"))
            ext = file.filename.rsplit(".", 1)[1].lower()
            stored_name = f"{secrets.token_hex(8)}.{ext}"
            file.save(os.path.join(UPLOAD_FOLDER, stored_name))
            file_path = stored_name

        if title:
            cur = db.execute(
                """INSERT INTO documents (title, content, file_path, requires_signature, requires_upload, is_onboarding)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (title, content, file_path, requires_signature, requires_upload, is_onboarding),
            )
            new_doc_id = cur.lastrowid

            if is_onboarding:
                document = db.execute(
                    "SELECT * FROM documents WHERE id = ?", (new_doc_id,)
                ).fetchone()
                employees = db.execute(
                    "SELECT id FROM employees WHERE role = 'Employee'"
                ).fetchall()
                for emp in employees:
                    assign_document_to_employee(db, document, emp["id"])

            db.commit()
            if is_onboarding:
                flash("Document created and assigned to every employee.", "success")
            else:
                flash("Document created. Pick which onboarding checklist(s) it belongs to below.", "success")
            return redirect(url_for("document_audit", document_id=new_doc_id) + "#onboarding")
        return redirect(url_for("admin_documents"))

    documents = db.execute("SELECT * FROM documents ORDER BY created_at DESC").fetchall()
    doc_rows = []
    for doc in documents:
        if doc["requires_upload"]:
            uploaded_count = db.execute(
                """SELECT COUNT(*) FROM onboarding_steps
                   WHERE step_type = 'upload' AND related_id = ? AND completed_at IS NOT NULL""",
                (doc["id"],),
            ).fetchone()[0]
            doc_rows.append({"document": doc, "signed_count": uploaded_count})
        else:
            signed_count = db.execute(
                "SELECT COUNT(*) FROM signatures WHERE document_id = ?", (doc["id"],)
            ).fetchone()[0]
            doc_rows.append({"document": doc, "signed_count": signed_count})

    return render_template("admin_documents.html", doc_rows=doc_rows)


@app.route("/admin/documents/<int:document_id>/toggle-onboarding", methods=["POST"])
def toggle_document_onboarding(document_id):
    resp = require_permission("documents_assign")
    if resp:
        return resp

    db = get_db()
    document = db.execute("SELECT * FROM documents WHERE id = ?", (document_id,)).fetchone()
    if document is None:
        flash("Document not found.", "error")
        return redirect(url_for("admin_documents"))

    new_value = 0 if document["is_onboarding"] else 1
    db.execute("UPDATE documents SET is_onboarding = ? WHERE id = ?", (new_value, document_id))

    if new_value:
        document = db.execute("SELECT * FROM documents WHERE id = ?", (document_id,)).fetchone()
        employees = db.execute("SELECT id FROM employees WHERE role = 'Employee'").fetchall()
        for emp in employees:
            assign_document_to_employee(db, document, emp["id"])
        flash(f'"{document["title"]}" added to every employee\'s onboarding checklist.', "success")
    else:
        flash(f'"{document["title"]}" removed from new-employee onboarding checklists.', "success")

    db.commit()
    return redirect(url_for("document_audit", document_id=document_id) + "#onboarding")


@app.route("/admin/documents/<int:document_id>/checklists", methods=["POST"])
def update_document_checklists(document_id):
    resp = require_permission("documents_assign")
    if resp:
        return resp

    db = get_db()
    document = db.execute("SELECT * FROM documents WHERE id = ?", (document_id,)).fetchone()
    if document is None:
        flash("Document not found.", "error")
        return redirect(url_for("admin_documents"))

    step_type = "upload" if document["requires_upload"] else "document"
    verb = "Upload" if document["requires_upload"] else "Sign"

    selected_ids = {int(x) for x in request.form.getlist("template_ids")}
    existing_items = db.execute(
        "SELECT * FROM onboarding_template_items WHERE step_type = ? AND related_id = ?",
        (step_type, document_id),
    ).fetchall()
    existing_template_ids = {item["template_id"] for item in existing_items}

    for template_id in selected_ids - existing_template_ids:
        max_order = db.execute(
            "SELECT COALESCE(MAX(sort_order), -1) FROM onboarding_template_items WHERE template_id = ?",
            (template_id,),
        ).fetchone()[0]
        db.execute(
            """INSERT INTO onboarding_template_items (template_id, step_name, step_type, related_id, sort_order)
               VALUES (?, ?, ?, ?, ?)""",
            (template_id, f"{verb} {document['title']}", step_type, document_id, max_order + 1),
        )
        # Apply immediately to employees already on this checklist, not just future hires.
        current_members = db.execute(
            "SELECT id FROM employees WHERE onboarding_template_id = ? AND role = 'Employee'",
            (template_id,),
        ).fetchall()
        for emp in current_members:
            assign_document_to_employee(db, document, emp["id"])

    for item in existing_items:
        if item["template_id"] not in selected_ids:
            db.execute("DELETE FROM onboarding_template_items WHERE id = ?", (item["id"],))

    db.commit()
    flash("Onboarding checklist membership updated.", "success")
    return redirect(url_for("document_audit", document_id=document_id) + "#onboarding")


@app.route("/admin/documents/<int:document_id>")
def document_audit(document_id):
    resp = require_admin_or_manager()
    if resp:
        return resp

    db = get_db()
    document = db.execute("SELECT * FROM documents WHERE id = ?", (document_id,)).fetchone()
    if document is None:
        flash("Document not found.", "error")
        return redirect(url_for("admin_documents"))

    step_type = "upload" if document["requires_upload"] else "document"
    templates = db.execute("SELECT * FROM onboarding_templates ORDER BY name").fetchall()
    linked_template_ids = {
        row["template_id"]
        for row in db.execute(
            "SELECT template_id FROM onboarding_template_items WHERE step_type = ? AND related_id = ?",
            (step_type, document_id),
        ).fetchall()
    }

    if document["requires_upload"]:
        uploaded = db.execute(
            """SELECT employee_uploads.*, employees.name AS employee_name
               FROM employee_uploads
               JOIN onboarding_steps ON onboarding_steps.id = employee_uploads.onboarding_step_id
               JOIN employees ON employees.id = employee_uploads.employee_id
               WHERE onboarding_steps.step_type = 'upload' AND onboarding_steps.related_id = ?
               ORDER BY employee_uploads.uploaded_at DESC""",
            (document_id,),
        ).fetchall()
        not_uploaded = db.execute(
            """SELECT employees.* FROM employees
               JOIN onboarding_steps ON onboarding_steps.employee_id = employees.id
               WHERE onboarding_steps.step_type = 'upload' AND onboarding_steps.related_id = ?
                 AND onboarding_steps.completed_at IS NULL""",
            (document_id,),
        ).fetchall()
        return render_template(
            "document_audit.html",
            document=document,
            uploaded=uploaded,
            not_uploaded=not_uploaded,
            templates=templates,
            linked_template_ids=linked_template_ids,
        )

    signed = db.execute(
        """SELECT signatures.*, employees.name AS employee_name
           FROM signatures JOIN employees ON employees.id = signatures.employee_id
           WHERE document_id = ? ORDER BY signed_at DESC""",
        (document_id,),
    ).fetchall()
    unsigned = db.execute(
        """SELECT employees.* FROM employees
           JOIN onboarding_steps ON onboarding_steps.employee_id = employees.id
           WHERE onboarding_steps.step_type = 'document' AND onboarding_steps.related_id = ?
             AND onboarding_steps.completed_at IS NULL""",
        (document_id,),
    ).fetchall()

    return render_template(
        "document_audit.html",
        document=document,
        signed=signed,
        unsigned=unsigned,
        templates=templates,
        linked_template_ids=linked_template_ids,
    )


@app.route("/admin/documents/<int:document_id>/edit", methods=["POST"])
def edit_document(document_id):
    resp = require_permission("documents_edit")
    if resp:
        return resp

    db = get_db()
    document = db.execute("SELECT * FROM documents WHERE id = ?", (document_id,)).fetchone()
    if document is None:
        flash("Document not found.", "error")
        return redirect(url_for("admin_documents"))

    title = request.form.get("title", "").strip()
    content = request.form.get("content", "").strip()
    if not title:
        flash("Title is required.", "error")
        return redirect(url_for("document_audit", document_id=document_id))

    file = request.files.get("document_file")
    file_path = document["file_path"]
    if file and file.filename:
        if not allowed_file(file.filename):
            flash("Unsupported file type. Allowed: PDF, Word, text, PNG, JPG.", "error")
            return redirect(url_for("document_audit", document_id=document_id))
        if file_path:
            try:
                os.remove(os.path.join(UPLOAD_FOLDER, file_path))
            except OSError:
                pass
        ext = file.filename.rsplit(".", 1)[1].lower()
        stored_name = f"{secrets.token_hex(8)}.{ext}"
        file.save(os.path.join(UPLOAD_FOLDER, stored_name))
        file_path = stored_name

    db.execute(
        "UPDATE documents SET title = ?, content = ?, file_path = ? WHERE id = ?",
        (title, content, file_path, document_id),
    )
    db.commit()
    flash("Document updated.", "success")
    return redirect(url_for("document_audit", document_id=document_id))


@app.route("/admin/documents/<int:document_id>/delete", methods=["POST"])
def delete_document(document_id):
    resp = require_permission("documents_delete")
    if resp:
        return resp

    confirm = request.form.get("confirm", "").strip().lower()
    if confirm != "delete":
        flash('You must type "delete" to confirm.', "error")
        return redirect(url_for("admin_documents"))

    db = get_db()
    document = db.execute("SELECT file_path FROM documents WHERE id = ?", (document_id,)).fetchone()
    if document and document["file_path"]:
        try:
            os.remove(os.path.join(UPLOAD_FOLDER, document["file_path"]))
        except OSError:
            pass

    uploads = db.execute(
        """SELECT employee_uploads.id, employee_uploads.file_path
           FROM employee_uploads
           JOIN onboarding_steps ON onboarding_steps.id = employee_uploads.onboarding_step_id
           WHERE onboarding_steps.step_type = 'upload' AND onboarding_steps.related_id = ?""",
        (document_id,),
    ).fetchall()
    for upload in uploads:
        try:
            os.remove(os.path.join(EMPLOYEE_UPLOAD_FOLDER, upload["file_path"]))
        except OSError:
            pass
    db.execute(
        """DELETE FROM employee_uploads WHERE onboarding_step_id IN (
             SELECT id FROM onboarding_steps WHERE step_type = 'upload' AND related_id = ?
           )""",
        (document_id,),
    )

    db.execute(
        "DELETE FROM onboarding_steps WHERE step_type = 'document' AND related_id = ?",
        (document_id,),
    )
    db.execute(
        "DELETE FROM onboarding_template_items WHERE step_type = 'document' AND related_id = ?",
        (document_id,),
    )
    db.execute(
        "DELETE FROM onboarding_steps WHERE step_type = 'upload' AND related_id = ?",
        (document_id,),
    )
    db.execute(
        "DELETE FROM onboarding_template_items WHERE step_type = 'upload' AND related_id = ?",
        (document_id,),
    )
    db.execute("DELETE FROM signatures WHERE document_id = ?", (document_id,))
    db.execute("DELETE FROM documents WHERE id = ?", (document_id,))
    db.commit()

    flash("Document deleted.", "success")
    return redirect(url_for("admin_documents"))


@app.route("/admin/documents/signatures/<int:signature_id>/delete", methods=["POST"])
def delete_signature(signature_id):
    resp = require_permission("documents_signatures")
    if resp:
        return resp

    db = get_db()
    signature = db.execute("SELECT * FROM signatures WHERE id = ?", (signature_id,)).fetchone()
    if signature is None:
        flash("Signature not found.", "error")
        return redirect(url_for("admin_documents"))

    document_id = signature["document_id"]
    employee_id = signature["employee_id"]
    db.execute("DELETE FROM signatures WHERE id = ?", (signature_id,))
    # Signing was what completed this checklist step — removing the signature
    # puts the requirement back on their checklist as incomplete.
    db.execute(
        """UPDATE onboarding_steps SET completed_at = NULL
           WHERE employee_id = ? AND step_type = 'document' AND related_id = ?""",
        (employee_id, document_id),
    )
    db.commit()
    flash("Signature removed; that employee's checklist step is pending again.", "success")
    return redirect(url_for("document_audit", document_id=document_id))


@app.route("/documents/file/<path:filename>")
def document_file(filename):
    resp = require_login()
    if resp:
        return resp
    return send_from_directory(UPLOAD_FOLDER, filename)


@app.route("/employee/documents/<int:document_id>/sign", methods=["GET", "POST"])
def document_sign(document_id):
    resp = require_login()
    if resp:
        return resp

    db = get_db()
    document = db.execute("SELECT * FROM documents WHERE id = ?", (document_id,)).fetchone()
    if document is None:
        flash("Document not found.", "error")
        return redirect(url_for("employee_dashboard"))

    existing_signature = db.execute(
        "SELECT * FROM signatures WHERE employee_id = ? AND document_id = ?",
        (session["user_id"], document_id),
    ).fetchone()

    if request.method == "POST" and not existing_signature:
        signature_text = request.form.get("signature_text", "").strip()
        agree = request.form.get("agree") == "on"

        if not signature_text or not agree:
            flash("Please type your full name and check the agreement box.", "error")
            return render_template("document_sign.html", document=document, signature=None)

        db.execute(
            """INSERT INTO signatures (employee_id, document_id, signature_text, session_marker)
               VALUES (?, ?, ?, ?)""",
            (session["user_id"], document_id, signature_text, request.remote_addr),
        )
        mark_document_step_complete(db, session["user_id"], document_id)
        db.commit()

        flash("Document signed.", "success")
        return redirect(url_for("employee_dashboard"))

    return render_template("document_sign.html", document=document, signature=existing_signature)


# ---------------------------------------------------------------------------
# Training (admin management + employee completion)
# ---------------------------------------------------------------------------

@app.route("/admin/training")
def admin_training():
    resp = require_admin_or_manager()
    if resp:
        return resp

    db = get_db()
    modules = db.execute("SELECT * FROM training_modules ORDER BY created_at DESC").fetchall()
    module_rows = []
    for mod in modules:
        assigned_count = db.execute(
            "SELECT COUNT(*) FROM training_assignments WHERE module_id = ?", (mod["id"],)
        ).fetchone()[0]
        completed_count = db.execute(
            "SELECT COUNT(*) FROM training_assignments WHERE module_id = ? AND completed_at IS NOT NULL",
            (mod["id"],),
        ).fetchone()[0]
        slide_count = db.execute(
            "SELECT COUNT(*) FROM training_slides WHERE module_id = ?", (mod["id"],)
        ).fetchone()[0]
        module_rows.append(
            {
                "module": mod,
                "assigned_count": assigned_count,
                "completed_count": completed_count,
                "slide_count": slide_count,
            }
        )

    return render_template("admin_training.html", module_rows=module_rows)


@app.route("/admin/training/create", methods=["GET", "POST"])
def add_training_module():
    resp = require_permission("training_create")
    if resp:
        return resp

    if request.method == "POST":
        db = get_db()
        title = request.form.get("title", "").strip()
        description = request.form.get("description", "").strip()
        content = request.form.get("content", "").strip()
        is_onboarding = 1 if request.form.get("is_onboarding") == "on" else 0

        if not title:
            flash("Title is required.", "error")
            return render_template("add_training.html")

        cur = db.execute(
            "INSERT INTO training_modules (title, description, content, is_onboarding) VALUES (?, ?, ?, ?)",
            (title, description, content, is_onboarding),
        )
        new_id = cur.lastrowid
        save_slides(
            db, new_id, request.files.getlist("slides"), request.form.getlist("slide_captions")
        )

        if is_onboarding:
            employees = db.execute("SELECT id FROM employees WHERE role = 'Employee'").fetchall()
            for emp in employees:
                assign_module_to_employee(db, new_id, title, emp["id"])

        db.commit()
        flash("Training module created.", "success")
        return redirect(url_for("training_detail_admin", module_id=new_id))

    return render_template("add_training.html")


@app.route("/admin/training/<int:module_id>/toggle-onboarding", methods=["POST"])
def toggle_module_onboarding(module_id):
    resp = require_permission("training_assign")
    if resp:
        return resp

    db = get_db()
    module = db.execute("SELECT * FROM training_modules WHERE id = ?", (module_id,)).fetchone()
    if module is None:
        flash("Training module not found.", "error")
        return redirect(url_for("admin_training"))

    new_value = 0 if module["is_onboarding"] else 1
    db.execute("UPDATE training_modules SET is_onboarding = ? WHERE id = ?", (new_value, module_id))

    if new_value:
        employees = db.execute("SELECT id FROM employees WHERE role = 'Employee'").fetchall()
        for emp in employees:
            assign_module_to_employee(db, module_id, module["title"], emp["id"])
        flash(f'"{module["title"]}" added to every employee\'s onboarding checklist.', "success")
    else:
        flash(f'"{module["title"]}" removed from new-employee onboarding checklists.', "success")

    db.commit()
    return redirect(url_for("training_detail_admin", module_id=module_id) + "#onboarding")


@app.route("/admin/training/<int:module_id>")
def training_detail_admin(module_id):
    resp = require_admin_or_manager()
    if resp:
        return resp

    db = get_db()
    module = db.execute("SELECT * FROM training_modules WHERE id = ?", (module_id,)).fetchone()
    if module is None:
        flash("Training module not found.", "error")
        return redirect(url_for("admin_training"))

    assignments = db.execute(
        """SELECT training_assignments.*, employees.name AS employee_name
           FROM training_assignments JOIN employees ON employees.id = training_assignments.employee_id
           WHERE module_id = ? ORDER BY training_assignments.assigned_at DESC""",
        (module_id,),
    ).fetchall()
    assigned_ids = {a["employee_id"] for a in assignments}
    unassigned = [
        e
        for e in db.execute(
            "SELECT * FROM employees WHERE role = 'Employee' ORDER BY name"
        ).fetchall()
        if e["id"] not in assigned_ids
    ]
    slides = get_module_slides(db, module_id)

    templates = db.execute("SELECT * FROM onboarding_templates ORDER BY name").fetchall()
    linked_template_ids = {
        row["template_id"]
        for row in db.execute(
            """SELECT template_id FROM onboarding_template_items
               WHERE step_type = 'training' AND related_id = ?""",
            (module_id,),
        ).fetchall()
    }

    return render_template(
        "training_detail_admin.html",
        module=module,
        assignments=assignments,
        unassigned=unassigned,
        slides=slides,
        templates=templates,
        linked_template_ids=linked_template_ids,
    )


@app.route("/admin/training/<int:module_id>/preview")
def preview_training_module(module_id):
    resp = require_admin_or_manager()
    if resp:
        return resp

    db = get_db()
    module = db.execute("SELECT * FROM training_modules WHERE id = ?", (module_id,)).fetchone()
    if module is None:
        flash("Training module not found.", "error")
        return redirect(url_for("admin_training"))

    slides = get_module_slides(db, module_id)

    return render_template(
        "training_module.html", module=module, assignment=None, slides=slides, preview=True
    )


@app.route("/admin/training/<int:module_id>/edit", methods=["POST"])
def edit_training_module(module_id):
    resp = require_permission("training_edit")
    if resp:
        return resp

    db = get_db()
    module = db.execute("SELECT id FROM training_modules WHERE id = ?", (module_id,)).fetchone()
    if module is None:
        flash("Training module not found.", "error")
        return redirect(url_for("admin_training"))

    title = request.form.get("title", "").strip()
    description = request.form.get("description", "").strip()
    content = request.form.get("content", "").strip()

    if not title:
        flash("Title is required.", "error")
        return redirect(url_for("training_detail_admin", module_id=module_id))

    db.execute(
        "UPDATE training_modules SET title = ?, description = ?, content = ? WHERE id = ?",
        (title, description, content, module_id),
    )
    db.commit()

    flash("Module updated.", "success")
    return redirect(url_for("training_detail_admin", module_id=module_id))


@app.route("/admin/training/<int:module_id>/slides", methods=["POST"])
def add_training_slides(module_id):
    resp = require_permission("training_slides")
    if resp:
        return resp

    db = get_db()
    module = db.execute("SELECT id FROM training_modules WHERE id = ?", (module_id,)).fetchone()
    if module is None:
        flash("Training module not found.", "error")
        return redirect(url_for("admin_training"))

    added = save_slides(
        db, module_id, request.files.getlist("slides"), request.form.getlist("slide_captions")
    )
    db.commit()

    if added:
        flash(f"Added {added} slide(s).", "success")
    else:
        flash("No valid images were uploaded. Allowed: PNG, JPG, GIF, WEBP.", "error")

    return redirect(url_for("training_detail_admin", module_id=module_id) + "#slides")


@app.route("/admin/training/slides/<int:slide_id>/delete", methods=["POST"])
def delete_training_slide(slide_id):
    resp = require_permission("training_slides")
    if resp:
        return resp

    db = get_db()
    slide = db.execute("SELECT * FROM training_slides WHERE id = ?", (slide_id,)).fetchone()
    if slide is None:
        flash("Slide not found.", "error")
        return redirect(url_for("admin_training"))

    module_id = slide["module_id"]
    if slide["image_path"]:
        try:
            os.remove(os.path.join(TRAINING_SLIDES_FOLDER, slide["image_path"]))
        except OSError:
            pass

    elements = db.execute(
        "SELECT * FROM slide_elements WHERE slide_id = ?", (slide_id,)
    ).fetchall()
    for el in elements:
        if el["element_type"] == "image" and el["content"]:
            try:
                os.remove(os.path.join(TRAINING_SLIDES_FOLDER, el["content"]))
            except OSError:
                pass
    db.execute("DELETE FROM slide_elements WHERE slide_id = ?", (slide_id,))
    db.execute("DELETE FROM training_slides WHERE id = ?", (slide_id,))
    db.commit()

    flash("Slide removed.", "success")
    return redirect(url_for("training_detail_admin", module_id=module_id) + "#slides")


@app.route("/admin/training/slides/<int:slide_id>/caption", methods=["POST"])
def update_training_slide_caption(slide_id):
    resp = require_permission("training_slides")
    if resp:
        return resp

    db = get_db()
    slide = db.execute("SELECT * FROM training_slides WHERE id = ?", (slide_id,)).fetchone()
    if slide is None:
        flash("Slide not found.", "error")
        return redirect(url_for("admin_training"))

    caption = request.form.get("caption", "").strip()
    db.execute("UPDATE training_slides SET caption = ? WHERE id = ?", (caption or None, slide_id))
    db.commit()

    flash("Slide caption updated.", "success")
    return redirect(url_for("training_detail_admin", module_id=slide["module_id"]) + "#slides")


# ---------------------------------------------------------------------------
# Slide canvas editor (advanced, PowerPoint-style slide building)
# ---------------------------------------------------------------------------

@app.route("/admin/training/slides/<int:slide_id>/edit")
def slide_editor(slide_id):
    resp = require_permission("training_slides")
    if resp:
        return resp

    db = get_db()
    slide = db.execute("SELECT * FROM training_slides WHERE id = ?", (slide_id,)).fetchone()
    if slide is None:
        flash("Slide not found.", "error")
        return redirect(url_for("admin_training"))

    module = db.execute(
        "SELECT * FROM training_modules WHERE id = ?", (slide["module_id"],)
    ).fetchone()
    all_slides = get_module_slides(db, slide["module_id"])
    elements = db.execute(
        "SELECT * FROM slide_elements WHERE slide_id = ? ORDER BY z_index, id", (slide_id,)
    ).fetchall()

    return render_template(
        "slide_editor.html",
        module=module,
        slide=slide,
        all_slides=all_slides,
        elements=elements,
    )


@app.route("/admin/training/<int:module_id>/slides/blank", methods=["POST"])
def add_blank_slide(module_id):
    resp = require_permission("training_slides")
    if resp:
        return resp

    db = get_db()
    module = db.execute("SELECT id FROM training_modules WHERE id = ?", (module_id,)).fetchone()
    if module is None:
        flash("Training module not found.", "error")
        return redirect(url_for("admin_training"))

    max_order = db.execute(
        "SELECT COALESCE(MAX(sort_order), -1) FROM training_slides WHERE module_id = ?",
        (module_id,),
    ).fetchone()[0]
    cur = db.execute(
        "INSERT INTO training_slides (module_id, image_path, sort_order) VALUES (?, '', ?)",
        (module_id, max_order + 1),
    )
    db.commit()

    return redirect(url_for("slide_editor", slide_id=cur.lastrowid))


@app.route("/admin/training/slides/<int:slide_id>/duplicate", methods=["POST"])
def duplicate_training_slide(slide_id):
    resp = require_permission("training_slides")
    if resp:
        return resp

    db = get_db()
    slide = db.execute("SELECT * FROM training_slides WHERE id = ?", (slide_id,)).fetchone()
    if slide is None:
        flash("Slide not found.", "error")
        return redirect(url_for("admin_training"))

    module_id = slide["module_id"]
    max_order = db.execute(
        "SELECT COALESCE(MAX(sort_order), -1) FROM training_slides WHERE module_id = ?",
        (module_id,),
    ).fetchone()[0]

    # Legacy file-based slides (rare now — new uploads store bytes in the DB directly).
    new_image_path = slide["image_path"]
    if new_image_path:
        src = os.path.join(TRAINING_SLIDES_FOLDER, new_image_path)
        ext = new_image_path.rsplit(".", 1)[-1]
        candidate = f"{secrets.token_hex(8)}.{ext}"
        try:
            shutil.copyfile(src, os.path.join(TRAINING_SLIDES_FOLDER, candidate))
            new_image_path = candidate
        except OSError:
            pass

    cur = db.execute(
        """INSERT INTO training_slides
           (module_id, image_path, caption, background_color, sort_order,
            media_data, media_mimetype, media_kind)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            module_id,
            new_image_path,
            slide["caption"],
            slide["background_color"],
            max_order + 1,
            slide["media_data"],
            slide["media_mimetype"],
            slide["media_kind"],
        ),
    )
    new_slide_id = cur.lastrowid

    elements = db.execute(
        "SELECT * FROM slide_elements WHERE slide_id = ?", (slide_id,)
    ).fetchall()
    for el in elements:
        content = el["content"]
        if el["element_type"] in ("image", "video") and content and not el["media_data"]:
            # Legacy file-based element (no BLOB yet) — copy the file if it still exists.
            src = os.path.join(TRAINING_SLIDES_FOLDER, content)
            ext = content.rsplit(".", 1)[-1]
            new_name = f"{secrets.token_hex(8)}.{ext}"
            try:
                shutil.copyfile(src, os.path.join(TRAINING_SLIDES_FOLDER, new_name))
                content = new_name
            except OSError:
                pass
        db.execute(
            """INSERT INTO slide_elements
               (slide_id, element_type, content, pos_x, pos_y, width, height, z_index,
                font_size, color, bold, align, media_data, media_mimetype)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                new_slide_id,
                el["element_type"],
                content,
                el["pos_x"],
                el["pos_y"],
                el["width"],
                el["height"],
                el["z_index"],
                el["font_size"],
                el["color"],
                el["bold"],
                el["align"],
                el["media_data"],
                el["media_mimetype"],
            ),
        )

    db.commit()
    flash("Slide duplicated.", "success")
    return redirect(url_for("slide_editor", slide_id=new_slide_id))


@app.route("/admin/training/slides/<int:slide_id>/background", methods=["POST"])
def update_slide_background(slide_id):
    resp = require_permission("training_slides")
    if resp:
        return resp

    db = get_db()
    slide = db.execute("SELECT id FROM training_slides WHERE id = ?", (slide_id,)).fetchone()
    if slide is None:
        return {"error": "not found"}, 404

    data = request.get_json(silent=True) or {}
    color = str(data.get("background_color", ""))
    if not HEX_COLOR_RE.match(color):
        return {"error": "invalid color"}, 400

    db.execute("UPDATE training_slides SET background_color = ? WHERE id = ?", (color, slide_id))
    db.commit()
    return {"ok": True}


@app.route("/admin/training/<int:module_id>/slides/reorder", methods=["POST"])
def reorder_slides(module_id):
    resp = require_permission("training_slides")
    if resp:
        return resp

    db = get_db()
    module = db.execute("SELECT id FROM training_modules WHERE id = ?", (module_id,)).fetchone()
    if module is None:
        return {"error": "not found"}, 404

    data = request.get_json(silent=True) or {}
    order = data.get("order", [])
    valid_ids = {
        row["id"]
        for row in db.execute(
            "SELECT id FROM training_slides WHERE module_id = ?", (module_id,)
        ).fetchall()
    }

    for index, slide_id in enumerate(order):
        try:
            slide_id = int(slide_id)
        except (TypeError, ValueError):
            continue
        if slide_id in valid_ids:
            db.execute(
                "UPDATE training_slides SET sort_order = ? WHERE id = ?", (index, slide_id)
            )

    db.commit()
    return {"ok": True}


@app.route("/admin/training/slides/<int:slide_id>/elements/text", methods=["POST"])
def add_text_element(slide_id):
    resp = require_permission("training_slides")
    if resp:
        return resp

    db = get_db()
    slide = db.execute("SELECT id FROM training_slides WHERE id = ?", (slide_id,)).fetchone()
    if slide is None:
        return {"error": "not found"}, 404

    max_z = db.execute(
        "SELECT COALESCE(MAX(z_index), 0) FROM slide_elements WHERE slide_id = ?", (slide_id,)
    ).fetchone()[0]

    cur = db.execute(
        """INSERT INTO slide_elements
           (slide_id, element_type, content, pos_x, pos_y, width, height, z_index,
            font_size, color, bold, align)
           VALUES (?, 'text', 'New text', 10, 10, 40, 15, ?, 18, '#1f2430', 0, 'left')""",
        (slide_id, max_z + 1),
    )
    db.commit()

    element = db.execute(
        "SELECT * FROM slide_elements WHERE id = ?", (cur.lastrowid,)
    ).fetchone()
    return {"ok": True, "element": dict(element)}


@app.route("/admin/training/slides/<int:slide_id>/elements/media", methods=["POST"])
def add_media_element(slide_id):
    resp = require_permission("training_slides")
    if resp:
        return resp

    db = get_db()
    slide = db.execute("SELECT id FROM training_slides WHERE id = ?", (slide_id,)).fetchone()
    if slide is None:
        return {"error": "not found"}, 404

    file = request.files.get("image_file")
    if not file or not file.filename or not allowed_media(file.filename):
        return {"error": "invalid file — allowed: PNG, JPG, GIF, WEBP, MP4, WEBM, OGG, MOV"}, 400

    data = file.read()
    if not data or len(data) > MAX_MEDIA_BYTES:
        return {"error": "file too large (50 MB max)"}, 400

    mimetype = file.mimetype or guess_mimetype(file.filename)
    kind = media_kind_for(file.filename)

    max_z = db.execute(
        "SELECT COALESCE(MAX(z_index), 0) FROM slide_elements WHERE slide_id = ?", (slide_id,)
    ).fetchone()[0]

    cur = db.execute(
        """INSERT INTO slide_elements
           (slide_id, element_type, content, pos_x, pos_y, width, height, z_index,
            media_data, media_mimetype)
           VALUES (?, ?, NULL, 10, 10, 50, 50, ?, ?, ?)""",
        (slide_id, kind, max_z + 1, data, mimetype),
    )
    db.commit()

    element = db.execute(
        "SELECT * FROM slide_elements WHERE id = ?", (cur.lastrowid,)
    ).fetchone()
    result = dict(element)
    result.pop("media_data", None)
    result["media_url"] = url_for("slide_element_media", element_id=element["id"])
    return {"ok": True, "element": result}


@app.route("/admin/training/slides/elements/<int:element_id>/update", methods=["POST"])
def update_slide_element(element_id):
    resp = require_permission("training_slides")
    if resp:
        return resp

    db = get_db()
    element = db.execute("SELECT * FROM slide_elements WHERE id = ?", (element_id,)).fetchone()
    if element is None:
        return {"error": "not found"}, 404

    data = request.get_json(silent=True) or {}
    fields = {}

    for key in ("pos_x", "pos_y", "width", "height"):
        if key in data:
            try:
                fields[key] = max(0.0, min(100.0, float(data[key])))
            except (TypeError, ValueError):
                pass

    if "content" in data and element["element_type"] == "text":
        fields["content"] = str(data["content"])[:4000]

    if "font_size" in data:
        try:
            fields["font_size"] = max(8, min(120, int(data["font_size"])))
        except (TypeError, ValueError):
            pass

    if "color" in data:
        color = str(data["color"])
        if HEX_COLOR_RE.match(color):
            fields["color"] = color

    if "bold" in data:
        fields["bold"] = 1 if data["bold"] else 0

    if "align" in data and data["align"] in ("left", "center", "right"):
        fields["align"] = data["align"]

    if "z_index" in data:
        try:
            fields["z_index"] = max(0, min(9999, int(data["z_index"])))
        except (TypeError, ValueError):
            pass

    if not fields:
        return {"ok": True}

    set_clause = ", ".join(f"{k} = ?" for k in fields)
    db.execute(
        f"UPDATE slide_elements SET {set_clause} WHERE id = ?",
        (*fields.values(), element_id),
    )
    db.commit()
    return {"ok": True}


@app.route("/admin/training/slides/elements/<int:element_id>/bring-to-front", methods=["POST"])
def bring_element_to_front(element_id):
    resp = require_permission("training_slides")
    if resp:
        return resp

    db = get_db()
    element = db.execute("SELECT * FROM slide_elements WHERE id = ?", (element_id,)).fetchone()
    if element is None:
        return {"error": "not found"}, 404

    max_z = db.execute(
        "SELECT COALESCE(MAX(z_index), 0) FROM slide_elements WHERE slide_id = ?",
        (element["slide_id"],),
    ).fetchone()[0]
    new_z = max_z + 1
    db.execute("UPDATE slide_elements SET z_index = ? WHERE id = ?", (new_z, element_id))
    db.commit()
    return {"ok": True, "z_index": new_z}


@app.route("/admin/training/slides/elements/<int:element_id>/delete", methods=["POST"])
def delete_slide_element(element_id):
    resp = require_permission("training_slides")
    if resp:
        return resp

    db = get_db()
    element = db.execute("SELECT * FROM slide_elements WHERE id = ?", (element_id,)).fetchone()
    if element is None:
        return {"error": "not found"}, 404

    if element["element_type"] == "image" and element["content"]:
        try:
            os.remove(os.path.join(TRAINING_SLIDES_FOLDER, element["content"]))
        except OSError:
            pass

    db.execute("DELETE FROM slide_elements WHERE id = ?", (element_id,))
    db.commit()
    return {"ok": True}


@app.route("/uploads/training-slide/<path:filename>")
def training_slide_file(filename):
    resp = require_login()
    if resp:
        return resp
    return send_from_directory(TRAINING_SLIDES_FOLDER, filename)


@app.route("/uploads/training-slide-media/<int:slide_id>")
def training_slide_media(slide_id):
    """Serves a slide's image/video straight from the database — works no matter
    where the app is deployed, since it doesn't depend on the local filesystem."""
    resp = require_login()
    if resp:
        return resp

    db = get_db()
    slide = db.execute(
        "SELECT media_data, media_mimetype FROM training_slides WHERE id = ?", (slide_id,)
    ).fetchone()
    if slide is None or slide["media_data"] is None:
        flash("File not found.", "error")
        return redirect(url_for("employee_dashboard"))

    return send_file(
        io.BytesIO(slide["media_data"]),
        mimetype=slide["media_mimetype"] or "application/octet-stream",
    )


@app.route("/uploads/slide-element-media/<int:element_id>")
def slide_element_media(element_id):
    """Serves a canvas element's image/video straight from the database."""
    resp = require_login()
    if resp:
        return resp

    db = get_db()
    element = db.execute(
        "SELECT media_data, media_mimetype FROM slide_elements WHERE id = ?", (element_id,)
    ).fetchone()
    if element is None or element["media_data"] is None:
        flash("File not found.", "error")
        return redirect(url_for("employee_dashboard"))

    return send_file(
        io.BytesIO(element["media_data"]),
        mimetype=element["media_mimetype"] or "application/octet-stream",
    )


@app.route("/admin/training/<int:module_id>/assign", methods=["POST"])
def assign_training(module_id):
    resp = require_permission("training_assign")
    if resp:
        return resp

    db = get_db()
    module = db.execute("SELECT * FROM training_modules WHERE id = ?", (module_id,)).fetchone()
    if module is None:
        flash("Training module not found.", "error")
        return redirect(url_for("admin_training"))

    if request.form.get("assign_all") == "on":
        target_ids = [
            e["id"]
            for e in db.execute("SELECT id FROM employees WHERE role = 'Employee'").fetchall()
        ]
    else:
        target_ids = [int(x) for x in request.form.getlist("employee_ids")]

    for emp_id in target_ids:
        assign_module_to_employee(db, module_id, module["title"], emp_id)

    db.commit()
    flash("Training assigned.", "success")
    return redirect(url_for("training_detail_admin", module_id=module_id) + "#assign")


@app.route("/admin/training/<int:module_id>/delete", methods=["POST"])
def delete_training(module_id):
    resp = require_permission("training_delete")
    if resp:
        return resp

    confirm = request.form.get("confirm", "").strip().lower()
    if confirm != "delete":
        flash('You must type "delete" to confirm.', "error")
        return redirect(url_for("admin_training"))

    db = get_db()
    slides = db.execute(
        "SELECT id, image_path FROM training_slides WHERE module_id = ?", (module_id,)
    ).fetchall()
    for slide in slides:
        if slide["image_path"]:
            try:
                os.remove(os.path.join(TRAINING_SLIDES_FOLDER, slide["image_path"]))
            except OSError:
                pass
        elements = db.execute(
            "SELECT * FROM slide_elements WHERE slide_id = ?", (slide["id"],)
        ).fetchall()
        for el in elements:
            if el["element_type"] == "image" and el["content"]:
                try:
                    os.remove(os.path.join(TRAINING_SLIDES_FOLDER, el["content"]))
                except OSError:
                    pass
        db.execute("DELETE FROM slide_elements WHERE slide_id = ?", (slide["id"],))

    db.execute("DELETE FROM training_slides WHERE module_id = ?", (module_id,))
    db.execute(
        "DELETE FROM onboarding_steps WHERE step_type = 'training' AND related_id = ?",
        (module_id,),
    )
    db.execute("DELETE FROM training_assignments WHERE module_id = ?", (module_id,))
    db.execute(
        "DELETE FROM onboarding_template_items WHERE step_type = 'training' AND related_id = ?",
        (module_id,),
    )
    # Quizzes can reference a training module as "related material" — that's a
    # soft link, not a reason to block deleting the module, so just detach it.
    db.execute(
        "UPDATE quizzes SET training_module_id = NULL WHERE training_module_id = ?",
        (module_id,),
    )
    db.execute("DELETE FROM training_modules WHERE id = ?", (module_id,))
    db.commit()

    flash("Training module deleted.", "success")
    return redirect(url_for("admin_training"))


@app.route("/employee/training/<int:module_id>", methods=["GET", "POST"])
def training_module_view(module_id):
    resp = require_login()
    if resp:
        return resp

    db = get_db()
    module = db.execute("SELECT * FROM training_modules WHERE id = ?", (module_id,)).fetchone()
    if module is None:
        flash("Training module not found.", "error")
        return redirect(url_for("employee_dashboard"))

    assignment = db.execute(
        "SELECT * FROM training_assignments WHERE employee_id = ? AND module_id = ?",
        (session["user_id"], module_id),
    ).fetchone()
    if assignment is None:
        flash("You are not assigned to this training module.", "error")
        return redirect(url_for("employee_dashboard"))

    slides = get_module_slides(db, module_id)

    if request.method == "POST" and not assignment["completed_at"]:
        acknowledge = request.form.get("acknowledge") == "on"
        if not acknowledge:
            flash("Please confirm you completed the module.", "error")
            return render_template(
                "training_module.html", module=module, assignment=assignment, slides=slides
            )

        db.execute(
            """UPDATE training_assignments SET completed_at = ?, status = 'Completed'
               WHERE id = ?""",
            (datetime.utcnow().isoformat(timespec="seconds"), assignment["id"]),
        )
        mark_training_step_complete(db, session["user_id"], module_id)
        db.commit()

        flash("Training module marked complete.", "success")
        return redirect(url_for("employee_dashboard"))

    return render_template(
        "training_module.html", module=module, assignment=assignment, slides=slides
    )


# ---------------------------------------------------------------------------
# Quizzes (admin/manager management + employee taking)
# ---------------------------------------------------------------------------

def delete_question_cascade(db, question_id):
    """Remove a question along with its choices and any recorded answers that
    reference it. Attempts themselves (score/total) are left alone — they're
    a historical record of what an employee scored at the time."""
    db.execute("DELETE FROM quiz_attempt_answers WHERE question_id = ?", (question_id,))
    db.execute("DELETE FROM quiz_choices WHERE question_id = ?", (question_id,))
    db.execute("DELETE FROM quiz_questions WHERE id = ?", (question_id,))


def delete_quiz_cascade(db, quiz_id):
    question_ids = [
        row["id"]
        for row in db.execute("SELECT id FROM quiz_questions WHERE quiz_id = ?", (quiz_id,)).fetchall()
    ]
    for question_id in question_ids:
        delete_question_cascade(db, question_id)
    db.execute("DELETE FROM quiz_attempts WHERE quiz_id = ?", (quiz_id,))
    db.execute("DELETE FROM quiz_locks WHERE quiz_id = ?", (quiz_id,))
    db.execute(
        "DELETE FROM onboarding_steps WHERE step_type = 'quiz' AND related_id = ?", (quiz_id,)
    )
    db.execute(
        "DELETE FROM onboarding_template_items WHERE step_type = 'quiz' AND related_id = ?",
        (quiz_id,),
    )
    db.execute("DELETE FROM quizzes WHERE id = ?", (quiz_id,))


QUESTION_TYPES = {"single_choice", "multi_choice", "text", "matching"}


def save_question_choices(db, question_id, choice_texts, correct_indices):
    """Insert non-blank choices for a question. correct_indices (strings from
    the form) mark which ones are correct; falls back to the first choice if
    none resolve to a valid index, so a question is never left answerless."""
    choices = [text.strip() for text in choice_texts if text.strip()]
    if not choices:
        return
    correct_set = set()
    for raw in correct_indices:
        try:
            i = int(raw)
        except (TypeError, ValueError):
            continue
        if 0 <= i < len(choices):
            correct_set.add(i)
    if not correct_set:
        correct_set = {0}
    for i, text in enumerate(choices):
        db.execute(
            """INSERT INTO quiz_choices (question_id, choice_text, is_correct, sort_order)
               VALUES (?, ?, ?, ?)""",
            (question_id, text, 1 if i in correct_set else 0, i),
        )


def save_matching_pairs(db, question_id, prompt_texts, match_texts):
    """Insert non-blank prompt/match pairs for a matching question. A pair is
    only saved if both sides have text — a lone prompt or lone match with
    nothing on the other side isn't a usable pair."""
    pairs = []
    for prompt, match in zip(prompt_texts, match_texts):
        prompt = prompt.strip()
        match = match.strip()
        if prompt and match:
            pairs.append((prompt, match))
    for i, (prompt, match) in enumerate(pairs):
        db.execute(
            """INSERT INTO quiz_choices (question_id, choice_text, match_text, is_correct, sort_order)
               VALUES (?, ?, ?, 1, ?)""",
            (question_id, prompt, match, i),
        )


def save_quiz_question(
    db, question_id, question_type, choice_texts, correct_indices, text_answer,
    prompt_texts=None, match_texts=None,
):
    """Set a question's type and its answer data, replacing whatever was
    there before. Used for both a brand-new question and an edit — clearing
    old choices/answers first makes both cases the same code path."""
    if question_type not in QUESTION_TYPES:
        question_type = "single_choice"

    db.execute("DELETE FROM quiz_attempt_answers WHERE question_id = ?", (question_id,))
    db.execute("DELETE FROM quiz_choices WHERE question_id = ?", (question_id,))

    if question_type == "text":
        db.execute(
            "UPDATE quiz_questions SET question_type = ?, text_answer = ? WHERE id = ?",
            (question_type, text_answer.strip(), question_id),
        )
    elif question_type == "matching":
        db.execute(
            "UPDATE quiz_questions SET question_type = ?, text_answer = NULL WHERE id = ?",
            (question_type, question_id),
        )
        save_matching_pairs(db, question_id, prompt_texts or [], match_texts or [])
    else:
        db.execute(
            "UPDATE quiz_questions SET question_type = ?, text_answer = NULL WHERE id = ?",
            (question_type, question_id),
        )
        save_question_choices(db, question_id, choice_texts, correct_indices)


@app.route("/admin/quizzes")
def admin_quizzes():
    resp = require_admin_or_manager()
    if resp:
        return resp

    db = get_db()
    quizzes = db.execute("SELECT * FROM quizzes ORDER BY created_at DESC").fetchall()
    quiz_rows = []
    for quiz in quizzes:
        question_count = db.execute(
            "SELECT COUNT(*) FROM quiz_questions WHERE quiz_id = ?", (quiz["id"],)
        ).fetchone()[0]
        attempt_count = db.execute(
            "SELECT COUNT(*) FROM quiz_attempts WHERE quiz_id = ?", (quiz["id"],)
        ).fetchone()[0]
        quiz_rows.append(
            {"quiz": quiz, "question_count": question_count, "attempt_count": attempt_count}
        )

    training_modules = db.execute("SELECT id, title FROM training_modules ORDER BY title").fetchall()
    return render_template(
        "admin_quizzes.html", quiz_rows=quiz_rows, training_modules=training_modules
    )


@app.route("/admin/quizzes/create", methods=["GET", "POST"])
def create_quiz():
    resp = require_permission("quizzes_create")
    if resp:
        return resp

    db = get_db()

    if request.method == "POST":
        title = request.form.get("title", "").strip()
        description = request.form.get("description", "").strip()
        training_module_id = request.form.get("training_module_id") or None
        is_onboarding = 1 if request.form.get("is_onboarding") == "on" else 0
        try:
            passing_score = max(0, min(100, int(request.form.get("passing_score", 70))))
        except ValueError:
            passing_score = 70

        question_indices = request.form.getlist("question_indices")
        if not title:
            flash("Title is required.", "error")
            return redirect(url_for("create_quiz"))
        if not question_indices:
            flash("Add at least one question.", "error")
            return redirect(url_for("create_quiz"))

        cur = db.execute(
            """INSERT INTO quizzes (title, description, training_module_id, passing_score, is_onboarding)
               VALUES (?, ?, ?, ?, ?)""",
            (title, description, training_module_id, passing_score, is_onboarding),
        )
        quiz_id = cur.lastrowid

        sort_order = 0
        questions_added = 0
        for i in question_indices:
            question_text = request.form.get(f"q{i}_text", "").strip()
            question_type = request.form.get(f"q{i}_type", "single_choice")
            if question_type not in QUESTION_TYPES:
                question_type = "single_choice"
            if not question_text:
                continue

            qcur = db.execute(
                """INSERT INTO quiz_questions (quiz_id, question_text, question_type, sort_order)
                   VALUES (?, ?, ?, ?)""",
                (quiz_id, question_text, question_type, sort_order),
            )
            question_id = qcur.lastrowid

            if question_type == "text":
                text_answer = request.form.get(f"q{i}_text_answer", "").strip()
                db.execute(
                    "UPDATE quiz_questions SET text_answer = ? WHERE id = ?",
                    (text_answer, question_id),
                )
            elif question_type == "matching":
                prompt_texts = request.form.getlist(f"q{i}_prompt_text")
                match_texts = request.form.getlist(f"q{i}_match_text")
                save_matching_pairs(db, question_id, prompt_texts, match_texts)
            else:
                choice_texts = request.form.getlist(f"q{i}_choice_text")
                if question_type == "multi_choice":
                    correct_indices = request.form.getlist(f"q{i}_correct_indices")
                else:
                    correct_indices = [request.form.get(f"q{i}_correct_index", "0")]
                save_question_choices(db, question_id, choice_texts, correct_indices)

            sort_order += 1
            questions_added += 1

        if questions_added == 0:
            db.rollback()
            flash("Add at least one question with text.", "error")
            return redirect(url_for("create_quiz"))

        if is_onboarding:
            employees = db.execute("SELECT id FROM employees WHERE role = 'Employee'").fetchall()
            for emp in employees:
                db.execute(
                    """INSERT INTO onboarding_steps (employee_id, step_name, step_type, related_id)
                       VALUES (?, ?, 'quiz', ?)""",
                    (emp["id"], f"Take Quiz: {title}", quiz_id),
                )

        db.commit()
        flash("Quiz created.", "success")
        return redirect(url_for("quiz_detail_admin", quiz_id=quiz_id))

    training_modules = db.execute("SELECT id, title FROM training_modules ORDER BY title").fetchall()
    return render_template("create_quiz.html", training_modules=training_modules)


@app.route("/admin/quizzes/<int:quiz_id>")
def quiz_detail_admin(quiz_id):
    resp = require_admin_or_manager()
    if resp:
        return resp

    db = get_db()
    quiz = db.execute("SELECT * FROM quizzes WHERE id = ?", (quiz_id,)).fetchone()
    if quiz is None:
        flash("Quiz not found.", "error")
        return redirect(url_for("admin_quizzes"))

    questions = db.execute(
        "SELECT * FROM quiz_questions WHERE quiz_id = ? ORDER BY sort_order, id", (quiz_id,)
    ).fetchall()
    question_rows = []
    for question in questions:
        choices = db.execute(
            "SELECT * FROM quiz_choices WHERE question_id = ? ORDER BY sort_order, id",
            (question["id"],),
        ).fetchall()
        question_rows.append({"question": question, "choices": choices})

    training_modules = db.execute("SELECT id, title FROM training_modules ORDER BY title").fetchall()
    attempt_count = db.execute(
        "SELECT COUNT(*) FROM quiz_attempts WHERE quiz_id = ?", (quiz_id,)
    ).fetchone()[0]

    assigned_employee_ids = {
        row["employee_id"]
        for row in db.execute(
            "SELECT DISTINCT employee_id FROM onboarding_steps WHERE step_type = 'quiz' AND related_id = ?",
            (quiz_id,),
        ).fetchall()
    }
    assignable_employees = [
        emp
        for emp in db.execute(
            "SELECT * FROM employees WHERE role = 'Employee' ORDER BY name"
        ).fetchall()
        if emp["id"] not in assigned_employee_ids
    ]

    locked_ids = {
        row["employee_id"]
        for row in db.execute(
            "SELECT employee_id FROM quiz_locks WHERE quiz_id = ?", (quiz_id,)
        ).fetchall()
    }
    all_employees = db.execute(
        "SELECT * FROM employees WHERE role = 'Employee' ORDER BY name"
    ).fetchall()
    lock_rows = [
        {"employee": emp, "locked": emp["id"] in locked_ids} for emp in all_employees
    ]

    return render_template(
        "quiz_detail_admin.html",
        quiz=quiz,
        question_rows=question_rows,
        training_modules=training_modules,
        attempt_count=attempt_count,
        assignable_employees=assignable_employees,
        lock_rows=lock_rows,
    )


@app.route("/admin/quizzes/<int:quiz_id>/edit", methods=["POST"])
def edit_quiz(quiz_id):
    resp = require_permission("quizzes_edit")
    if resp:
        return resp

    db = get_db()
    quiz = db.execute("SELECT id FROM quizzes WHERE id = ?", (quiz_id,)).fetchone()
    if quiz is None:
        flash("Quiz not found.", "error")
        return redirect(url_for("admin_quizzes"))

    title = request.form.get("title", "").strip()
    description = request.form.get("description", "").strip()
    training_module_id = request.form.get("training_module_id") or None
    try:
        passing_score = max(0, min(100, int(request.form.get("passing_score", 70))))
    except ValueError:
        passing_score = 70

    if not title:
        flash("Title is required.", "error")
        return redirect(url_for("quiz_detail_admin", quiz_id=quiz_id))

    db.execute(
        """UPDATE quizzes SET title = ?, description = ?, training_module_id = ?, passing_score = ?
           WHERE id = ?""",
        (title, description, training_module_id, passing_score, quiz_id),
    )
    db.commit()
    flash("Quiz updated.", "success")
    return redirect(url_for("quiz_detail_admin", quiz_id=quiz_id))


@app.route("/admin/quizzes/<int:quiz_id>/toggle-onboarding", methods=["POST"])
def toggle_quiz_onboarding(quiz_id):
    resp = require_permission("quizzes_assign")
    if resp:
        return resp

    db = get_db()
    quiz = db.execute("SELECT * FROM quizzes WHERE id = ?", (quiz_id,)).fetchone()
    if quiz is None:
        flash("Quiz not found.", "error")
        return redirect(url_for("admin_quizzes"))

    new_value = 0 if quiz["is_onboarding"] else 1
    db.execute("UPDATE quizzes SET is_onboarding = ? WHERE id = ?", (new_value, quiz_id))

    if new_value:
        employees = db.execute("SELECT id FROM employees WHERE role = 'Employee'").fetchall()
        for emp in employees:
            already = db.execute(
                """SELECT 1 FROM onboarding_steps
                   WHERE employee_id = ? AND step_type = 'quiz' AND related_id = ?""",
                (emp["id"], quiz_id),
            ).fetchone()
            if already:
                continue
            db.execute(
                """INSERT INTO onboarding_steps (employee_id, step_name, step_type, related_id)
                   VALUES (?, ?, 'quiz', ?)""",
                (emp["id"], f"Take Quiz: {quiz['title']}", quiz_id),
            )
        flash(f'"{quiz["title"]}" added to every employee\'s checklist.', "success")
    else:
        flash(f'"{quiz["title"]}" removed from new-employee onboarding checklists.', "success")

    db.commit()
    return redirect(url_for("quiz_detail_admin", quiz_id=quiz_id))


@app.route("/admin/quizzes/<int:quiz_id>/lock/<int:employee_id>", methods=["POST"])
def toggle_employee_quiz_lock(quiz_id, employee_id):
    resp = require_permission("quizzes_lock")
    if resp:
        return resp

    db = get_db()
    quiz = db.execute("SELECT * FROM quizzes WHERE id = ?", (quiz_id,)).fetchone()
    employee = db.execute("SELECT * FROM employees WHERE id = ?", (employee_id,)).fetchone()
    if quiz is None or employee is None:
        flash("Quiz or employee not found.", "error")
        return redirect(url_for("admin_quizzes"))

    existing = db.execute(
        "SELECT id FROM quiz_locks WHERE quiz_id = ? AND employee_id = ?", (quiz_id, employee_id)
    ).fetchone()
    if existing:
        db.execute("DELETE FROM quiz_locks WHERE id = ?", (existing["id"],))
        flash(f"Unlocked \"{quiz['title']}\" for {employee['name']}.", "success")
    else:
        db.execute(
            "INSERT INTO quiz_locks (quiz_id, employee_id) VALUES (?, ?)", (quiz_id, employee_id)
        )
        flash(f"Locked \"{quiz['title']}\" for {employee['name']}.", "success")
    db.commit()
    return redirect(url_for("quiz_detail_admin", quiz_id=quiz_id))


@app.route("/admin/quizzes/<int:quiz_id>/lock-all", methods=["POST"])
def lock_quiz_for_all(quiz_id):
    resp = require_permission("quizzes_lock")
    if resp:
        return resp

    db = get_db()
    quiz = db.execute("SELECT * FROM quizzes WHERE id = ?", (quiz_id,)).fetchone()
    if quiz is None:
        flash("Quiz not found.", "error")
        return redirect(url_for("admin_quizzes"))

    employees = db.execute("SELECT id FROM employees WHERE role = 'Employee'").fetchall()
    for emp in employees:
        already = db.execute(
            "SELECT 1 FROM quiz_locks WHERE quiz_id = ? AND employee_id = ?", (quiz_id, emp["id"])
        ).fetchone()
        if not already:
            db.execute(
                "INSERT INTO quiz_locks (quiz_id, employee_id) VALUES (?, ?)", (quiz_id, emp["id"])
            )
    db.commit()
    flash(f'"{quiz["title"]}" locked for everyone.', "success")
    return redirect(url_for("quiz_detail_admin", quiz_id=quiz_id))


@app.route("/admin/quizzes/<int:quiz_id>/unlock-all", methods=["POST"])
def unlock_quiz_for_all(quiz_id):
    resp = require_permission("quizzes_lock")
    if resp:
        return resp

    db = get_db()
    quiz = db.execute("SELECT * FROM quizzes WHERE id = ?", (quiz_id,)).fetchone()
    if quiz is None:
        flash("Quiz not found.", "error")
        return redirect(url_for("admin_quizzes"))

    db.execute("DELETE FROM quiz_locks WHERE quiz_id = ?", (quiz_id,))
    db.commit()
    flash(f'"{quiz["title"]}" unlocked for everyone.', "success")
    return redirect(url_for("quiz_detail_admin", quiz_id=quiz_id))


@app.route("/admin/quizzes/<int:quiz_id>/assign", methods=["POST"])
def assign_quiz(quiz_id):
    resp = require_permission("quizzes_assign")
    if resp:
        return resp

    db = get_db()
    quiz = db.execute("SELECT * FROM quizzes WHERE id = ?", (quiz_id,)).fetchone()
    if quiz is None:
        flash("Quiz not found.", "error")
        return redirect(url_for("admin_quizzes"))

    employee_id = request.form.get("employee_id")
    employee = db.execute(
        "SELECT * FROM employees WHERE id = ? AND role = 'Employee'", (employee_id,)
    ).fetchone()
    if employee is None:
        flash("Please choose an employee.", "error")
        return redirect(url_for("quiz_detail_admin", quiz_id=quiz_id))

    already = db.execute(
        """SELECT 1 FROM onboarding_steps
           WHERE employee_id = ? AND step_type = 'quiz' AND related_id = ?""",
        (employee["id"], quiz_id),
    ).fetchone()
    if already:
        flash(f"{employee['name']} already has this quiz on their checklist.", "error")
        return redirect(url_for("quiz_detail_admin", quiz_id=quiz_id))

    db.execute(
        """INSERT INTO onboarding_steps (employee_id, step_name, step_type, related_id)
           VALUES (?, ?, 'quiz', ?)""",
        (employee["id"], f"Take Quiz: {quiz['title']}", quiz_id),
    )
    db.commit()
    flash(f"Assigned to {employee['name']}.", "success")
    return redirect(url_for("quiz_detail_admin", quiz_id=quiz_id))


@app.route("/admin/quizzes/<int:quiz_id>/delete", methods=["POST"])
def delete_quiz(quiz_id):
    resp = require_permission("quizzes_delete")
    if resp:
        return resp

    confirm = request.form.get("confirm", "").strip().lower()
    if confirm != "delete":
        flash('You must type "delete" to confirm.', "error")
        return redirect(url_for("quiz_detail_admin", quiz_id=quiz_id))

    db = get_db()
    quiz = db.execute("SELECT title FROM quizzes WHERE id = ?", (quiz_id,)).fetchone()
    if quiz is None:
        flash("Quiz not found.", "error")
        return redirect(url_for("admin_quizzes"))

    delete_quiz_cascade(db, quiz_id)
    db.commit()
    flash(f'"{quiz["title"]}" deleted.', "success")
    return redirect(url_for("admin_quizzes"))


@app.route("/admin/quizzes/<int:quiz_id>/questions", methods=["POST"])
def add_quiz_question(quiz_id):
    resp = require_permission("quizzes_edit")
    if resp:
        return resp

    db = get_db()
    quiz = db.execute("SELECT id FROM quizzes WHERE id = ?", (quiz_id,)).fetchone()
    if quiz is None:
        flash("Quiz not found.", "error")
        return redirect(url_for("admin_quizzes"))

    question_text = request.form.get("question_text", "").strip()
    question_type = request.form.get("question_type", "single_choice")
    if question_type not in QUESTION_TYPES:
        question_type = "single_choice"
    choice_texts = request.form.getlist("choice_text")
    text_answer = request.form.get("text_answer", "")
    prompt_texts = request.form.getlist("prompt_text")
    match_texts = request.form.getlist("match_text")
    if question_type == "multi_choice":
        correct_indices = request.form.getlist("correct_indices")
    else:
        correct_indices = [request.form.get("correct_index", "0")]

    if not question_text:
        flash("A question needs text.", "error")
        return redirect(url_for("quiz_detail_admin", quiz_id=quiz_id))
    if question_type == "text" and not text_answer.strip():
        flash("Give the correct answer for this text question.", "error")
        return redirect(url_for("quiz_detail_admin", quiz_id=quiz_id))
    if question_type == "matching" and not any(
        p.strip() and m.strip() for p, m in zip(prompt_texts, match_texts)
    ):
        flash("A matching question needs at least one complete prompt/match pair.", "error")
        return redirect(url_for("quiz_detail_admin", quiz_id=quiz_id))
    if question_type not in ("text", "matching") and not any(text.strip() for text in choice_texts):
        flash("A choice-based question needs at least one answer choice.", "error")
        return redirect(url_for("quiz_detail_admin", quiz_id=quiz_id))

    max_order = db.execute(
        "SELECT COALESCE(MAX(sort_order), -1) FROM quiz_questions WHERE quiz_id = ?", (quiz_id,)
    ).fetchone()[0]
    cur = db.execute(
        """INSERT INTO quiz_questions (quiz_id, question_text, sort_order) VALUES (?, ?, ?)""",
        (quiz_id, question_text, max_order + 1),
    )
    save_quiz_question(
        db, cur.lastrowid, question_type, choice_texts, correct_indices, text_answer,
        prompt_texts, match_texts,
    )
    db.commit()
    flash("Question added.", "success")
    return redirect(url_for("quiz_detail_admin", quiz_id=quiz_id))


@app.route("/admin/quizzes/questions/<int:question_id>/edit", methods=["POST"])
def edit_quiz_question(question_id):
    resp = require_permission("quizzes_edit")
    if resp:
        return resp

    db = get_db()
    question = db.execute(
        "SELECT * FROM quiz_questions WHERE id = ?", (question_id,)
    ).fetchone()
    if question is None:
        flash("Question not found.", "error")
        return redirect(url_for("admin_quizzes"))

    quiz_id = question["quiz_id"]
    question_text = request.form.get("question_text", "").strip()
    question_type = request.form.get("question_type", "single_choice")
    if question_type not in QUESTION_TYPES:
        question_type = "single_choice"
    choice_texts = request.form.getlist("choice_text")
    text_answer = request.form.get("text_answer", "")
    prompt_texts = request.form.getlist("prompt_text")
    match_texts = request.form.getlist("match_text")
    if question_type == "multi_choice":
        correct_indices = request.form.getlist("correct_indices")
    else:
        correct_indices = [request.form.get("correct_index", "0")]

    if not question_text:
        flash("A question needs text.", "error")
        return redirect(url_for("quiz_detail_admin", quiz_id=quiz_id))
    if question_type == "text" and not text_answer.strip():
        flash("Give the correct answer for this text question.", "error")
        return redirect(url_for("quiz_detail_admin", quiz_id=quiz_id))
    if question_type == "matching" and not any(
        p.strip() and m.strip() for p, m in zip(prompt_texts, match_texts)
    ):
        flash("A matching question needs at least one complete prompt/match pair.", "error")
        return redirect(url_for("quiz_detail_admin", quiz_id=quiz_id))
    if question_type not in ("text", "matching") and not any(text.strip() for text in choice_texts):
        flash("A choice-based question needs at least one answer choice.", "error")
        return redirect(url_for("quiz_detail_admin", quiz_id=quiz_id))

    db.execute("UPDATE quiz_questions SET question_text = ? WHERE id = ?", (question_text, question_id))
    # save_quiz_question clears old choices/recorded answers before writing the
    # new ones — any previously recorded answers for this question no longer
    # mean anything once it's edited. Overall attempt scores stay as they were.
    save_quiz_question(
        db, question_id, question_type, choice_texts, correct_indices, text_answer,
        prompt_texts, match_texts,
    )
    db.commit()
    flash("Question updated.", "success")
    return redirect(url_for("quiz_detail_admin", quiz_id=quiz_id))


@app.route("/admin/quizzes/questions/<int:question_id>/delete", methods=["POST"])
def delete_quiz_question(question_id):
    resp = require_permission("quizzes_edit")
    if resp:
        return resp

    db = get_db()
    question = db.execute(
        "SELECT quiz_id FROM quiz_questions WHERE id = ?", (question_id,)
    ).fetchone()
    if question is None:
        flash("Question not found.", "error")
        return redirect(url_for("admin_quizzes"))

    quiz_id = question["quiz_id"]
    delete_question_cascade(db, question_id)
    db.commit()
    flash("Question removed.", "success")
    return redirect(url_for("quiz_detail_admin", quiz_id=quiz_id))


@app.route("/admin/quizzes/<int:quiz_id>/results")
def quiz_results(quiz_id):
    resp = require_permission("quizzes_results_view")
    if resp:
        return resp

    db = get_db()
    quiz = db.execute("SELECT * FROM quizzes WHERE id = ?", (quiz_id,)).fetchone()
    if quiz is None:
        flash("Quiz not found.", "error")
        return redirect(url_for("admin_quizzes"))

    attempts = db.execute(
        """SELECT quiz_attempts.*, employees.name AS employee_name
           FROM quiz_attempts
           JOIN employees ON employees.id = quiz_attempts.employee_id
           WHERE quiz_attempts.quiz_id = ?
           ORDER BY quiz_attempts.submitted_at DESC""",
        (quiz_id,),
    ).fetchall()
    not_attempted = db.execute(
        """SELECT * FROM employees WHERE role = 'Employee' AND id NOT IN (
             SELECT employee_id FROM quiz_attempts WHERE quiz_id = ?
           )""",
        (quiz_id,),
    ).fetchall()

    return render_template(
        "quiz_results.html", quiz=quiz, attempts=attempts, not_attempted=not_attempted
    )


@app.route("/admin/quizzes/attempts/<int:attempt_id>")
def quiz_attempt_detail(attempt_id):
    resp = require_permission("quizzes_results_view")
    if resp:
        return resp

    db = get_db()
    attempt = db.execute(
        """SELECT quiz_attempts.*, employees.name AS employee_name
           FROM quiz_attempts JOIN employees ON employees.id = quiz_attempts.employee_id
           WHERE quiz_attempts.id = ?""",
        (attempt_id,),
    ).fetchone()
    if attempt is None:
        flash("Attempt not found.", "error")
        return redirect(url_for("admin_quizzes"))

    quiz = db.execute("SELECT * FROM quizzes WHERE id = ?", (attempt["quiz_id"],)).fetchone()
    questions = db.execute(
        "SELECT * FROM quiz_questions WHERE quiz_id = ? ORDER BY sort_order, id", (quiz["id"],)
    ).fetchall()

    review_rows = []
    for question in questions:
        choices = db.execute(
            "SELECT * FROM quiz_choices WHERE question_id = ? ORDER BY sort_order, id",
            (question["id"],),
        ).fetchall()
        answers = db.execute(
            "SELECT * FROM quiz_attempt_answers WHERE attempt_id = ? AND question_id = ?",
            (attempt_id, question["id"]),
        ).fetchall()
        qtype = question["question_type"]

        if qtype == "text":
            text_given = answers[0]["text_answer"] if answers else ""
            question_correct = bool(answers) and bool(answers[0]["is_correct"])
            row = {
                "question": question, "choices": choices, "selected_choice_ids": set(),
                "text_given": text_given, "question_correct": question_correct,
                "matching_selections": {},
            }
        elif qtype == "matching":
            selections = {a["choice_id"]: a["text_answer"] for a in answers if a["choice_id"] is not None}
            question_correct = bool(choices) and all(
                selections.get(c["id"]) == c["match_text"] for c in choices
            )
            row = {
                "question": question, "choices": choices, "selected_choice_ids": set(),
                "text_given": None, "question_correct": question_correct,
                "matching_selections": selections,
            }
        elif qtype == "multi_choice":
            selected_ids = {a["choice_id"] for a in answers if a["choice_id"] is not None}
            correct_ids = {c["id"] for c in choices if c["is_correct"]}
            question_correct = bool(selected_ids) and selected_ids == correct_ids
            row = {
                "question": question, "choices": choices, "selected_choice_ids": selected_ids,
                "text_given": None, "question_correct": question_correct,
                "matching_selections": {},
            }
        else:  # single_choice
            selected_ids = {a["choice_id"] for a in answers if a["choice_id"] is not None}
            question_correct = bool(answers) and bool(answers[0]["is_correct"])
            row = {
                "question": question, "choices": choices, "selected_choice_ids": selected_ids,
                "text_given": None, "question_correct": question_correct,
                "matching_selections": {},
            }

        review_rows.append(row)

    return render_template(
        "quiz_attempt_detail.html", quiz=quiz, attempt=attempt, review_rows=review_rows
    )


@app.route("/admin/quizzes/attempts/<int:attempt_id>/edit", methods=["POST"])
def edit_quiz_attempt(attempt_id):
    resp = require_permission("quizzes_results_edit")
    if resp:
        return resp

    db = get_db()
    attempt = db.execute("SELECT * FROM quiz_attempts WHERE id = ?", (attempt_id,)).fetchone()
    if attempt is None:
        flash("Attempt not found.", "error")
        return redirect(url_for("admin_quizzes"))

    try:
        total = max(1, int(request.form.get("total", attempt["total"])))
        score = max(0, min(total, int(request.form.get("score", attempt["score"]))))
    except ValueError:
        flash("Score and total must be numbers.", "error")
        return redirect(url_for("quiz_attempt_detail", attempt_id=attempt_id))
    passed = 1 if request.form.get("passed") == "on" else 0

    db.execute(
        "UPDATE quiz_attempts SET score = ?, total = ?, passed = ? WHERE id = ?",
        (score, total, passed, attempt_id),
    )
    sync_quiz_checklist_step(db, attempt["employee_id"], attempt["quiz_id"])
    db.commit()
    flash("Attempt updated.", "success")
    return redirect(url_for("quiz_attempt_detail", attempt_id=attempt_id))


@app.route("/admin/quizzes/attempts/<int:attempt_id>/delete", methods=["POST"])
def delete_quiz_attempt(attempt_id):
    resp = require_permission("quizzes_results_edit")
    if resp:
        return resp

    db = get_db()
    attempt = db.execute("SELECT * FROM quiz_attempts WHERE id = ?", (attempt_id,)).fetchone()
    if attempt is None:
        flash("Attempt not found.", "error")
        return redirect(url_for("admin_quizzes"))

    quiz_id = attempt["quiz_id"]
    employee_id = attempt["employee_id"]
    db.execute("DELETE FROM quiz_attempt_answers WHERE attempt_id = ?", (attempt_id,))
    db.execute("DELETE FROM quiz_attempts WHERE id = ?", (attempt_id,))
    sync_quiz_checklist_step(db, employee_id, quiz_id)
    db.commit()
    flash("Attempt deleted.", "success")
    return redirect(url_for("quiz_results", quiz_id=quiz_id))


@app.route("/employee/quizzes")
def employee_quizzes():
    resp = require_login()
    if resp:
        return resp

    db = get_db()
    quizzes = db.execute("SELECT * FROM quizzes ORDER BY created_at DESC").fetchall()
    quiz_rows = []
    for quiz in quizzes:
        question_count = db.execute(
            "SELECT COUNT(*) FROM quiz_questions WHERE quiz_id = ?", (quiz["id"],)
        ).fetchone()[0]
        best_attempt = db.execute(
            """SELECT * FROM quiz_attempts WHERE quiz_id = ? AND employee_id = ?
               ORDER BY score DESC, submitted_at DESC LIMIT 1""",
            (quiz["id"], session["user_id"]),
        ).fetchone()
        locked = is_quiz_locked_for(db, quiz["id"], session["user_id"])
        quiz_rows.append(
            {
                "quiz": quiz,
                "question_count": question_count,
                "best_attempt": best_attempt,
                "locked": locked,
            }
        )

    return render_template("employee_quizzes.html", quiz_rows=quiz_rows)


@app.route("/employee/quizzes/<int:quiz_id>/take")
def take_quiz(quiz_id):
    resp = require_login()
    if resp:
        return resp

    db = get_db()
    quiz = db.execute("SELECT * FROM quizzes WHERE id = ?", (quiz_id,)).fetchone()
    if quiz is None:
        flash("Quiz not found.", "error")
        return redirect(url_for("employee_quizzes"))
    if is_quiz_locked_for(db, quiz_id, session["user_id"]) and not has_permission(
        db, session.get("role"), "manage_quizzes"
    ):
        flash("This quiz is locked for you right now. Check back once it's opened up.", "error")
        return redirect(url_for("employee_quizzes"))

    questions = db.execute(
        "SELECT * FROM quiz_questions WHERE quiz_id = ? ORDER BY sort_order, id", (quiz_id,)
    ).fetchall()
    question_rows = []
    for question in questions:
        choices = db.execute(
            "SELECT * FROM quiz_choices WHERE question_id = ? ORDER BY sort_order, id",
            (question["id"],),
        ).fetchall()
        row = {"question": question, "choices": choices}
        if question["question_type"] == "matching":
            match_options = [c["match_text"] for c in choices]
            random.shuffle(match_options)
            row["match_options"] = match_options
        question_rows.append(row)

    if not question_rows:
        flash("This quiz doesn't have any questions yet.", "error")
        return redirect(url_for("employee_quizzes"))

    return render_template("take_quiz.html", quiz=quiz, question_rows=question_rows)


@app.route("/employee/quizzes/<int:quiz_id>/submit", methods=["POST"])
def submit_quiz(quiz_id):
    resp = require_login()
    if resp:
        return resp

    db = get_db()
    quiz = db.execute("SELECT * FROM quizzes WHERE id = ?", (quiz_id,)).fetchone()
    if quiz is None:
        flash("Quiz not found.", "error")
        return redirect(url_for("employee_quizzes"))
    if is_quiz_locked_for(db, quiz_id, session["user_id"]) and not has_permission(
        db, session.get("role"), "manage_quizzes"
    ):
        flash("This quiz is locked for you right now. Check back once it's opened up.", "error")
        return redirect(url_for("employee_quizzes"))

    questions = db.execute(
        "SELECT * FROM quiz_questions WHERE quiz_id = ? ORDER BY sort_order, id", (quiz_id,)
    ).fetchall()

    score = 0
    graded = []  # per-question: question, choices, selected_choice_ids, text_given, question_correct
    for question in questions:
        qtype = question["question_type"]
        choices = db.execute(
            "SELECT * FROM quiz_choices WHERE question_id = ? ORDER BY sort_order, id",
            (question["id"],),
        ).fetchall()

        if qtype == "text":
            given = request.form.get(f"question_{question['id']}", "").strip()
            expected = (question["text_answer"] or "").strip()
            is_correct = bool(expected) and given.lower() == expected.lower()
            if is_correct:
                score += 1
            graded.append(
                {
                    "question": question,
                    "choices": choices,
                    "selected_choice_ids": set(),
                    "text_given": given,
                    "question_correct": is_correct,
                    "rows_to_insert": [
                        {"choice_id": None, "text_answer": given, "is_correct": 1 if is_correct else 0}
                    ],
                }
            )
        elif qtype == "matching":
            selections = {}
            rows_to_insert = []
            all_correct = bool(choices)
            for choice in choices:
                given = request.form.get(f"question_{question['id']}_choice_{choice['id']}", "").strip()
                selections[choice["id"]] = given
                is_pair_correct = bool(given) and given == (choice["match_text"] or "")
                if not is_pair_correct:
                    all_correct = False
                rows_to_insert.append(
                    {
                        "choice_id": choice["id"],
                        "text_answer": given,
                        "is_correct": 1 if is_pair_correct else 0,
                    }
                )
            if all_correct:
                score += 1
            graded.append(
                {
                    "question": question,
                    "choices": choices,
                    "selected_choice_ids": set(),
                    "text_given": None,
                    "question_correct": all_correct,
                    "rows_to_insert": rows_to_insert,
                    "matching_selections": selections,
                }
            )
        elif qtype == "multi_choice":
            selected_ids = set(request.form.getlist(f"question_{question['id']}"))
            correct_ids = {str(c["id"]) for c in choices if c["is_correct"]}
            is_correct = bool(selected_ids) and selected_ids == correct_ids
            if is_correct:
                score += 1
            rows_to_insert = []
            if selected_ids:
                for cid in selected_ids:
                    choice = next((c for c in choices if str(c["id"]) == cid), None)
                    if choice:
                        rows_to_insert.append(
                            {
                                "choice_id": choice["id"],
                                "text_answer": None,
                                "is_correct": 1 if choice["is_correct"] else 0,
                            }
                        )
            else:
                rows_to_insert.append({"choice_id": None, "text_answer": None, "is_correct": 0})
            graded.append(
                {
                    "question": question,
                    "choices": choices,
                    "selected_choice_ids": {int(cid) for cid in selected_ids if cid.isdigit()},
                    "text_given": None,
                    "question_correct": is_correct,
                    "rows_to_insert": rows_to_insert,
                }
            )
        else:  # single_choice
            selected_choice_id = request.form.get(f"question_{question['id']}")
            is_correct = False
            choice_id = None
            if selected_choice_id:
                choice = db.execute(
                    "SELECT * FROM quiz_choices WHERE id = ? AND question_id = ?",
                    (selected_choice_id, question["id"]),
                ).fetchone()
                if choice:
                    choice_id = choice["id"]
                    is_correct = bool(choice["is_correct"])
            if is_correct:
                score += 1
            graded.append(
                {
                    "question": question,
                    "choices": choices,
                    "selected_choice_ids": {choice_id} if choice_id else set(),
                    "text_given": None,
                    "question_correct": is_correct,
                    "rows_to_insert": [
                        {"choice_id": choice_id, "text_answer": None, "is_correct": 1 if is_correct else 0}
                    ],
                }
            )

    total = len(questions)
    pct = round((score / total) * 100) if total else 0
    passed = 1 if pct >= quiz["passing_score"] else 0

    cur = db.execute(
        """INSERT INTO quiz_attempts (quiz_id, employee_id, score, total, passed)
           VALUES (?, ?, ?, ?, ?)""",
        (quiz_id, session["user_id"], score, total, passed),
    )
    attempt_id = cur.lastrowid
    for row in graded:
        for insert_row in row["rows_to_insert"]:
            db.execute(
                """INSERT INTO quiz_attempt_answers (attempt_id, question_id, choice_id, text_answer, is_correct)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    attempt_id,
                    row["question"]["id"],
                    insert_row["choice_id"],
                    insert_row["text_answer"],
                    insert_row["is_correct"],
                ),
            )

    if passed:
        mark_quiz_step_complete(db, session["user_id"], quiz_id)

    db.commit()

    return render_template(
        "quiz_result.html", quiz=quiz, score=score, total=total, pct=pct, passed=passed,
        review_rows=graded,
    )


# ---------------------------------------------------------------------------
# Master onboarding checklist (baseline tasks every employee gets, regardless
# of which job-specific onboarding template they're assigned)
# ---------------------------------------------------------------------------

@app.route("/admin/settings/master-checklist", methods=["GET", "POST"])
def master_checklist_settings():
    resp = require_permission("checklists_master")
    if resp:
        return resp

    db = get_db()

    if request.method == "POST":
        step_name = request.form.get("step_name", "").strip()
        if not step_name:
            flash("Please describe the task.", "error")
            return redirect(url_for("master_checklist_settings"))

        max_order = db.execute(
            "SELECT COALESCE(MAX(sort_order), -1) FROM master_checklist_items"
        ).fetchone()[0]
        db.execute(
            "INSERT INTO master_checklist_items (step_name, sort_order) VALUES (?, ?)",
            (step_name, max_order + 1),
        )

        # Broadcast to every current employee too, same as adding a
        # signature-required document or an onboarding training module does —
        # otherwise this would only ever affect employees added from now on.
        employees = db.execute("SELECT id FROM employees WHERE role = 'Employee'").fetchall()
        for emp in employees:
            already = db.execute(
                """SELECT 1 FROM onboarding_steps
                   WHERE employee_id = ? AND step_type = 'task' AND step_name = ?""",
                (emp["id"], step_name),
            ).fetchone()
            if already:
                continue
            db.execute(
                """INSERT INTO onboarding_steps (employee_id, step_name, step_type, related_id)
                   VALUES (?, ?, 'task', NULL)""",
                (emp["id"], step_name),
            )

        db.commit()
        flash("Added to the master checklist and every employee's list.", "success")
        return redirect(url_for("master_checklist_settings"))

    items = db.execute("SELECT * FROM master_checklist_items ORDER BY sort_order, id").fetchall()
    return render_template("master_checklist.html", items=items)


@app.route("/admin/settings/master-checklist/<int:item_id>/edit", methods=["POST"])
def edit_master_checklist_item(item_id):
    resp = require_permission("checklists_master")
    if resp:
        return resp

    db = get_db()
    item = db.execute("SELECT * FROM master_checklist_items WHERE id = ?", (item_id,)).fetchone()
    if item is None:
        flash("Checklist item not found.", "error")
        return redirect(url_for("master_checklist_settings"))

    step_name = request.form.get("step_name", "").strip()
    if not step_name:
        flash("Please describe the task.", "error")
        return redirect(url_for("master_checklist_settings"))

    old_name = item["step_name"]
    db.execute("UPDATE master_checklist_items SET step_name = ? WHERE id = ?", (step_name, item_id))
    # Keep every employee's already-broadcast step in sync with the rename —
    # delete matches on step_name, so this also keeps that working correctly.
    db.execute(
        "UPDATE onboarding_steps SET step_name = ? WHERE step_type = 'task' AND step_name = ?",
        (step_name, old_name),
    )
    db.commit()
    flash("Master checklist item updated (including on every employee's checklist).", "success")
    return redirect(url_for("master_checklist_settings"))


@app.route("/admin/settings/master-checklist/<int:item_id>/delete", methods=["POST"])
def delete_master_checklist_item(item_id):
    resp = require_permission("checklists_master")
    if resp:
        return resp

    db = get_db()
    item = db.execute("SELECT * FROM master_checklist_items WHERE id = ?", (item_id,)).fetchone()
    if item is None:
        flash("Checklist item not found.", "error")
        return redirect(url_for("master_checklist_settings"))

    db.execute("DELETE FROM master_checklist_items WHERE id = ?", (item_id,))
    # Also pull this task off of everyone's checklist, completed or not —
    # otherwise it lingers forever with no way to get rid of it.
    db.execute(
        "DELETE FROM onboarding_steps WHERE step_type = 'task' AND related_id IS NULL AND step_name = ?",
        (item["step_name"],),
    )
    db.commit()
    flash(f'"{item["step_name"]}" removed from the master checklist and every employee\'s list.', "success")
    return redirect(url_for("master_checklist_settings"))


@app.route("/admin/settings/step-order", methods=["GET", "POST"])
def step_order_settings():
    resp = require_permission("checklists_order")
    if resp:
        return resp

    db = get_db()

    if request.method == "POST":
        ranked = []
        for step_type in DEFAULT_STEP_TYPE_PRIORITY:
            try:
                rank = int(request.form.get(f"rank_{step_type}", 999))
            except ValueError:
                rank = 999
            ranked.append((rank, DEFAULT_STEP_TYPE_PRIORITY.index(step_type), step_type))
        ranked.sort(key=lambda x: (x[0], x[1]))
        order = [step_type for _, _, step_type in ranked]

        db.execute(
            """INSERT INTO portal_settings (key, value) VALUES ('step_type_priority', ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
            (json.dumps(order),),
        )
        db.commit()
        flash("Checklist order updated.", "success")
        return redirect(url_for("step_order_settings"))

    current_order = get_step_type_priority(db)
    return render_template(
        "step_order_settings.html", current_order=current_order, labels=STEP_TYPE_LABELS
    )


# ---------------------------------------------------------------------------
# Settings / onboarding checklist templates (job-specific onboarding)
# ---------------------------------------------------------------------------

@app.route("/admin/settings")
def settings_home():
    resp = require_admin_or_manager()
    if resp:
        return resp
    return render_template("settings.html")


def render_role_permissions_page(role_name):
    """Shared permissions editor for Manager, Employee, and every custom
    access level — same UI, different role name. Admin is never edited here
    since it always has full access."""
    db = get_db()

    if request.method == "POST":
        selected = {
            key for key in request.form.getlist("permissions") if key in PERMISSION_KEYS
        }
        db.execute("DELETE FROM role_permissions WHERE role = ?", (role_name,))
        for key in selected:
            db.execute(
                "INSERT INTO role_permissions (role, permission) VALUES (?, ?)", (role_name, key)
            )
        db.commit()
        flash(f"{role_name} permissions updated.", "success")
        return redirect(url_for(request.endpoint, **request.view_args))

    granted_permissions = {
        row["permission"]
        for row in db.execute(
            "SELECT permission FROM role_permissions WHERE role = ?", (role_name,)
        ).fetchall()
    }

    return render_template(
        "role_permissions.html",
        role_name=role_name,
        permission_categories=PERMISSION_CATEGORIES,
        manager_permissions=granted_permissions,
    )


@app.route("/admin/settings/permissions/<role_name>", methods=["GET", "POST"])
def edit_role_permissions(role_name):
    resp = require_admin()
    if resp:
        return resp

    db = get_db()
    if role_name == "Admin" or role_name not in get_all_roles(db):
        flash("That role can't be edited here.", "error")
        return redirect(url_for("access_levels_list"))

    return render_role_permissions_page(role_name)


@app.route("/admin/settings/role-permissions")
def role_permissions_settings():
    # Kept so any old bookmarks/links still work — Manager now lives in the
    # same unified Access Levels list as everything else.
    resp = require_admin()
    if resp:
        return resp
    return redirect(url_for("edit_role_permissions", role_name="Manager"))


@app.route("/admin/settings/access-levels")
def access_levels_list():
    resp = require_admin()
    if resp:
        return resp

    db = get_db()

    def role_summary(name, description, built_in):
        return {
            "name": name,
            "description": description,
            "built_in": built_in,
            "employee_count": db.execute(
                "SELECT COUNT(*) FROM employees WHERE role = ?", (name,)
            ).fetchone()[0],
            "permission_count": db.execute(
                "SELECT COUNT(*) FROM role_permissions WHERE role = ?", (name,)
            ).fetchone()[0],
        }

    built_in_rows = [
        role_summary(
            "Manager",
            "Sees the full admin portal, scoped to whatever's granted below.",
            True,
        ),
        role_summary(
            "Employee",
            "The standard employee experience by default — grant permissions here to also give employees specific admin-side capabilities.",
            True,
        ),
    ]

    custom_rows = [
        role_summary(role["name"], role["description"], False)
        for role in db.execute("SELECT * FROM custom_roles ORDER BY name").fetchall()
    ]

    return render_template(
        "access_levels.html", built_in_rows=built_in_rows, custom_rows=custom_rows
    )


@app.route("/admin/settings/access-levels/create", methods=["POST"])
def create_access_level():
    resp = require_admin()
    if resp:
        return resp

    db = get_db()
    name = request.form.get("name", "").strip()
    description = request.form.get("description", "").strip()

    if not name:
        flash("Name is required.", "error")
        return redirect(url_for("access_levels_list"))
    if name in ("Admin", "Manager", "Employee"):
        flash(f'"{name}" is already a built-in role — choose a different name.', "error")
        return redirect(url_for("access_levels_list"))

    existing = db.execute("SELECT 1 FROM custom_roles WHERE name = ?", (name,)).fetchone()
    if existing:
        flash("An access level with that name already exists.", "error")
        return redirect(url_for("access_levels_list"))

    db.execute(
        "INSERT INTO custom_roles (name, description) VALUES (?, ?)", (name, description)
    )
    db.commit()
    flash(f'"{name}" created. Set its permissions below.', "success")
    return redirect(url_for("edit_role_permissions", role_name=name))


@app.route("/admin/settings/access-levels/<role_name>/delete", methods=["POST"])
def delete_access_level(role_name):
    resp = require_admin()
    if resp:
        return resp

    confirm = request.form.get("confirm", "").strip().lower()
    if confirm != "delete":
        flash('You must type "delete" to confirm.', "error")
        return redirect(url_for("access_levels_list"))

    db = get_db()
    role = db.execute("SELECT * FROM custom_roles WHERE name = ?", (role_name,)).fetchone()
    if role is None:
        flash("Access level not found.", "error")
        return redirect(url_for("access_levels_list"))

    # Anyone currently on this access level falls back to the plain Employee
    # role rather than being left with an undefined/dangling role.
    reassigned = db.execute(
        "SELECT COUNT(*) FROM employees WHERE role = ?", (role_name,)
    ).fetchone()[0]
    db.execute("UPDATE employees SET role = 'Employee' WHERE role = ?", (role_name,))
    db.execute("DELETE FROM role_permissions WHERE role = ?", (role_name,))
    db.execute("DELETE FROM custom_roles WHERE id = ?", (role["id"],))
    db.commit()

    note = f" {reassigned} employee(s) were moved to the Employee role." if reassigned else ""
    flash(f'"{role_name}" deleted.{note}', "success")
    return redirect(url_for("access_levels_list"))


@app.route("/admin/settings/signup-page", methods=["GET", "POST"])
def onboarding_page_settings():
    resp = require_permission("settings_signup_page")
    if resp:
        return resp

    db = get_db()

    if request.method == "POST":
        heading = request.form.get("heading", "").strip() or DEFAULT_ONBOARDING_HEADING
        message = request.form.get("message", "").strip() or DEFAULT_ONBOARDING_MESSAGE
        button_text = request.form.get("button_text", "").strip() or DEFAULT_ONBOARDING_BUTTON

        set_setting(db, "onboarding_page_heading", heading)
        set_setting(db, "onboarding_page_message", message)
        set_setting(db, "onboarding_page_button_text", button_text)
        db.commit()

        flash("Signup page updated.", "success")
        return redirect(url_for("onboarding_page_settings"))

    heading = get_setting(db, "onboarding_page_heading", DEFAULT_ONBOARDING_HEADING)
    message = get_setting(db, "onboarding_page_message", DEFAULT_ONBOARDING_MESSAGE)
    button_text = get_setting(db, "onboarding_page_button_text", DEFAULT_ONBOARDING_BUTTON)

    return render_template(
        "onboarding_page_settings.html", heading=heading, message=message, button_text=button_text
    )


@app.route("/admin/settings/onboarding-templates")
def onboarding_templates_list():
    resp = require_admin_or_manager()
    if resp:
        return resp

    db = get_db()
    templates = db.execute(
        "SELECT * FROM onboarding_templates ORDER BY name"
    ).fetchall()
    template_rows = []
    for t in templates:
        item_count = db.execute(
            "SELECT COUNT(*) FROM onboarding_template_items WHERE template_id = ?", (t["id"],)
        ).fetchone()[0]
        employee_count = db.execute(
            "SELECT COUNT(*) FROM employees WHERE onboarding_template_id = ?", (t["id"],)
        ).fetchone()[0]
        template_rows.append({"template": t, "item_count": item_count, "employee_count": employee_count})

    return render_template("onboarding_templates_list.html", template_rows=template_rows)


@app.route("/admin/settings/onboarding-templates/create", methods=["GET", "POST"])
def add_onboarding_template():
    resp = require_permission("checklists_templates")
    if resp:
        return resp

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        description = request.form.get("description", "").strip()

        if not name:
            flash("Please give the checklist a name.", "error")
            return render_template("add_onboarding_template.html")

        db = get_db()
        cur = db.execute(
            "INSERT INTO onboarding_templates (name, description) VALUES (?, ?)",
            (name, description),
        )
        db.commit()
        flash("Onboarding checklist created.", "success")
        return redirect(url_for("onboarding_template_detail", template_id=cur.lastrowid))

    return render_template("add_onboarding_template.html")


@app.route("/admin/settings/onboarding-templates/<int:template_id>")
def onboarding_template_detail(template_id):
    resp = require_admin_or_manager()
    if resp:
        return resp

    db = get_db()
    template = db.execute(
        "SELECT * FROM onboarding_templates WHERE id = ?", (template_id,)
    ).fetchone()
    if template is None:
        flash("Onboarding checklist not found.", "error")
        return redirect(url_for("onboarding_templates_list"))

    raw_items = db.execute(
        "SELECT * FROM onboarding_template_items WHERE template_id = ? ORDER BY sort_order, id",
        (template_id,),
    ).fetchall()
    items = []
    for item in raw_items:
        item_dict = dict(item)
        item_dict["step_name"] = resolve_checklist_display_name(
            db, item["step_type"], item["related_id"], item["step_name"]
        )
        items.append(item_dict)
    documents = db.execute("SELECT * FROM documents ORDER BY title").fetchall()
    modules = db.execute("SELECT * FROM training_modules ORDER BY title").fetchall()
    quizzes = db.execute("SELECT * FROM quizzes ORDER BY title").fetchall()
    employees_using = db.execute(
        "SELECT * FROM employees WHERE onboarding_template_id = ? ORDER BY name", (template_id,)
    ).fetchall()

    return render_template(
        "onboarding_template_detail.html",
        template=template,
        items=items,
        documents=documents,
        modules=modules,
        quizzes=quizzes,
        employees_using=employees_using,
    )


@app.route("/admin/settings/onboarding-templates/<int:template_id>/edit", methods=["POST"])
def edit_onboarding_template(template_id):
    resp = require_permission("checklists_templates")
    if resp:
        return resp

    db = get_db()
    template = db.execute(
        "SELECT id FROM onboarding_templates WHERE id = ?", (template_id,)
    ).fetchone()
    if template is None:
        flash("Onboarding checklist not found.", "error")
        return redirect(url_for("onboarding_templates_list"))

    name = request.form.get("name", "").strip()
    description = request.form.get("description", "").strip()
    if not name:
        flash("Name is required.", "error")
        return redirect(url_for("onboarding_template_detail", template_id=template_id))

    db.execute(
        "UPDATE onboarding_templates SET name = ?, description = ? WHERE id = ?",
        (name, description, template_id),
    )
    db.commit()
    flash("Checklist updated.", "success")
    return redirect(url_for("onboarding_template_detail", template_id=template_id))


@app.route("/admin/settings/onboarding-templates/<int:template_id>/items/task", methods=["POST"])
def add_template_task_item(template_id):
    resp = require_permission("checklists_items")
    if resp:
        return resp

    db = get_db()
    template = db.execute(
        "SELECT id FROM onboarding_templates WHERE id = ?", (template_id,)
    ).fetchone()
    if template is None:
        flash("Onboarding checklist not found.", "error")
        return redirect(url_for("onboarding_templates_list"))

    step_name = request.form.get("step_name", "").strip()
    if not step_name:
        flash("Please describe the task.", "error")
        return redirect(url_for("onboarding_template_detail", template_id=template_id))

    max_order = db.execute(
        "SELECT COALESCE(MAX(sort_order), -1) FROM onboarding_template_items WHERE template_id = ?",
        (template_id,),
    ).fetchone()[0]
    db.execute(
        """INSERT INTO onboarding_template_items (template_id, step_name, step_type, related_id, sort_order)
           VALUES (?, ?, 'task', NULL, ?)""",
        (template_id, step_name, max_order + 1),
    )
    db.commit()
    flash("Task added to checklist.", "success")
    return redirect(url_for("onboarding_template_detail", template_id=template_id))


@app.route("/admin/settings/onboarding-templates/<int:template_id>/items/document", methods=["POST"])
def add_template_document_item(template_id):
    resp = require_permission("checklists_items")
    if resp:
        return resp

    db = get_db()
    template = db.execute(
        "SELECT id FROM onboarding_templates WHERE id = ?", (template_id,)
    ).fetchone()
    if template is None:
        flash("Onboarding checklist not found.", "error")
        return redirect(url_for("onboarding_templates_list"))

    document_ids = request.form.getlist("document_ids")
    if not document_ids:
        flash("Please choose at least one document.", "error")
        return redirect(url_for("onboarding_template_detail", template_id=template_id))

    existing = {
        row["related_id"]
        for row in db.execute(
            """SELECT related_id FROM onboarding_template_items
               WHERE template_id = ? AND step_type IN ('document', 'upload')""",
            (template_id,),
        ).fetchall()
    }
    max_order = db.execute(
        "SELECT COALESCE(MAX(sort_order), -1) FROM onboarding_template_items WHERE template_id = ?",
        (template_id,),
    ).fetchone()[0]

    added = 0
    for document_id in document_ids:
        document = db.execute("SELECT * FROM documents WHERE id = ?", (document_id,)).fetchone()
        if document is None or document["id"] in existing:
            continue
        step_type = "upload" if document["requires_upload"] else "document"
        verb = "Upload" if document["requires_upload"] else "Sign"
        max_order += 1
        db.execute(
            """INSERT INTO onboarding_template_items (template_id, step_name, step_type, related_id, sort_order)
               VALUES (?, ?, ?, ?, ?)""",
            (template_id, f"{verb} {document['title']}", step_type, document["id"], max_order),
        )
        existing.add(document["id"])
        added += 1

    if added == 0:
        flash("Those documents are already on this checklist.", "error")
        return redirect(url_for("onboarding_template_detail", template_id=template_id))

    db.commit()
    flash(f"{added} document(s) added to checklist.", "success")
    return redirect(url_for("onboarding_template_detail", template_id=template_id))


@app.route("/admin/settings/onboarding-templates/<int:template_id>/items/training", methods=["POST"])
def add_template_training_item(template_id):
    resp = require_permission("checklists_items")
    if resp:
        return resp

    db = get_db()
    template = db.execute(
        "SELECT id FROM onboarding_templates WHERE id = ?", (template_id,)
    ).fetchone()
    if template is None:
        flash("Onboarding checklist not found.", "error")
        return redirect(url_for("onboarding_templates_list"))

    module_ids = request.form.getlist("module_ids")
    if not module_ids:
        flash("Please choose at least one training module.", "error")
        return redirect(url_for("onboarding_template_detail", template_id=template_id))

    existing = {
        row["related_id"]
        for row in db.execute(
            """SELECT related_id FROM onboarding_template_items
               WHERE template_id = ? AND step_type = 'training'""",
            (template_id,),
        ).fetchall()
    }
    max_order = db.execute(
        "SELECT COALESCE(MAX(sort_order), -1) FROM onboarding_template_items WHERE template_id = ?",
        (template_id,),
    ).fetchone()[0]

    added = 0
    for module_id in module_ids:
        module = db.execute("SELECT * FROM training_modules WHERE id = ?", (module_id,)).fetchone()
        if module is None or module["id"] in existing:
            continue
        max_order += 1
        db.execute(
            """INSERT INTO onboarding_template_items (template_id, step_name, step_type, related_id, sort_order)
               VALUES (?, ?, 'training', ?, ?)""",
            (template_id, f"Complete {module['title']}", module["id"], max_order),
        )
        existing.add(module["id"])
        added += 1

    if added == 0:
        flash("Those training modules are already on this checklist.", "error")
        return redirect(url_for("onboarding_template_detail", template_id=template_id))

    db.commit()
    flash(f"{added} training module(s) added to checklist.", "success")
    return redirect(url_for("onboarding_template_detail", template_id=template_id))


@app.route("/admin/settings/onboarding-templates/<int:template_id>/items/quiz", methods=["POST"])
def add_template_quiz_item(template_id):
    resp = require_permission("checklists_items")
    if resp:
        return resp

    db = get_db()
    template = db.execute(
        "SELECT id FROM onboarding_templates WHERE id = ?", (template_id,)
    ).fetchone()
    if template is None:
        flash("Onboarding checklist not found.", "error")
        return redirect(url_for("onboarding_templates_list"))

    quiz_ids = request.form.getlist("quiz_ids")
    if not quiz_ids:
        flash("Please choose at least one quiz.", "error")
        return redirect(url_for("onboarding_template_detail", template_id=template_id))

    existing = {
        row["related_id"]
        for row in db.execute(
            """SELECT related_id FROM onboarding_template_items
               WHERE template_id = ? AND step_type = 'quiz'""",
            (template_id,),
        ).fetchall()
    }
    max_order = db.execute(
        "SELECT COALESCE(MAX(sort_order), -1) FROM onboarding_template_items WHERE template_id = ?",
        (template_id,),
    ).fetchone()[0]

    added = 0
    for quiz_id in quiz_ids:
        quiz = db.execute("SELECT * FROM quizzes WHERE id = ?", (quiz_id,)).fetchone()
        if quiz is None or quiz["id"] in existing:
            continue
        max_order += 1
        db.execute(
            """INSERT INTO onboarding_template_items (template_id, step_name, step_type, related_id, sort_order)
               VALUES (?, ?, 'quiz', ?, ?)""",
            (template_id, f"Take Quiz: {quiz['title']}", quiz["id"], max_order),
        )
        existing.add(quiz["id"])
        added += 1

    if added == 0:
        flash("Those quizzes are already on this checklist.", "error")
        return redirect(url_for("onboarding_template_detail", template_id=template_id))

    db.commit()
    flash(f"{added} quiz(zes) added to checklist.", "success")
    return redirect(url_for("onboarding_template_detail", template_id=template_id))


@app.route("/admin/settings/onboarding-templates/items/<int:item_id>/edit", methods=["POST"])
def edit_template_task_item(item_id):
    resp = require_permission("checklists_items")
    if resp:
        return resp

    db = get_db()
    item = db.execute(
        "SELECT * FROM onboarding_template_items WHERE id = ?", (item_id,)
    ).fetchone()
    if item is None:
        flash("Item not found.", "error")
        return redirect(url_for("onboarding_templates_list"))

    if item["step_type"] != "task":
        flash("Only tasks can be edited. Remove and re-add documents or training instead.", "error")
        return redirect(url_for("onboarding_template_detail", template_id=item["template_id"]))

    step_name = request.form.get("step_name", "").strip()
    if not step_name:
        flash("Please describe the task.", "error")
        return redirect(url_for("onboarding_template_detail", template_id=item["template_id"]))

    old_name = item["step_name"]
    db.execute(
        "UPDATE onboarding_template_items SET step_name = ? WHERE id = ?",
        (step_name, item_id),
    )
    # Keep already-assigned employees' checklists in sync with the rename,
    # same as editing a master checklist task does.
    db.execute(
        "UPDATE onboarding_steps SET step_name = ? WHERE step_type = 'task' AND step_name = ?",
        (step_name, old_name),
    )
    db.commit()
    flash("Task updated (including on every employee's checklist).", "success")
    return redirect(url_for("onboarding_template_detail", template_id=item["template_id"]))


@app.route("/admin/settings/onboarding-templates/items/<int:item_id>/delete", methods=["POST"])
def delete_template_item(item_id):
    resp = require_permission("checklists_items")
    if resp:
        return resp

    db = get_db()
    item = db.execute(
        "SELECT * FROM onboarding_template_items WHERE id = ?", (item_id,)
    ).fetchone()
    if item is None:
        flash("Item not found.", "error")
        return redirect(url_for("onboarding_templates_list"))

    template_id = item["template_id"]
    db.execute("DELETE FROM onboarding_template_items WHERE id = ?", (item_id,))
    db.commit()
    flash("Removed from checklist.", "success")
    return redirect(url_for("onboarding_template_detail", template_id=template_id))


@app.route("/admin/settings/onboarding-templates/<int:template_id>/delete", methods=["POST"])
def delete_onboarding_template(template_id):
    resp = require_permission("checklists_templates")
    if resp:
        return resp

    confirm = request.form.get("confirm", "").strip().lower()
    if confirm != "delete":
        flash('You must type "delete" to confirm.', "error")
        return redirect(url_for("onboarding_template_detail", template_id=template_id))

    db = get_db()
    db.execute(
        "UPDATE employees SET onboarding_template_id = NULL WHERE onboarding_template_id = ?",
        (template_id,),
    )
    db.execute("DELETE FROM onboarding_template_items WHERE template_id = ?", (template_id,))
    db.execute("DELETE FROM onboarding_templates WHERE id = ?", (template_id,))
    db.commit()

    flash("Onboarding checklist deleted.", "success")
    return redirect(url_for("onboarding_templates_list"))


@app.route("/admin/training/<int:module_id>/checklists", methods=["POST"])
def update_module_checklists(module_id):
    resp = require_permission("checklists_items")
    if resp:
        return resp

    db = get_db()
    module = db.execute("SELECT * FROM training_modules WHERE id = ?", (module_id,)).fetchone()
    if module is None:
        flash("Training module not found.", "error")
        return redirect(url_for("admin_training"))

    selected_ids = {int(x) for x in request.form.getlist("template_ids")}
    existing_items = db.execute(
        """SELECT * FROM onboarding_template_items
           WHERE step_type = 'training' AND related_id = ?""",
        (module_id,),
    ).fetchall()
    existing_template_ids = {item["template_id"] for item in existing_items}

    for template_id in selected_ids - existing_template_ids:
        max_order = db.execute(
            "SELECT COALESCE(MAX(sort_order), -1) FROM onboarding_template_items WHERE template_id = ?",
            (template_id,),
        ).fetchone()[0]
        db.execute(
            """INSERT INTO onboarding_template_items (template_id, step_name, step_type, related_id, sort_order)
               VALUES (?, ?, 'training', ?, ?)""",
            (template_id, f"Complete {module['title']}", module_id, max_order + 1),
        )

    for item in existing_items:
        if item["template_id"] not in selected_ids:
            db.execute("DELETE FROM onboarding_template_items WHERE id = ?", (item["id"],))

    db.commit()
    flash("Onboarding checklist membership updated.", "success")
    return redirect(url_for("training_detail_admin", module_id=module_id) + "#onboarding")


# ---------------------------------------------------------------------------
# Audit log viewer (Admin-only)
# ---------------------------------------------------------------------------

AUDIT_PAGE_SIZE = 50


@app.route("/admin/audit")
def audit_log_list():
    resp = require_admin()
    if resp:
        return resp

    db = get_db()
    q = request.args.get("q", "").strip()
    actor_id = request.args.get("actor_id", "").strip()
    action_type = request.args.get("action_type", "").strip()
    endpoint = request.args.get("endpoint", "").strip()
    date_from = request.args.get("date_from", "").strip()
    date_to = request.args.get("date_to", "").strip()
    try:
        page = max(1, int(request.args.get("page", 1)))
    except ValueError:
        page = 1

    where = []
    params = []
    if q:
        like = f"%{q}%"
        where.append(
            "(action_label LIKE ? OR entity_summary LIKE ? OR path LIKE ? OR actor_name LIKE ? OR details LIKE ?)"
        )
        params.extend([like, like, like, like, like])
    if actor_id:
        where.append("actor_id = ?")
        params.append(actor_id)
    if action_type in ("view", "change"):
        where.append("action_type = ?")
        params.append(action_type)
    if endpoint:
        where.append("endpoint = ?")
        params.append(endpoint)
    if date_from:
        where.append("created_at >= ?")
        params.append(date_from)
    if date_to:
        where.append("created_at <= ?")
        params.append(date_to + " 23:59:59")

    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    total = db.execute(f"SELECT COUNT(*) FROM audit_log {where_sql}", params).fetchone()[0]
    total_pages = max(1, (total + AUDIT_PAGE_SIZE - 1) // AUDIT_PAGE_SIZE)
    page = min(page, total_pages)
    offset = (page - 1) * AUDIT_PAGE_SIZE

    entries = db.execute(
        f"""SELECT * FROM audit_log {where_sql}
            ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?""",
        params + [AUDIT_PAGE_SIZE, offset],
    ).fetchall()

    actors = db.execute(
        """SELECT DISTINCT actor_id, actor_name FROM audit_log
           WHERE actor_id IS NOT NULL ORDER BY actor_name"""
    ).fetchall()
    endpoints = db.execute(
        "SELECT DISTINCT endpoint FROM audit_log WHERE endpoint IS NOT NULL ORDER BY endpoint"
    ).fetchall()

    return render_template(
        "audit_log.html",
        entries=entries,
        total=total,
        page=page,
        total_pages=total_pages,
        q=q,
        actor_id=actor_id,
        action_type=action_type,
        endpoint=endpoint,
        date_from=date_from,
        date_to=date_to,
        actors=actors,
        endpoints=endpoints,
    )


@app.route("/admin/audit/<int:log_id>")
def audit_log_detail(log_id):
    resp = require_admin()
    if resp:
        return resp

    db = get_db()
    entry = db.execute("SELECT * FROM audit_log WHERE id = ?", (log_id,)).fetchone()
    if entry is None:
        flash("Audit log entry not found.", "error")
        return redirect(url_for("audit_log_list"))

    try:
        details = json.loads(entry["details"]) if entry["details"] else {}
    except (ValueError, TypeError):
        details = {}

    return render_template("audit_log_detail.html", entry=entry, details=details)


# ---------------------------------------------------------------------------
# Reports (customizable, exportable views across employees/checklist/quizzes)
# ---------------------------------------------------------------------------

REPORT_LABELS = {
    "employees": "Employee Directory",
    "checklist": "Checklist Status",
    "quizzes": "Quiz Results",
}

REPORT_COLUMNS = {
    "employees": [
        ("name", "Name"), ("email", "Email"), ("username", "Username"), ("role", "Role"),
        ("job_title", "Job Title"), ("department", "Department"), ("hire_date", "Hire Date"),
        ("status", "Status"), ("phone", "Phone"), ("emergency_contact_name", "Emergency Contact"),
        ("emergency_contact_phone", "Emergency Contact Phone"), ("date_of_birth", "Date of Birth"),
        ("onboarding_pct", "Onboarding %"), ("training_pct", "Training %"), ("created_at", "Added On"),
    ],
    "checklist": [
        ("employee_name", "Employee"), ("step_name", "Checklist Item"), ("step_type", "Type"),
        ("status", "Status"), ("completed_at", "Completed At"),
    ],
    "quizzes": [
        ("employee_name", "Employee"), ("quiz_title", "Quiz"), ("score", "Score"),
        ("total", "Total Questions"), ("pct", "Percent"), ("passed", "Passed"),
        ("submitted_at", "Submitted At"),
    ],
}


def get_report_rows(db, report_type, args):
    if report_type == "employees":
        query = "SELECT * FROM employees WHERE 1=1"
        params = []
        role_filter = args.get("role", "")
        status_filter = args.get("status", "")
        if role_filter:
            query += " AND role = ?"
            params.append(role_filter)
        if status_filter:
            query += " AND status = ?"
            params.append(status_filter)
        query += " ORDER BY name"
        employees = db.execute(query, params).fetchall()
        rows = []
        for emp in employees:
            row = dict(emp)
            _, _, _, onboarding_pct = onboarding_progress(db, emp["id"])
            _, _, _, training_pct = training_progress(db, emp["id"])
            row["onboarding_pct"] = onboarding_pct
            row["training_pct"] = training_pct
            rows.append(row)
        return rows

    if report_type == "checklist":
        query = """SELECT onboarding_steps.*, employees.name AS employee_name
                   FROM onboarding_steps JOIN employees ON employees.id = onboarding_steps.employee_id
                   WHERE 1=1"""
        params = []
        step_type_filter = args.get("step_type", "")
        status_filter = args.get("status", "")
        employee_filter = args.get("employee_id", "")
        if step_type_filter:
            query += " AND onboarding_steps.step_type = ?"
            params.append(step_type_filter)
        if status_filter == "complete":
            query += " AND onboarding_steps.completed_at IS NOT NULL"
        elif status_filter == "pending":
            query += " AND onboarding_steps.completed_at IS NULL"
        if employee_filter:
            query += " AND onboarding_steps.employee_id = ?"
            params.append(employee_filter)
        query += " ORDER BY employees.name, onboarding_steps.id"
        steps = db.execute(query, params).fetchall()
        rows = []
        for step in steps:
            rows.append(
                {
                    "employee_name": step["employee_name"],
                    "step_name": resolve_checklist_display_name(
                        db, step["step_type"], step["related_id"], step["step_name"]
                    ),
                    "step_type": step["step_type"],
                    "status": "Complete" if step["completed_at"] else "Pending",
                    "completed_at": step["completed_at"] or "",
                }
            )
        return rows

    if report_type == "quizzes":
        query = """SELECT quiz_attempts.*, employees.name AS employee_name, quizzes.title AS quiz_title
                   FROM quiz_attempts
                   JOIN employees ON employees.id = quiz_attempts.employee_id
                   JOIN quizzes ON quizzes.id = quiz_attempts.quiz_id
                   WHERE 1=1"""
        params = []
        quiz_filter = args.get("quiz_id", "")
        passed_filter = args.get("passed", "")
        if quiz_filter:
            query += " AND quiz_attempts.quiz_id = ?"
            params.append(quiz_filter)
        if passed_filter == "passed":
            query += " AND quiz_attempts.passed = 1"
        elif passed_filter == "failed":
            query += " AND quiz_attempts.passed = 0"
        query += " ORDER BY quiz_attempts.submitted_at DESC"
        attempts = db.execute(query, params).fetchall()
        rows = []
        for a in attempts:
            pct = round((a["score"] / a["total"]) * 100) if a["total"] else 0
            rows.append(
                {
                    "employee_name": a["employee_name"],
                    "quiz_title": a["quiz_title"],
                    "score": a["score"],
                    "total": a["total"],
                    "pct": pct,
                    "passed": "Yes" if a["passed"] else "No",
                    "submitted_at": a["submitted_at"],
                }
            )
        return rows

    return []


def selected_report_columns(report_type, args):
    all_columns = REPORT_COLUMNS.get(report_type, [])
    selected_keys = args.getlist("columns")
    if not selected_keys:
        return all_columns
    return [c for c in all_columns if c[0] in selected_keys]


@app.route("/admin/reports")
def reports_home():
    resp = require_permission("reports_view")
    if resp:
        return resp

    db = get_db()
    report_type = request.args.get("report_type", "")
    columns = []
    rows = []
    if report_type in REPORT_COLUMNS:
        columns = selected_report_columns(report_type, request.args)
        rows = get_report_rows(db, report_type, request.args)

    employees_list = db.execute(
        "SELECT id, name FROM employees ORDER BY name"
    ).fetchall()
    quizzes_list = db.execute("SELECT id, title FROM quizzes ORDER BY title").fetchall()

    return render_template(
        "reports.html",
        report_labels=REPORT_LABELS,
        report_columns=REPORT_COLUMNS,
        report_type=report_type,
        columns=columns,
        rows=rows,
        employees_list=employees_list,
        quizzes_list=quizzes_list,
        args=request.args,
    )


@app.route("/admin/reports/export")
def reports_export():
    resp = require_permission("reports_view")
    if resp:
        return resp

    db = get_db()
    report_type = request.args.get("report_type", "")
    if report_type not in REPORT_COLUMNS:
        flash("Choose a report type first.", "error")
        return redirect(url_for("reports_home"))

    columns = selected_report_columns(report_type, request.args)
    rows = get_report_rows(db, report_type, request.args)

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow([label for _, label in columns])
    for row in rows:
        writer.writerow([row.get(key, "") for key, _ in columns])

    filename = f"{report_type}-report-{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}.csv"
    return Response(
        buffer.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


# ---------------------------------------------------------------------------
# Full data export / import (backup and restore)
# ---------------------------------------------------------------------------

def build_backup_zip_bytes(exported_by=None):
    """Build a full backup zip (db snapshot + manifest + uploads) and return its bytes.
    Shared by the on-demand export route and the automatic daily backup job."""
    # Consistent snapshot of the live database via SQLite's own backup API,
    # rather than copying the file directly (safe even mid-write).
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        tmp_path = tmp.name
    src = dst = None
    try:
        src = sqlite3.connect(DB_PATH)
        dst = sqlite3.connect(tmp_path)
        src.backup(dst)
    finally:
        if dst is not None:
            dst.close()
        if src is not None:
            src.close()

    try:
        with open(tmp_path, "rb") as f:
            db_bytes = f.read()
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("portal.db", db_bytes)
        zf.writestr(
            "manifest.json",
            json.dumps(
                {
                    "app": "employee-portal",
                    "exported_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
                    "exported_by": exported_by,
                },
                indent=2,
            ),
        )
        if os.path.isdir(UPLOADS_ROOT):
            for root, _dirs, files in os.walk(UPLOADS_ROOT):
                for fname in files:
                    full_path = os.path.join(root, fname)
                    arcname = os.path.join("uploads", os.path.relpath(full_path, UPLOADS_ROOT))
                    zf.write(full_path, arcname)
    buffer.seek(0)
    return buffer.getvalue()


@app.route("/admin/settings/export")
def export_data():
    resp = require_admin()
    if resp:
        return resp

    zip_bytes = build_backup_zip_bytes(exported_by=session.get("name"))
    filename = f"employee-portal-backup-{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}.zip"
    return send_file(
        io.BytesIO(zip_bytes), mimetype="application/zip", as_attachment=True, download_name=filename
    )


# ---------------------------------------------------------------------------
# Automatic daily backup
# ---------------------------------------------------------------------------

def list_backup_files():
    """Backup files on disk, newest first, as dicts with filename/size/mtime."""
    if not os.path.isdir(BACKUP_DIR):
        return []
    items = []
    for fname in os.listdir(BACKUP_DIR):
        if not BACKUP_FILENAME_RE.match(fname):
            continue
        full_path = os.path.join(BACKUP_DIR, fname)
        try:
            stat = os.stat(full_path)
        except OSError:
            continue
        items.append(
            {
                "filename": fname,
                "size": stat.st_size,
                "mtime": stat.st_mtime,
                "created": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %I:%M %p"),
            }
        )
    items.sort(key=lambda x: x["mtime"], reverse=True)
    return items


def cleanup_old_backups(retention_days):
    """Delete backup files older than retention_days (by file mtime)."""
    cutoff = time.time() - (retention_days * 86400)
    for item in list_backup_files():
        if item["mtime"] < cutoff:
            try:
                os.remove(os.path.join(BACKUP_DIR, item["filename"]))
            except OSError:
                pass


def perform_automatic_backup():
    """Build and save a backup file for today, if one doesn't already exist.
    Uses an exclusive lock file so multiple worker processes never race to
    write the same day's backup twice."""
    os.makedirs(BACKUP_DIR, exist_ok=True)
    today = datetime.utcnow().strftime("%Y%m%d")
    lock_path = os.path.join(BACKUP_DIR, f".lock-{today}")

    if any(f["filename"].startswith(f"employee-portal-backup-{today}-") for f in list_backup_files()):
        return False

    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd)
    except FileExistsError:
        return False  # another process already claimed today's backup

    try:
        zip_bytes = build_backup_zip_bytes(exported_by="Automatic Daily Backup")
        filename = f"employee-portal-backup-{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}.zip"
        with open(os.path.join(BACKUP_DIR, filename), "wb") as f:
            f.write(zip_bytes)

        db = sqlite3.connect(DB_PATH)
        try:
            retention_days = int(get_setting(db, "backup_retention_days", "30"))
        except ValueError:
            retention_days = 30
        finally:
            db.close()
        cleanup_old_backups(retention_days)
        return True
    except Exception:
        print("Automatic backup failed:", file=sys.stderr)
        import traceback
        traceback.print_exc(file=sys.stderr)
        return False


def backup_scheduler_loop():
    # Try once at startup (covers the app having been down at the usual time),
    # then just check hourly — perform_automatic_backup() is a no-op once
    # today's backup already exists, so this is safe to call repeatedly.
    while True:
        try:
            perform_automatic_backup()
        except Exception:
            print("Backup scheduler loop error:", file=sys.stderr)
            import traceback
            traceback.print_exc(file=sys.stderr)
        time.sleep(3600)


def start_backup_scheduler():
    thread = threading.Thread(target=backup_scheduler_loop, daemon=True, name="backup-scheduler")
    thread.start()


@app.route("/admin/settings/backups")
def backups_list():
    resp = require_admin()
    if resp:
        return resp
    db = get_db()
    try:
        retention_days = int(get_setting(db, "backup_retention_days", "30"))
    except ValueError:
        retention_days = 30
    return render_template("backups.html", backups=list_backup_files(), retention_days=retention_days)


@app.route("/admin/settings/backups/run-now", methods=["POST"])
def backups_run_now():
    resp = require_admin()
    if resp:
        return resp
    os.makedirs(BACKUP_DIR, exist_ok=True)
    zip_bytes = build_backup_zip_bytes(exported_by=session.get("name"))
    filename = f"employee-portal-backup-{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}.zip"
    with open(os.path.join(BACKUP_DIR, filename), "wb") as f:
        f.write(zip_bytes)
    db = get_db()
    try:
        retention_days = int(get_setting(db, "backup_retention_days", "30"))
    except ValueError:
        retention_days = 30
    cleanup_old_backups(retention_days)
    flash("Backup created.", "success")
    return redirect(url_for("backups_list"))


@app.route("/admin/settings/backups/<filename>/download")
def backups_download(filename):
    resp = require_admin()
    if resp:
        return resp
    if not BACKUP_FILENAME_RE.match(filename):
        flash("Invalid backup filename.", "error")
        return redirect(url_for("backups_list"))
    return send_from_directory(BACKUP_DIR, filename, as_attachment=True)


@app.route("/admin/settings/backups/<filename>/delete", methods=["POST"])
def backups_delete(filename):
    resp = require_admin()
    if resp:
        return resp
    if BACKUP_FILENAME_RE.match(filename):
        try:
            os.remove(os.path.join(BACKUP_DIR, filename))
            flash("Backup deleted.", "success")
        except OSError:
            flash("Could not delete that backup file.", "error")
    else:
        flash("Invalid backup filename.", "error")
    return redirect(url_for("backups_list"))


@app.route("/admin/settings/backups/retention", methods=["POST"])
def backups_set_retention():
    resp = require_admin()
    if resp:
        return resp
    db = get_db()
    try:
        days = max(1, int(request.form.get("retention_days", "30")))
    except ValueError:
        days = 30
    set_setting(db, "backup_retention_days", str(days))
    db.commit()
    cleanup_old_backups(days)
    flash(f"Backups will now be kept for {days} day(s).", "success")
    return redirect(url_for("backups_list"))


@app.route("/admin/settings/import", methods=["GET", "POST"])
def import_data():
    resp = require_admin()
    if resp:
        return resp

    if request.method == "POST":
        confirm = request.form.get("confirm", "").strip().lower()
        if confirm != "restore":
            flash('You must type "restore" to confirm.', "error")
            return render_template("import_data.html")

        file = request.files.get("backup_file")
        if not file or not file.filename or not file.filename.lower().endswith(".zip"):
            flash("Please choose a .zip backup file to restore.", "error")
            return render_template("import_data.html")

        try:
            zf = zipfile.ZipFile(io.BytesIO(file.read()))
        except zipfile.BadZipFile:
            flash("That file isn't a valid backup zip.", "error")
            return render_template("import_data.html")

        if "portal.db" not in zf.namelist():
            flash(
                "That zip doesn't contain a portal.db — it doesn't look like a backup from this app.",
                "error",
            )
            return render_template("import_data.html")

        # Extract the DB into a temp file and sanity-check it before touching anything live.
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
            tmp_path = tmp.name
            tmp.write(zf.read("portal.db"))

        valid = False
        check_conn = None
        try:
            check_conn = sqlite3.connect(tmp_path)
            valid = (
                check_conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'employees'"
                ).fetchone()
                is not None
            )
        except sqlite3.DatabaseError:
            valid = False
        finally:
            if check_conn is not None:
                check_conn.close()

        if not valid:
            os.remove(tmp_path)
            flash(
                "That backup's database looks corrupt or isn't an employee-portal backup.", "error"
            )
            return render_template("import_data.html")

        # Release our own connection to the live DB before swapping the file out from under it.
        db = g.pop("db", None)
        if db is not None:
            db.close()

        shutil.copyfile(tmp_path, DB_PATH)
        os.remove(tmp_path)

        # Bring an older backup's schema up to date with what this version of the app expects.
        db_migrate.migrate()

        # Replace uploaded files with exactly what's in the backup.
        if os.path.isdir(UPLOADS_ROOT):
            shutil.rmtree(UPLOADS_ROOT)
        os.makedirs(UPLOADS_ROOT, exist_ok=True)

        uploads_root_normalized = os.path.normpath(UPLOADS_ROOT) + os.sep
        for member in zf.namelist():
            if not member.startswith("uploads/") or member.endswith("/"):
                continue
            relative = member[len("uploads/") :]
            target = os.path.normpath(os.path.join(UPLOADS_ROOT, relative))
            if not target.startswith(uploads_root_normalized):
                continue  # zip-slip guard: skip anything that would escape the uploads folder
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with zf.open(member) as src_f, open(target, "wb") as dst_f:
                shutil.copyfileobj(src_f, dst_f)

        os.makedirs(UPLOAD_FOLDER, exist_ok=True)
        os.makedirs(EMPLOYEE_UPLOAD_FOLDER, exist_ok=True)
        os.makedirs(TRAINING_SLIDES_FOLDER, exist_ok=True)

        session.clear()
        flash("Backup restored. Please log in again.", "success")
        return redirect(url_for("login"))

    return render_template("import_data.html")


start_backup_scheduler()


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5001, debug=True, use_reloader=False)

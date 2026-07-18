import io
import json
import os
import re
import secrets
import shutil
import sqlite3
import tempfile
import zipfile
from datetime import datetime

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

UPLOADS_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "uploads")

app = Flask(__name__)
app.secret_key = "dev-secret-key-change-me"
app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024  # 200 MB (backup imports can be large)

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(EMPLOYEE_UPLOAD_FOLDER, exist_ok=True)
os.makedirs(TRAINING_SLIDES_FOLDER, exist_ok=True)

ADMIN_ROLES = ("Admin", "Manager")


def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def allowed_image(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_IMAGE_EXTENSIONS


def save_slides(db, module_id, files, captions=None):
    """Save uploaded image files as slides for a training module. `captions`, if given,
    is matched to `files` by position. Returns count added."""
    captions = captions or []
    max_order = db.execute(
        "SELECT COALESCE(MAX(sort_order), -1) FROM training_slides WHERE module_id = ?",
        (module_id,),
    ).fetchone()[0]
    added = 0
    for i, f in enumerate(files):
        if not f or not f.filename:
            continue
        if not allowed_image(f.filename):
            continue
        max_order += 1
        ext = f.filename.rsplit(".", 1)[1].lower()
        stored_name = f"{secrets.token_hex(8)}.{ext}"
        f.save(os.path.join(TRAINING_SLIDES_FOLDER, stored_name))
        caption = captions[i].strip() if i < len(captions) and captions[i].strip() else None
        db.execute(
            "INSERT INTO training_slides (module_id, image_path, caption, sort_order) VALUES (?, ?, ?, ?)",
            (module_id, stored_name, caption, max_order),
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
    """Return a redirect response if not logged in as Admin/Manager, otherwise None."""
    resp = require_login()
    if resp:
        return resp
    if session.get("role") not in ADMIN_ROLES:
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

PERMISSIONS = [
    ("manage_employees", "Add and edit employees; manage their notes and document requests"),
    ("manage_documents", "Create and delete policy documents"),
    ("manage_training", "Create, edit, and delete training modules; assign training"),
    ("manage_onboarding_checklists", "Create and manage onboarding checklist templates"),
    ("manage_settings", "Access Settings (signup page customization, etc.)"),
]
PERMISSION_KEYS = {key for key, _ in PERMISSIONS}


def has_permission(db, role, permission):
    """Admin always has every permission. Employee never has any. Manager is
    whatever's been explicitly granted in role_permissions."""
    if role == "Admin":
        return True
    if role != "Manager":
        return False
    row = db.execute(
        "SELECT 1 FROM role_permissions WHERE role = ? AND permission = ?", ("Manager", permission)
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

    return {"can": can}


def current_employee(db):
    return db.execute(
        "SELECT * FROM employees WHERE id = ?", (session["user_id"],)
    ).fetchone()


# ---------------------------------------------------------------------------
# Progress helpers
# ---------------------------------------------------------------------------

def onboarding_progress(db, employee_id):
    steps = db.execute(
        "SELECT * FROM onboarding_steps WHERE employee_id = ? ORDER BY id", (employee_id,)
    ).fetchall()
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
    one step per existing signature-required document, every onboarding-required
    training module, plus a generic task."""
    db.execute(
        """INSERT INTO onboarding_steps (employee_id, step_name, step_type, related_id)
           VALUES (?, 'Review Company Policies', 'task', NULL)""",
        (employee_id,),
    )
    docs = db.execute(
        "SELECT id, title FROM documents WHERE requires_signature = 1"
    ).fetchall()
    for doc in docs:
        db.execute(
            """INSERT INTO onboarding_steps (employee_id, step_name, step_type, related_id)
               VALUES (?, ?, 'document', ?)""",
            (employee_id, f"Sign {doc['title']}", doc["id"]),
        )

    modules = db.execute(
        "SELECT id, title FROM training_modules WHERE is_onboarding = 1"
    ).fetchall()
    for mod in modules:
        assign_module_to_employee(db, mod["id"], mod["title"], employee_id)

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


# ---------------------------------------------------------------------------
# Root / login / logout
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    if "user_id" not in session:
        return redirect(url_for("login"))
    if session.get("role") in ADMIN_ROLES:
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

        if employee["role"] in ADMIN_ROLES:
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

    return render_template("admin_dashboard.html", rows=rows)


@app.route("/admin/employees/add", methods=["GET", "POST"])
def add_employee():
    resp = require_permission("manage_employees")
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

        if role not in ("Admin", "Manager", "Employee"):
            role = "Employee"

        existing = db.execute(
            "SELECT id FROM employees WHERE lower(email) = ?", (email,)
        ).fetchone()
        if existing:
            flash("An employee with that email already exists.", "error")
            templates = db.execute("SELECT * FROM onboarding_templates ORDER BY name").fetchall()
            return render_template("add_employee.html", templates=templates)

        try:
            dob_date = datetime.strptime(date_of_birth, "%Y-%m-%d")
        except ValueError:
            flash("Please enter a valid date of birth — it's used to generate their password.", "error")
            templates = db.execute("SELECT * FROM onboarding_templates ORDER BY name").fetchall()
            return render_template("add_employee.html", templates=templates)

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
    return render_template("add_employee.html", templates=templates)


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


@app.route("/admin/employees/<int:employee_id>/edit", methods=["GET", "POST"])
def edit_employee(employee_id):
    resp = require_permission("manage_employees")
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

        if role not in ("Admin", "Manager", "Employee"):
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
                return render_template("edit_employee.html", employee=employee, templates=templates)

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
    return render_template("edit_employee.html", employee=employee, templates=templates)


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
    db.execute("DELETE FROM employees WHERE id = ?", (employee_id,))
    db.commit()

    flash("Employee deleted.", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/employees/<int:employee_id>/notes", methods=["POST"])
def add_note(employee_id):
    resp = require_permission("manage_employees")
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
    resp = require_permission("manage_employees")
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
    resp = require_permission("manage_employees")
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
    resp = require_permission("manage_employees")
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

    return render_template(
        "employee_dashboard.html",
        employee=employee,
        onboarding_pct=onboarding_pct,
        steps_done=steps_done,
        steps_total=steps_total,
        assignments=assignments,
        training_pct=training_pct,
        pending_docs=pending_docs,
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

    if session.get("role") not in ADMIN_ROLES and upload["employee_id"] != session["user_id"]:
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

    if session.get("role") not in ADMIN_ROLES and upload["employee_id"] != session["user_id"]:
        flash("You do not have permission to delete that file.", "error")
        return redirect(url_for("employee_checklist"))

    is_admin_view = session.get("role") in ADMIN_ROLES and upload["employee_id"] != session["user_id"]

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
        resp = require_permission("manage_documents")
        if resp:
            return resp

        title = request.form.get("title", "").strip()
        content = request.form.get("content", "").strip()
        requires_signature = 1 if request.form.get("requires_signature") == "on" else 0

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
                "INSERT INTO documents (title, content, file_path, requires_signature) VALUES (?, ?, ?, ?)",
                (title, content, file_path, requires_signature),
            )
            new_doc_id = cur.lastrowid

            if requires_signature:
                employees = db.execute(
                    "SELECT id FROM employees WHERE role = 'Employee'"
                ).fetchall()
                for emp in employees:
                    db.execute(
                        """INSERT INTO onboarding_steps (employee_id, step_name, step_type, related_id)
                           VALUES (?, ?, 'document', ?)""",
                        (emp["id"], f"Sign {title}", new_doc_id),
                    )

            db.commit()
            flash("Document created.", "success")
        return redirect(url_for("admin_documents"))

    documents = db.execute("SELECT * FROM documents ORDER BY created_at DESC").fetchall()
    doc_rows = []
    for doc in documents:
        signed_count = db.execute(
            "SELECT COUNT(*) FROM signatures WHERE document_id = ?", (doc["id"],)
        ).fetchone()[0]
        doc_rows.append({"document": doc, "signed_count": signed_count})

    return render_template("admin_documents.html", doc_rows=doc_rows)


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

    signed = db.execute(
        """SELECT signatures.*, employees.name AS employee_name
           FROM signatures JOIN employees ON employees.id = signatures.employee_id
           WHERE document_id = ? ORDER BY signed_at DESC""",
        (document_id,),
    ).fetchall()
    unsigned = db.execute(
        """SELECT * FROM employees WHERE role = 'Employee' AND id NOT IN (
             SELECT employee_id FROM signatures WHERE document_id = ?
           )""",
        (document_id,),
    ).fetchall()

    return render_template(
        "document_audit.html", document=document, signed=signed, unsigned=unsigned
    )


@app.route("/admin/documents/<int:document_id>/delete", methods=["POST"])
def delete_document(document_id):
    resp = require_permission("manage_documents")
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

    db.execute(
        "DELETE FROM onboarding_steps WHERE step_type = 'document' AND related_id = ?",
        (document_id,),
    )
    db.execute(
        "DELETE FROM onboarding_template_items WHERE step_type = 'document' AND related_id = ?",
        (document_id,),
    )
    db.execute("DELETE FROM signatures WHERE document_id = ?", (document_id,))
    db.execute("DELETE FROM documents WHERE id = ?", (document_id,))
    db.commit()

    flash("Document deleted.", "success")
    return redirect(url_for("admin_documents"))


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
    resp = require_permission("manage_training")
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
    resp = require_permission("manage_training")
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
    resp = require_permission("manage_training")
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
    resp = require_permission("manage_training")
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
    resp = require_permission("manage_training")
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
    resp = require_permission("manage_training")
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
    resp = require_permission("manage_training")
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
    resp = require_permission("manage_training")
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
    resp = require_permission("manage_training")
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

    new_image_path = slide["image_path"]
    if new_image_path:
        src = os.path.join(TRAINING_SLIDES_FOLDER, new_image_path)
        ext = new_image_path.rsplit(".", 1)[-1]
        new_image_path = f"{secrets.token_hex(8)}.{ext}"
        try:
            shutil.copyfile(src, os.path.join(TRAINING_SLIDES_FOLDER, new_image_path))
        except OSError:
            new_image_path = slide["image_path"]

    cur = db.execute(
        """INSERT INTO training_slides (module_id, image_path, caption, background_color, sort_order)
           VALUES (?, ?, ?, ?, ?)""",
        (module_id, new_image_path, slide["caption"], slide["background_color"], max_order + 1),
    )
    new_slide_id = cur.lastrowid

    elements = db.execute(
        "SELECT * FROM slide_elements WHERE slide_id = ?", (slide_id,)
    ).fetchall()
    for el in elements:
        content = el["content"]
        if el["element_type"] == "image" and content:
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
                font_size, color, bold, align)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
            ),
        )

    db.commit()
    flash("Slide duplicated.", "success")
    return redirect(url_for("slide_editor", slide_id=new_slide_id))


@app.route("/admin/training/slides/<int:slide_id>/background", methods=["POST"])
def update_slide_background(slide_id):
    resp = require_permission("manage_training")
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
    resp = require_permission("manage_training")
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
    resp = require_permission("manage_training")
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


@app.route("/admin/training/slides/<int:slide_id>/elements/image", methods=["POST"])
def add_image_element(slide_id):
    resp = require_permission("manage_training")
    if resp:
        return resp

    db = get_db()
    slide = db.execute("SELECT id FROM training_slides WHERE id = ?", (slide_id,)).fetchone()
    if slide is None:
        return {"error": "not found"}, 404

    file = request.files.get("image_file")
    if not file or not file.filename or not allowed_image(file.filename):
        return {"error": "invalid image"}, 400

    ext = file.filename.rsplit(".", 1)[1].lower()
    stored_name = f"{secrets.token_hex(8)}.{ext}"
    file.save(os.path.join(TRAINING_SLIDES_FOLDER, stored_name))

    max_z = db.execute(
        "SELECT COALESCE(MAX(z_index), 0) FROM slide_elements WHERE slide_id = ?", (slide_id,)
    ).fetchone()[0]

    cur = db.execute(
        """INSERT INTO slide_elements
           (slide_id, element_type, content, pos_x, pos_y, width, height, z_index)
           VALUES (?, 'image', ?, 10, 10, 50, 50, ?)""",
        (slide_id, stored_name, max_z + 1),
    )
    db.commit()

    element = db.execute(
        "SELECT * FROM slide_elements WHERE id = ?", (cur.lastrowid,)
    ).fetchone()
    result = dict(element)
    result["image_url"] = url_for("training_slide_file", filename=stored_name)
    return {"ok": True, "element": result}


@app.route("/admin/training/slides/elements/<int:element_id>/update", methods=["POST"])
def update_slide_element(element_id):
    resp = require_permission("manage_training")
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
    resp = require_permission("manage_training")
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
    resp = require_permission("manage_training")
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


@app.route("/admin/training/<int:module_id>/assign", methods=["POST"])
def assign_training(module_id):
    resp = require_permission("manage_training")
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
    resp = require_permission("manage_training")
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
# Settings / onboarding checklist templates (job-specific onboarding)
# ---------------------------------------------------------------------------

@app.route("/admin/settings")
def settings_home():
    resp = require_admin_or_manager()
    if resp:
        return resp
    return render_template("settings.html")


@app.route("/admin/settings/role-permissions", methods=["GET", "POST"])
def role_permissions_settings():
    resp = require_admin()
    if resp:
        return resp

    db = get_db()

    if request.method == "POST":
        selected = {
            key for key in request.form.getlist("permissions") if key in PERMISSION_KEYS
        }
        db.execute("DELETE FROM role_permissions WHERE role = 'Manager'")
        for key in selected:
            db.execute(
                "INSERT INTO role_permissions (role, permission) VALUES ('Manager', ?)", (key,)
            )
        db.commit()
        flash("Role permissions updated.", "success")
        return redirect(url_for("role_permissions_settings"))

    manager_permissions = {
        row["permission"]
        for row in db.execute(
            "SELECT permission FROM role_permissions WHERE role = 'Manager'"
        ).fetchall()
    }

    return render_template(
        "role_permissions.html", permissions=PERMISSIONS, manager_permissions=manager_permissions
    )


@app.route("/admin/settings/signup-page", methods=["GET", "POST"])
def onboarding_page_settings():
    resp = require_permission("manage_settings")
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
    resp = require_permission("manage_onboarding_checklists")
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

    items = db.execute(
        "SELECT * FROM onboarding_template_items WHERE template_id = ? ORDER BY sort_order, id",
        (template_id,),
    ).fetchall()
    documents = db.execute("SELECT * FROM documents ORDER BY title").fetchall()
    modules = db.execute("SELECT * FROM training_modules ORDER BY title").fetchall()
    employees_using = db.execute(
        "SELECT * FROM employees WHERE onboarding_template_id = ? ORDER BY name", (template_id,)
    ).fetchall()

    return render_template(
        "onboarding_template_detail.html",
        template=template,
        items=items,
        documents=documents,
        modules=modules,
        employees_using=employees_using,
    )


@app.route("/admin/settings/onboarding-templates/<int:template_id>/items/task", methods=["POST"])
def add_template_task_item(template_id):
    resp = require_permission("manage_onboarding_checklists")
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
    resp = require_permission("manage_onboarding_checklists")
    if resp:
        return resp

    db = get_db()
    template = db.execute(
        "SELECT id FROM onboarding_templates WHERE id = ?", (template_id,)
    ).fetchone()
    if template is None:
        flash("Onboarding checklist not found.", "error")
        return redirect(url_for("onboarding_templates_list"))

    document_id = request.form.get("document_id", "")
    document = db.execute("SELECT * FROM documents WHERE id = ?", (document_id,)).fetchone()
    if document is None:
        flash("Please choose a document.", "error")
        return redirect(url_for("onboarding_template_detail", template_id=template_id))

    max_order = db.execute(
        "SELECT COALESCE(MAX(sort_order), -1) FROM onboarding_template_items WHERE template_id = ?",
        (template_id,),
    ).fetchone()[0]
    db.execute(
        """INSERT INTO onboarding_template_items (template_id, step_name, step_type, related_id, sort_order)
           VALUES (?, ?, 'document', ?, ?)""",
        (template_id, f"Sign {document['title']}", document["id"], max_order + 1),
    )
    db.commit()
    flash("Document added to checklist.", "success")
    return redirect(url_for("onboarding_template_detail", template_id=template_id))


@app.route("/admin/settings/onboarding-templates/<int:template_id>/items/training", methods=["POST"])
def add_template_training_item(template_id):
    resp = require_permission("manage_onboarding_checklists")
    if resp:
        return resp

    db = get_db()
    template = db.execute(
        "SELECT id FROM onboarding_templates WHERE id = ?", (template_id,)
    ).fetchone()
    if template is None:
        flash("Onboarding checklist not found.", "error")
        return redirect(url_for("onboarding_templates_list"))

    module_id = request.form.get("module_id", "")
    module = db.execute("SELECT * FROM training_modules WHERE id = ?", (module_id,)).fetchone()
    if module is None:
        flash("Please choose a training module.", "error")
        return redirect(url_for("onboarding_template_detail", template_id=template_id))

    max_order = db.execute(
        "SELECT COALESCE(MAX(sort_order), -1) FROM onboarding_template_items WHERE template_id = ?",
        (template_id,),
    ).fetchone()[0]
    db.execute(
        """INSERT INTO onboarding_template_items (template_id, step_name, step_type, related_id, sort_order)
           VALUES (?, ?, 'training', ?, ?)""",
        (template_id, f"Complete {module['title']}", module["id"], max_order + 1),
    )
    db.commit()
    flash("Training module added to checklist.", "success")
    return redirect(url_for("onboarding_template_detail", template_id=template_id))


@app.route("/admin/settings/onboarding-templates/items/<int:item_id>/delete", methods=["POST"])
def delete_template_item(item_id):
    resp = require_permission("manage_onboarding_checklists")
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
    resp = require_permission("manage_onboarding_checklists")
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
    resp = require_permission("manage_onboarding_checklists")
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
# Full data export / import (backup and restore)
# ---------------------------------------------------------------------------

@app.route("/admin/settings/export")
def export_data():
    resp = require_admin()
    if resp:
        return resp

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
                    "exported_by": session.get("name"),
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

    filename = f"employee-portal-backup-{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}.zip"
    return send_file(
        buffer, mimetype="application/zip", as_attachment=True, download_name=filename
    )


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
            zf = zipfile.ZipFile(file.stream)
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


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5001, debug=True, use_reloader=False)

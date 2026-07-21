import sqlite3
from werkzeug.security import generate_password_hash

DB_PATH = "portal.db"
SCHEMA_PATH = "schema.sql"


def init_db():
    conn = sqlite3.connect(DB_PATH)
    with open(SCHEMA_PATH, "r") as f:
        conn.executescript(f.read())

    admin_password_hash = generate_password_hash("admin123")
    conn.execute(
        """INSERT INTO employees
           (name, email, username, password_hash, role, job_title, department, hire_date, status, onboarding_token_used)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)""",
        (
            "Portal Admin",
            "admin@example.com",
            "portaladmin",
            admin_password_hash,
            "Admin",
            "System Administrator",
            "IT",
            "2020-01-01",
            "Active",
        ),
    )

    conn.execute(
        """INSERT INTO documents (title, content, requires_signature)
           VALUES (?, ?, 1)""",
        (
            "Employee Handbook Acknowledgment",
            "By signing below, you acknowledge that you have received, read, and understood the "
            "Employee Handbook, and agree to comply with all policies and procedures described within it. "
            "This includes workplace conduct, attendance expectations, and company values.",
        ),
    )

    conn.execute(
        """INSERT INTO training_modules (title, description, content)
           VALUES (?, ?, ?)""",
        (
            "Safety Training",
            "An introduction to workplace safety procedures and emergency protocols.",
            "Review the following: emergency exits are located at each end of the building. "
            "In case of fire, use the stairs, never the elevator. Report any safety hazards to your "
            "manager immediately. First aid kits are located in the break room and near reception.",
        ),
    )

    for key in (
        "employees_add", "employees_edit", "employees_notes", "employees_checklist",
        "documents_create", "documents_edit", "documents_delete", "documents_signatures", "documents_assign",
        "training_create", "training_edit", "training_delete", "training_slides", "training_assign",
        "quizzes_create", "quizzes_edit", "quizzes_delete", "quizzes_assign", "quizzes_lock",
        "quizzes_results_view", "quizzes_results_edit",
        "checklists_templates", "checklists_items", "checklists_master", "checklists_order",
        "settings_signup_page",
    ):
        conn.execute(
            "INSERT INTO role_permissions (role, permission) VALUES ('Manager', ?)", (key,)
        )

    conn.execute(
        "INSERT INTO master_checklist_items (step_name, sort_order) VALUES ('Review Company Policies', 0)"
    )

    conn.commit()
    conn.close()
    print("Database initialized at", DB_PATH)
    print("Seed admin login — username: portaladmin / password: admin123")


if __name__ == "__main__":
    init_db()

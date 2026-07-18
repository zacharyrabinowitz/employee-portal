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
        "manage_employees",
        "manage_documents",
        "manage_training",
        "manage_onboarding_checklists",
        "manage_settings",
    ):
        conn.execute(
            "INSERT INTO role_permissions (role, permission) VALUES ('Manager', ?)", (key,)
        )

    conn.commit()
    conn.close()
    print("Database initialized at", DB_PATH)
    print("Seed admin login — username: portaladmin / password: admin123")


if __name__ == "__main__":
    init_db()

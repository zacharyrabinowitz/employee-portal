DROP TABLE IF EXISTS custom_roles;
DROP TABLE IF EXISTS quiz_locks;
DROP TABLE IF EXISTS audit_log;
DROP TABLE IF EXISTS master_checklist_items;
DROP TABLE IF EXISTS quiz_attempt_answers;
DROP TABLE IF EXISTS quiz_attempts;
DROP TABLE IF EXISTS quiz_choices;
DROP TABLE IF EXISTS quiz_questions;
DROP TABLE IF EXISTS quizzes;
DROP TABLE IF EXISTS employee_uploads;
DROP TABLE IF EXISTS role_permissions;
DROP TABLE IF EXISTS portal_settings;
DROP TABLE IF EXISTS slide_elements;
DROP TABLE IF EXISTS training_slides;
DROP TABLE IF EXISTS notes;
DROP TABLE IF EXISTS onboarding_steps;
DROP TABLE IF EXISTS onboarding_template_items;
DROP TABLE IF EXISTS onboarding_templates;
DROP TABLE IF EXISTS training_assignments;
DROP TABLE IF EXISTS training_modules;
DROP TABLE IF EXISTS signatures;
DROP TABLE IF EXISTS documents;
DROP TABLE IF EXISTS employees;

CREATE TABLE onboarding_templates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    description TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE employees (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    email TEXT NOT NULL UNIQUE,
    username TEXT,
    password_hash TEXT,
    role TEXT NOT NULL DEFAULT 'Employee',
    job_title TEXT,
    department TEXT,
    hire_date TEXT,
    status TEXT NOT NULL DEFAULT 'Pending',
    phone TEXT,
    emergency_contact_name TEXT,
    emergency_contact_phone TEXT,
    onboarding_token TEXT,
    onboarding_token_used INTEGER NOT NULL DEFAULT 0,
    onboarding_template_id INTEGER REFERENCES onboarding_templates(id),
    date_of_birth TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE portal_settings (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE onboarding_template_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    template_id INTEGER NOT NULL REFERENCES onboarding_templates(id),
    step_name TEXT NOT NULL,
    step_type TEXT NOT NULL,
    related_id INTEGER,
    sort_order INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    content TEXT,
    file_path TEXT,
    requires_signature INTEGER NOT NULL DEFAULT 1,
    requires_upload INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE signatures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_id INTEGER NOT NULL REFERENCES employees(id),
    document_id INTEGER NOT NULL REFERENCES documents(id),
    signature_text TEXT NOT NULL,
    signed_at TEXT NOT NULL DEFAULT (datetime('now')),
    session_marker TEXT
);

CREATE TABLE training_modules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    description TEXT,
    content TEXT,
    is_onboarding INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE training_assignments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_id INTEGER NOT NULL REFERENCES employees(id),
    module_id INTEGER NOT NULL REFERENCES training_modules(id),
    assigned_at TEXT NOT NULL DEFAULT (datetime('now')),
    completed_at TEXT,
    status TEXT NOT NULL DEFAULT 'Assigned'
);

CREATE TABLE training_slides (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    module_id INTEGER NOT NULL REFERENCES training_modules(id),
    image_path TEXT NOT NULL,
    caption TEXT,
    background_color TEXT NOT NULL DEFAULT '#ffffff',
    sort_order INTEGER NOT NULL DEFAULT 0,
    media_data BLOB,
    media_mimetype TEXT,
    media_kind TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE slide_elements (
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
    media_data BLOB,
    media_mimetype TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE onboarding_steps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_id INTEGER NOT NULL REFERENCES employees(id),
    step_name TEXT NOT NULL,
    step_type TEXT NOT NULL,
    related_id INTEGER,
    completed_at TEXT
);

CREATE TABLE notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_id INTEGER NOT NULL REFERENCES employees(id),
    author_name TEXT,
    body TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT
);

CREATE TABLE role_permissions (
    role TEXT NOT NULL,
    permission TEXT NOT NULL,
    PRIMARY KEY (role, permission)
);

CREATE TABLE employee_uploads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_id INTEGER NOT NULL REFERENCES employees(id),
    label TEXT NOT NULL,
    file_path TEXT NOT NULL,
    onboarding_step_id INTEGER REFERENCES onboarding_steps(id),
    uploaded_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE quizzes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    description TEXT,
    training_module_id INTEGER REFERENCES training_modules(id),
    passing_score INTEGER NOT NULL DEFAULT 70,
    is_onboarding INTEGER NOT NULL DEFAULT 0,
    is_locked INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE quiz_questions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    quiz_id INTEGER NOT NULL REFERENCES quizzes(id),
    question_text TEXT NOT NULL,
    question_type TEXT NOT NULL DEFAULT 'single_choice',
    text_answer TEXT,
    sort_order INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE quiz_choices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    question_id INTEGER NOT NULL REFERENCES quiz_questions(id),
    choice_text TEXT NOT NULL,
    match_text TEXT,
    is_correct INTEGER NOT NULL DEFAULT 0,
    sort_order INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE quiz_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    quiz_id INTEGER NOT NULL REFERENCES quizzes(id),
    employee_id INTEGER NOT NULL REFERENCES employees(id),
    score INTEGER NOT NULL,
    total INTEGER NOT NULL,
    passed INTEGER NOT NULL DEFAULT 0,
    submitted_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE quiz_attempt_answers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    attempt_id INTEGER NOT NULL REFERENCES quiz_attempts(id),
    question_id INTEGER NOT NULL REFERENCES quiz_questions(id),
    choice_id INTEGER REFERENCES quiz_choices(id),
    text_answer TEXT,
    is_correct INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE master_checklist_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    step_name TEXT NOT NULL,
    sort_order INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE audit_log (
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
);

CREATE INDEX idx_audit_log_created_at ON audit_log(created_at);
CREATE INDEX idx_audit_log_actor_id ON audit_log(actor_id);
CREATE INDEX idx_audit_log_action_type ON audit_log(action_type);

CREATE TABLE quiz_locks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    quiz_id INTEGER NOT NULL REFERENCES quizzes(id),
    employee_id INTEGER NOT NULL REFERENCES employees(id),
    locked_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(quiz_id, employee_id)
);

CREATE TABLE custom_roles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    description TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

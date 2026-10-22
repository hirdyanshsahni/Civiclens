PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS users (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  email TEXT NOT NULL UNIQUE COLLATE NOCASE,
  password_hash TEXT NOT NULL,
  role TEXT NOT NULL DEFAULT 'citizen',
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reports (
  id TEXT PRIMARY KEY,
  reporter_id TEXT REFERENCES users(id),
  category TEXT NOT NULL,
  title TEXT NOT NULL,
  description TEXT NOT NULL,
  location TEXT NOT NULL,
  ward TEXT NOT NULL DEFAULT '',
  lat REAL NOT NULL,
  lng REAL NOT NULL,
  severity TEXT NOT NULL,
  status TEXT NOT NULL,
  department TEXT NOT NULL,
  assignee TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  resolved_at TEXT,
  photo_name TEXT NOT NULL DEFAULT '',
  photo_hash TEXT NOT NULL DEFAULT '',
  ai_provider TEXT NOT NULL DEFAULT 'none',
  ai_confidence REAL,
  ai_explanation TEXT NOT NULL DEFAULT '',
  ai_category TEXT NOT NULL DEFAULT '',
  ai_severity TEXT NOT NULL DEFAULT '',
  resolution_details TEXT NOT NULL DEFAULT '',
  impact_override INTEGER,
  override_reason TEXT NOT NULL DEFAULT '',
  duplicate_of TEXT REFERENCES reports(id),
  demo INTEGER NOT NULL DEFAULT 0,
  is_sensitive INTEGER NOT NULL DEFAULT 0,
  reported_official_role TEXT NOT NULL DEFAULT '',
  reported_official_name TEXT NOT NULL DEFAULT '',
  incident_at TEXT NOT NULL DEFAULT '',
  first_response_due_at TEXT NOT NULL DEFAULT '',
  first_response_at TEXT,
  resolution_confirmed_at TEXT,
  submission_key TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS timeline (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  report_id TEXT NOT NULL REFERENCES reports(id) ON DELETE CASCADE,
  status TEXT NOT NULL,
  actor_id TEXT REFERENCES users(id),
  detail TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  report_id TEXT NOT NULL REFERENCES reports(id) ON DELETE CASCADE,
  action TEXT NOT NULL,
  actor_id TEXT REFERENCES users(id),
  payload TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS internal_notes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  report_id TEXT NOT NULL REFERENCES reports(id) ON DELETE CASCADE,
  author_id TEXT NOT NULL REFERENCES users(id),
  note TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS verifications (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  report_id TEXT NOT NULL REFERENCES reports(id) ON DELETE CASCADE,
  user_id TEXT NOT NULL REFERENCES users(id),
  created_at TEXT NOT NULL,
  UNIQUE(report_id, user_id)
);

CREATE TABLE IF NOT EXISTS email_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  report_id TEXT NOT NULL REFERENCES reports(id) ON DELETE CASCADE,
  user_id TEXT REFERENCES users(id),
  event_type TEXT NOT NULL,
  recipient TEXT NOT NULL,
  delivery_status TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS resolution_evidence (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  report_id TEXT NOT NULL REFERENCES reports(id) ON DELETE CASCADE,
  uploader_id TEXT NOT NULL REFERENCES users(id),
  photo_name TEXT NOT NULL,
  caption TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS report_status_idx ON reports(status);
CREATE INDEX IF NOT EXISTS report_category_idx ON reports(category);
CREATE INDEX IF NOT EXISTS report_created_idx ON reports(created_at);
CREATE INDEX IF NOT EXISTS timeline_report_idx ON timeline(report_id);

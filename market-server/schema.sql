-- The Market's database (Cloudflare D1, which is SQLite).
-- Apply with: npx wrangler d1 execute teto-market --remote --file=schema.sql

CREATE TABLE IF NOT EXISTS users (
  id TEXT PRIMARY KEY,
  email TEXT NOT NULL UNIQUE,        -- never shown to anyone
  handle TEXT NOT NULL UNIQUE,       -- @name: a-z 0-9 _
  name TEXT NOT NULL,
  bio TEXT NOT NULL DEFAULT '',
  links TEXT NOT NULL DEFAULT '[]',  -- JSON list of https addresses
  avatar_type TEXT NOT NULL DEFAULT '',
  pass_salt TEXT NOT NULL,
  pass_hash TEXT NOT NULL,           -- PBKDF2-SHA256, 100k rounds
  status TEXT NOT NULL DEFAULT 'active', -- active | banned | closed
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
  token_hash TEXT PRIMARY KEY,       -- SHA-256 of the token the app keeps
  user_id TEXT NOT NULL,
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS sessions_user ON sessions (user_id);

CREATE TABLE IF NOT EXISTS items (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,                -- tuning | voicebank | sound
  status TEXT NOT NULL,              -- pending | live | rejected | removed
  name TEXT NOT NULL,
  author TEXT NOT NULL DEFAULT '',
  user_id TEXT NOT NULL DEFAULT '',  -- who shared it
  description TEXT NOT NULL DEFAULT '',
  tags TEXT NOT NULL DEFAULT '[]',   -- JSON list
  language TEXT NOT NULL DEFAULT '', -- ja | en | th | zh | ko | multi | other
  licence TEXT NOT NULL DEFAULT '',
  official_url TEXT NOT NULL DEFAULT '',
  file_size INTEGER NOT NULL DEFAULT 0,
  file_type TEXT NOT NULL DEFAULT '',
  file_name TEXT NOT NULL DEFAULT '',
  file_sha256 TEXT NOT NULL DEFAULT '',
  demo_size INTEGER NOT NULL DEFAULT 0,
  demo_type TEXT NOT NULL DEFAULT '',
  bpm INTEGER,
  music_key TEXT NOT NULL DEFAULT '',
  price REAL NOT NULL DEFAULT 0,     -- 0 is free; otherwise sold from shop_url
  currency TEXT NOT NULL DEFAULT '', -- THB | USD | JPY | EUR
  shop_url TEXT NOT NULL DEFAULT '',
  likes INTEGER NOT NULL DEFAULT 0,
  downloads INTEGER NOT NULL DEFAULT 0,
  reports INTEGER NOT NULL DEFAULT 0,
  edit_hash TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS items_live ON items (status, kind, likes DESC);
CREATE INDEX IF NOT EXISTS items_user ON items (user_id);

CREATE TABLE IF NOT EXISTS likes (
  item_id TEXT NOT NULL,
  device TEXT NOT NULL,              -- a random id the app makes once per PC
  PRIMARY KEY (item_id, device)
);

CREATE TABLE IF NOT EXISTS reports (
  item_id TEXT NOT NULL,
  device TEXT NOT NULL,
  reason TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  PRIMARY KEY (item_id, device)
);

-- Tries per (hashed) address, for the limits: upload | signup | login.
CREATE TABLE IF NOT EXISTS attempts (
  ip_hash TEXT NOT NULL,
  kind TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS attempts_recent ON attempts (ip_hash, kind, created_at);

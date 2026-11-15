import sqlite3
from datetime import datetime, timezone

SCHEMA = '''
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS models(slug TEXT PRIMARY KEY,name TEXT NOT NULL,vendor TEXT NOT NULL,kind TEXT NOT NULL CHECK(kind IN ('model','agent')),homepage TEXT);
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,subject_hash TEXT UNIQUE NOT NULL,account_month TEXT NOT NULL,display_name TEXT,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS reviews(id INTEGER PRIMARY KEY,user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,model_slug TEXT NOT NULL REFERENCES models(slug),model_version TEXT NOT NULL,task_category TEXT NOT NULL CHECK(task_category IN ('coding','extraction','rag','agents')),task_description TEXT NOT NULL,stack TEXT NOT NULL,harness TEXT NOT NULL,outcome INTEGER NOT NULL CHECK(outcome BETWEEN 1 AND 5),retries INTEGER NOT NULL CHECK(retries BETWEEN 0 AND 50),cost_usd_cents INTEGER CHECK(cost_usd_cents BETWEEN 0 AND 1000000),used_on TEXT NOT NULL,iso_week TEXT NOT NULL,proof_sha256 TEXT,source TEXT NOT NULL CHECK(source IN ('user','founder_seed','consented_import')),status TEXT NOT NULL DEFAULT 'published' CHECK(status IN ('published','hidden')),content_hash TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_reviews_model_week ON reviews(model_slug,iso_week);
CREATE UNIQUE INDEX IF NOT EXISTS uq_reviews_user_version_week ON reviews(user_id,model_slug,lower(model_version),iso_week) WHERE user_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_reviews_user_content ON reviews(user_id,content_hash) WHERE user_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_seed_review_content ON reviews(source,content_hash) WHERE source IN ('founder_seed','consented_import');
CREATE TABLE IF NOT EXISTS waitlist(id INTEGER PRIMARY KEY,kind TEXT NOT NULL CHECK(kind IN ('vendor','reader')),vault_token TEXT NOT NULL,contact_digest TEXT NOT NULL,company TEXT,role TEXT CHECK(role IN ('devrel','pmm','product','eng','other')),price_interest TEXT CHECK(price_interest IN ('report_500','report_1500','sub_500_month','none')),created_at TEXT NOT NULL,UNIQUE(kind,contact_digest));
CREATE TABLE IF NOT EXISTS sessions(nonce TEXT PRIMARY KEY,user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,csrf TEXT NOT NULL,expires_at TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS oauth_states(state_digest TEXT PRIMARY KEY,session_nonce TEXT NOT NULL REFERENCES sessions(nonce) ON DELETE CASCADE,expires_at TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS vault_deletions(vault_token TEXT PRIMARY KEY,created_at TEXT NOT NULL);
'''


def connect(path):
    db = sqlite3.connect(path, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    db.execute('PRAGMA busy_timeout=10000')
    return db


def init(path):
    with connect(path) as db:
        db.executescript(SCHEMA)


def now():
    return datetime.now(timezone.utc).isoformat()

"""The SQLite index in the state directory."""

import os
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS scans(
  id INTEGER PRIMARY KEY AUTOINCREMENT, started REAL, finished REAL, roots TEXT,
  files INTEGER DEFAULT 0, bytes INTEGER DEFAULT 0, errors INTEGER DEFAULT 0, status TEXT);
CREATE TABLE IF NOT EXISTS dirs(
  scan_id INTEGER, path TEXT, root TEXT, depth INTEGER, bytes INTEGER, files INTEGER,
  newest REAL, newest_path TEXT, exec_atime REAL, PRIMARY KEY(scan_id, path));
CREATE TABLE IF NOT EXISTS links(scan_id INTEGER, link TEXT, target TEXT);
CREATE INDEX IF NOT EXISTS links_target ON links(scan_id, target);
CREATE TABLE IF NOT EXISTS refs(scan_id INTEGER, token TEXT, source TEXT, source_mtime REAL);
CREATE INDEX IF NOT EXISTS refs_token ON refs(scan_id, token);
CREATE TABLE IF NOT EXISTS rrefs(scan_id INTEGER, token TEXT, source TEXT, source_mtime REAL);
CREATE TABLE IF NOT EXISTS hits(scan_id INTEGER, rule TEXT, path TEXT, bytes INTEGER, files INTEGER, newest REAL);
CREATE INDEX IF NOT EXISTS hits_scan ON hits(scan_id, rule);
CREATE TABLE IF NOT EXISTS repos(scan_id INTEGER, path TEXT);
CREATE TABLE IF NOT EXISTS tags(
  path TEXT PRIMARY KEY, category TEXT, project TEXT, owner TEXT, note TEXT, expires REAL,
  rebuild TEXT, created REAL);
CREATE TABLE IF NOT EXISTS plans(
  id TEXT PRIMARY KEY, created REAL, creator TEXT, note TEXT, status TEXT, items TEXT,
  approved_by TEXT, approved_at REAL, finished_at REAL);
CREATE TABLE IF NOT EXISTS records(
  id INTEGER PRIMARY KEY AUTOINCREMENT, time REAL, action TEXT, path TEXT, project TEXT,
  backend TEXT, location TEXT, bytes INTEGER, archive_bytes INTEGER, members INTEGER,
  sha256 TEXT, plan TEXT, actor TEXT, note TEXT);
CREATE INDEX IF NOT EXISTS records_path ON records(path);
"""


def connect(cfg):
    state = cfg["state_dir"]
    os.makedirs(state, exist_ok=True)
    con = sqlite3.connect(os.path.join(state, "index.sqlite"), timeout=120)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(SCHEMA)
    return con


def latest_scan(con):
    return con.execute("SELECT * FROM scans WHERE status='done' ORDER BY id DESC LIMIT 1").fetchone()


def scan_near(con, ts):
    """The finished scan closest to (and not after) ts."""
    return con.execute("SELECT * FROM scans WHERE status='done' AND finished<=? ORDER BY finished DESC LIMIT 1",
                       (ts,)).fetchone()


def prune(con, keep):
    """Keep the directory sizes of the last `keep` scans; the bulky tables only for the latest one."""
    ids = [r[0] for r in con.execute("SELECT id FROM scans WHERE status='done' ORDER BY id DESC")]
    if not ids:
        return
    latest = ids[0]
    for table in ("links", "refs", "rrefs", "hits", "repos"):
        con.execute("DELETE FROM %s WHERE scan_id<>?" % table, (latest,))
    if len(ids) > keep:
        cut = ids[keep - 1]
        con.execute("DELETE FROM dirs WHERE scan_id<?", (cut,))
        con.execute("DELETE FROM scans WHERE id<? AND status='done'", (cut,))
    con.execute("DELETE FROM dirs WHERE scan_id IN (SELECT id FROM scans WHERE status<>'done' AND id<>?)",
                (latest,))
    con.commit()

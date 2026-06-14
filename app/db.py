import sqlite3
import json
import datetime
import os

from . import config as cfg


def get_conn():
    os.makedirs(cfg.DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(cfg.DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_conn()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS categories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            search_terms TEXT NOT NULL,
            prompt TEXT NOT NULL,
            enabled INTEGER DEFAULT 1,
            last_scanned TEXT,
            created_at TEXT
        );

        CREATE TABLE IF NOT EXISTS videos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category_id INTEGER NOT NULL,
            video_id TEXT NOT NULL,
            url TEXT,
            title TEXT,
            author TEXT,
            tags TEXT,
            status TEXT,
            reasoning TEXT,
            file_path TEXT,
            scraped_at TEXT,
            cached_at TEXT,
            thumbnail TEXT,
            UNIQUE(category_id, video_id)
        );

        CREATE TABLE IF NOT EXISTS logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT,
            level TEXT,
            message TEXT
        );

        CREATE TABLE IF NOT EXISTS category_examples (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category_id INTEGER NOT NULL,
            video_id TEXT,
            url TEXT,
            title TEXT,
            author TEXT,
            tags TEXT,
            thumbnail TEXT,
            label TEXT NOT NULL,
            added_at TEXT
        );
        """
    )
    # Migration: add cached_at if upgrading from an older schema
    cols = [r[1] for r in conn.execute("PRAGMA table_info(videos)").fetchall()]
    if "cached_at" not in cols:
        conn.execute("ALTER TABLE videos ADD COLUMN cached_at TEXT")
    if "thumbnail" not in cols:
        conn.execute("ALTER TABLE videos ADD COLUMN thumbnail TEXT")
    cat_cols = [r[1] for r in conn.execute("PRAGMA table_info(categories)").fetchall()]
    if "empty_scan_streak" not in cat_cols:
        conn.execute("ALTER TABLE categories ADD COLUMN empty_scan_streak INTEGER DEFAULT 0")

    # Indexes for the lookups done on every scan (video_exists, find_existing_file,
    # get_videos_needing_download) and for the cache-cleanup sweep.
    conn.executescript(
        """
        CREATE INDEX IF NOT EXISTS idx_videos_category_video ON videos(category_id, video_id);
        CREATE INDEX IF NOT EXISTS idx_videos_video_id ON videos(video_id);
        CREATE INDEX IF NOT EXISTS idx_videos_category_status ON videos(category_id, status);
        CREATE INDEX IF NOT EXISTS idx_videos_file_path ON videos(file_path);
        CREATE INDEX IF NOT EXISTS idx_videos_cached_at ON videos(cached_at);
        CREATE INDEX IF NOT EXISTS idx_category_examples_category ON category_examples(category_id);
        """
    )
    conn.commit()
    conn.close()


def log(level, message):
    conn = get_conn()
    conn.execute(
        "INSERT INTO logs (ts, level, message) VALUES (?,?,?)",
        (datetime.datetime.utcnow().isoformat(), level, str(message)),
    )
    conn.execute(
        "DELETE FROM logs WHERE id NOT IN (SELECT id FROM logs ORDER BY id DESC LIMIT 1000)"
    )
    conn.commit()
    conn.close()
    print(f"[{level}] {message}")


def get_logs(limit=200):
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM logs ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ---------------- Categories ----------------

def _row_to_category(r):
    d = dict(r)
    d["search_terms"] = json.loads(d["search_terms"])
    d["enabled"] = bool(d["enabled"])
    return d


def get_categories():
    conn = get_conn()
    rows = conn.execute("SELECT * FROM categories ORDER BY id").fetchall()
    conn.close()
    return [_row_to_category(r) for r in rows]


def get_category(cat_id):
    conn = get_conn()
    r = conn.execute("SELECT * FROM categories WHERE id=?", (cat_id,)).fetchone()
    conn.close()
    if not r:
        return None
    return _row_to_category(r)


def create_category(name, search_terms, prompt, enabled=True):
    conn = get_conn()
    conn.execute(
        "INSERT INTO categories (name, search_terms, prompt, enabled, created_at) VALUES (?,?,?,?,?)",
        (
            name,
            json.dumps(search_terms),
            prompt,
            int(enabled),
            datetime.datetime.utcnow().isoformat(),
        ),
    )
    conn.commit()
    cat_id = conn.execute(
        "SELECT id FROM categories WHERE name=?", (name,)
    ).fetchone()[0]
    conn.close()
    return cat_id


def update_category(cat_id, name, search_terms, prompt, enabled):
    conn = get_conn()
    conn.execute(
        "UPDATE categories SET name=?, search_terms=?, prompt=?, enabled=? WHERE id=?",
        (name, json.dumps(search_terms), prompt, int(enabled), cat_id),
    )
    conn.commit()
    conn.close()


def delete_category(cat_id):
    conn = get_conn()
    conn.execute("DELETE FROM categories WHERE id=?", (cat_id,))
    conn.execute("DELETE FROM videos WHERE category_id=?", (cat_id,))
    conn.execute("DELETE FROM category_examples WHERE category_id=?", (cat_id,))
    conn.commit()
    conn.close()


def set_last_scanned(cat_id, new_candidates_count=None):
    """Update last_scanned timestamp. If new_candidates_count is provided,
    also update the consecutive 'no new candidates found' streak, which is
    used to warn the user if discovery may have broken for this category."""
    conn = get_conn()
    if new_candidates_count is not None:
        if new_candidates_count == 0:
            conn.execute(
                "UPDATE categories SET last_scanned=?, empty_scan_streak=empty_scan_streak+1 WHERE id=?",
                (datetime.datetime.utcnow().isoformat(), cat_id),
            )
        else:
            conn.execute(
                "UPDATE categories SET last_scanned=?, empty_scan_streak=0 WHERE id=?",
                (datetime.datetime.utcnow().isoformat(), cat_id),
            )
    else:
        conn.execute(
            "UPDATE categories SET last_scanned=? WHERE id=?",
            (datetime.datetime.utcnow().isoformat(), cat_id),
        )
    conn.commit()
    conn.close()


# ---------------- Videos ----------------

def video_exists(cat_id, video_id):
    conn = get_conn()
    r = conn.execute(
        "SELECT 1 FROM videos WHERE category_id=? AND video_id=?", (cat_id, video_id)
    ).fetchone()
    conn.close()
    return r is not None


def insert_video(cat_id, video_id, url, title, author, tags, status, reasoning, file_path=None, thumbnail=None):
    conn = get_conn()
    try:
        conn.execute(
            """INSERT INTO videos
               (category_id, video_id, url, title, author, tags, status, reasoning, file_path, scraped_at, cached_at, thumbnail)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                cat_id,
                video_id,
                url,
                title,
                author,
                json.dumps(tags or []),
                status,
                reasoning,
                file_path,
                datetime.datetime.utcnow().isoformat(),
                datetime.datetime.utcnow().isoformat() if file_path else None,
                thumbnail,
            ),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        pass
    conn.close()


def get_video(video_row_id):
    conn = get_conn()
    row = conn.execute("SELECT * FROM videos WHERE id=?", (video_row_id,)).fetchone()
    conn.close()
    if not row:
        return None
    d = dict(row)
    d["tags"] = json.loads(d["tags"]) if d["tags"] else []
    return d


def get_expired_cached_videos(ttl_hours):
    cutoff = (datetime.datetime.utcnow() - datetime.timedelta(hours=ttl_hours)).isoformat()
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM videos WHERE file_path IS NOT NULL AND cached_at IS NOT NULL AND cached_at < ?",
        (cutoff,),
    ).fetchall()
    conn.close()
    out = []
    for r in rows:
        d = dict(r)
        d["tags"] = json.loads(d["tags"]) if d["tags"] else []
        out.append(d)
    return out


def file_path_in_use(file_path, exclude_video_row_id=None):
    """Check if any other video row (besides exclude_video_row_id) still
    references this file_path — used to avoid deleting a shared file."""
    conn = get_conn()
    if exclude_video_row_id is not None:
        row = conn.execute(
            "SELECT 1 FROM videos WHERE file_path=? AND id!=? LIMIT 1",
            (file_path, exclude_video_row_id),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT 1 FROM videos WHERE file_path=? LIMIT 1", (file_path,)
        ).fetchone()
    conn.close()
    return row is not None


def clear_video_cache(video_row_id):
    conn = get_conn()
    conn.execute(
        "UPDATE videos SET file_path=NULL, cached_at=NULL WHERE id=?",
        (video_row_id,),
    )
    conn.commit()
    conn.close()


def find_existing_file(video_id):
    """Return a file_path already cached on disk for this TikTok video_id
    in ANY category, if one exists. Used to avoid re-downloading the same
    video when it matches multiple categories."""
    conn = get_conn()
    row = conn.execute(
        "SELECT file_path FROM videos WHERE video_id=? AND file_path IS NOT NULL LIMIT 1",
        (video_id,),
    ).fetchone()
    conn.close()
    if row and row["file_path"] and os.path.isfile(row["file_path"]):
        return row["file_path"]
    return None


def get_videos_needing_download(cat_id):
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM videos WHERE category_id=? AND status='matched' AND file_path IS NULL",
        (cat_id,),
    ).fetchall()
    conn.close()
    out = []
    for r in rows:
        d = dict(r)
        d["tags"] = json.loads(d["tags"]) if d["tags"] else []
        out.append(d)
    return out


def update_video_download(video_row_id, file_path):
    conn = get_conn()
    conn.execute(
        "UPDATE videos SET file_path=?, cached_at=?, status='downloaded' WHERE id=?",
        (file_path, datetime.datetime.utcnow().isoformat(), video_row_id),
    )
    conn.commit()
    conn.close()


def get_videos(cat_id, status=None):
    conn = get_conn()
    if status:
        rows = conn.execute(
            "SELECT * FROM videos WHERE category_id=? AND status=? ORDER BY id DESC",
            (cat_id, status),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM videos WHERE category_id=? ORDER BY id DESC", (cat_id,)
        ).fetchall()
    conn.close()
    out = []
    for r in rows:
        d = dict(r)
        d["tags"] = json.loads(d["tags"]) if d["tags"] else []
        out.append(d)
    return out


# ---------------- Category examples (few-shot tuning) ----------------

def _row_to_example(r):
    d = dict(r)
    d["tags"] = json.loads(d["tags"]) if d["tags"] else []
    return d


def add_category_example(cat_id, meta, label):
    """Store an example video (with metadata) for a category, labeled
    'match' or 'no_match'. Used to give the LLM few-shot examples."""
    conn = get_conn()
    conn.execute(
        """INSERT INTO category_examples
           (category_id, video_id, url, title, author, tags, thumbnail, label, added_at)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (
            cat_id,
            meta.get("id"),
            meta.get("url"),
            meta.get("title"),
            meta.get("author"),
            json.dumps(meta.get("tags") or []),
            meta.get("thumbnail"),
            label,
            datetime.datetime.utcnow().isoformat(),
        ),
    )
    conn.commit()
    example_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.close()
    return example_id


def get_category_examples(cat_id):
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM category_examples WHERE category_id=? ORDER BY id", (cat_id,)
    ).fetchall()
    conn.close()
    return [_row_to_example(r) for r in rows]


def delete_category_example(example_id):
    conn = get_conn()
    conn.execute("DELETE FROM category_examples WHERE id=?", (example_id,))
    conn.commit()
    conn.close()


# ---------------- Stats ----------------

def get_stats():
    """Return aggregate stats for the dashboard charts."""
    conn = get_conn()

    # Per-category match counts
    cats = conn.execute("""
        SELECT c.id, c.name,
            SUM(CASE WHEN v.status IN ('matched','downloaded') THEN 1 ELSE 0 END) as matches,
            SUM(CASE WHEN v.status = 'rejected' THEN 1 ELSE 0 END) as rejected,
            COUNT(v.id) as total
        FROM categories c
        LEFT JOIN videos v ON v.category_id = c.id
        GROUP BY c.id
    """).fetchall()

    # Videos found per day (last 14 days)
    daily = conn.execute("""
        SELECT substr(scraped_at,1,10) as day, COUNT(*) as count
        FROM videos
        WHERE scraped_at >= date('now','-14 days')
        GROUP BY day
        ORDER BY day
    """).fetchall()

    # Status breakdown overall
    statuses = conn.execute("""
        SELECT status, COUNT(*) as count FROM videos GROUP BY status
    """).fetchall()

    conn.close()
    return {
        "categories": [dict(r) for r in cats],
        "daily": [dict(r) for r in daily],
        "statuses": [dict(r) for r in statuses],
    }


def search_videos(query: str, cat_id: int = None, status: str = None, limit: int = 100):
    """Full-text search across video title, author, tags, reasoning."""
    conn = get_conn()
    like = f"%{query}%"
    params = [like, like, like, like]
    base = """
        SELECT v.*, c.name as category_name
        FROM videos v
        JOIN categories c ON c.id = v.category_id
        WHERE (v.title LIKE ? OR v.author LIKE ? OR v.tags LIKE ? OR v.reasoning LIKE ?)
    """
    if cat_id:
        base += " AND v.category_id = ?"
        params.append(cat_id)
    if status:
        base += " AND v.status = ?"
        params.append(status)
    base += " ORDER BY v.id DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(base, params).fetchall()
    conn.close()
    out = []
    for r in rows:
        d = dict(r)
        d["tags"] = json.loads(d["tags"]) if d["tags"] else []
        out.append(d)
    return out

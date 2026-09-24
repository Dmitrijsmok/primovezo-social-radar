"""SQLite persistence. One file, no server, your data stays on your box.

The store is deliberately tiny: upsert mentions (de-duplicated by content id),
query them back with filters, and compute the aggregates the dashboard needs.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

from harken.auth import validate_role, validate_username
from harken.models import ConversationPost, Mention, Sentiment

_CREATE_MENTIONS = """
CREATE TABLE mentions (
    id            TEXT NOT NULL,
    source        TEXT NOT NULL,
    query         TEXT NOT NULL,
    author        TEXT,
    title         TEXT,
    text          TEXT,
    url           TEXT,
    created_at    TEXT NOT NULL,
    score         INTEGER,
    sentiment     TEXT,
    sentiment_score REAL,
    theme         TEXT,
    fetched_at    TEXT NOT NULL,
    PRIMARY KEY (id, query)
);
"""

_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_query ON mentions(query);
CREATE INDEX IF NOT EXISTS idx_created ON mentions(created_at);
"""

_ALERT_SCHEMA = """
CREATE TABLE IF NOT EXISTS alert_outbox (
    query         TEXT NOT NULL,
    mention_id    TEXT NOT NULL,
    target_key    TEXT NOT NULL,
    enqueued_at   TEXT NOT NULL,
    delivered_at TEXT,
    attempts      INTEGER NOT NULL DEFAULT 0,
    last_error    TEXT,
    PRIMARY KEY (query, mention_id, target_key)
);
CREATE INDEX IF NOT EXISTS idx_alert_pending
    ON alert_outbox(target_key, query, delivered_at, enqueued_at);
"""

_TRACKING_SCHEMA = """
CREATE TABLE IF NOT EXISTS tracked_queries (
    query           TEXT PRIMARY KEY,
    sources         TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    last_scanned_at TEXT
);
CREATE TABLE IF NOT EXISTS source_scan_state (
    query              TEXT NOT NULL,
    source             TEXT NOT NULL,
    newest_at          TEXT,
    oldest_at          TEXT,
    incremental_cursor TEXT,
    incremental_since  TEXT,
    backfill_cursor    TEXT,
    backfill_complete  INTEGER NOT NULL DEFAULT 0,
    last_success_at    TEXT,
    last_error         TEXT,
    PRIMARY KEY (query, source)
);
CREATE INDEX IF NOT EXISTS idx_scan_state_query ON source_scan_state(query);
"""

_THRESHOLD_ALERT_SCHEMA = """
CREATE TABLE IF NOT EXISTS threshold_alerts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    query        TEXT NOT NULL,
    event_type   TEXT NOT NULL,
    target_key   TEXT NOT NULL,
    text         TEXT NOT NULL,
    payload      TEXT NOT NULL,
    triggered_at TEXT NOT NULL,
    delivered_at TEXT,
    cleared_at   TEXT,
    attempts     INTEGER NOT NULL DEFAULT 0,
    last_error   TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_threshold_alert_active
    ON threshold_alerts(query, event_type, target_key)
    WHERE cleared_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_threshold_alert_pending
    ON threshold_alerts(target_key, delivered_at, cleared_at, triggered_at);
"""

_SOURCE_METRICS_SCHEMA = """
CREATE TABLE IF NOT EXISTS source_metrics (
    source                 TEXT PRIMARY KEY,
    scans_total            INTEGER NOT NULL DEFAULT 0,
    errors_total           INTEGER NOT NULL DEFAULT 0,
    fetched_total          INTEGER NOT NULL DEFAULT 0,
    pages_total            INTEGER NOT NULL DEFAULT 0,
    retries_total          INTEGER NOT NULL DEFAULT 0,
    duration_seconds_total REAL NOT NULL DEFAULT 0,
    last_duration_seconds  REAL NOT NULL DEFAULT 0,
    last_fetched           INTEGER NOT NULL DEFAULT 0,
    last_pages             INTEGER NOT NULL DEFAULT 0,
    last_retries           INTEGER NOT NULL DEFAULT 0,
    last_success           INTEGER NOT NULL DEFAULT 0,
    last_scan_at           TEXT NOT NULL,
    last_success_at        TEXT,
    last_error_at          TEXT,
    last_error             TEXT
);
"""

_LEAD_SCHEMA = """
CREATE TABLE IF NOT EXISTS lead_analysis (
    mention_id       TEXT NOT NULL,
    query            TEXT NOT NULL,
    relevant         INTEGER NOT NULL,
    score            INTEGER NOT NULL,
    category         TEXT NOT NULL,
    reason           TEXT NOT NULL,
    suggested_reply  TEXT,
    conversation     TEXT NOT NULL DEFAULT '[]',
    analyzed_at      TEXT NOT NULL,
    PRIMARY KEY (mention_id, query),
    FOREIGN KEY (mention_id, query) REFERENCES mentions(id, query) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_lead_analysis_relevance
    ON lead_analysis(query, relevant, score);
"""

DEFAULT_PROJECT_ID = 1

_PROJECT_SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL COLLATE NOCASE UNIQUE,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
INSERT OR IGNORE INTO projects (id, name, created_at, updated_at)
VALUES (1, 'Default', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
        strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));
CREATE TABLE IF NOT EXISTS project_queries (
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    query      TEXT NOT NULL REFERENCES tracked_queries(query) ON DELETE CASCADE,
    added_at   TEXT NOT NULL,
    PRIMARY KEY (project_id, query)
);
CREATE INDEX IF NOT EXISTS idx_project_queries_query ON project_queries(query);
"""

_AUTH_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT NOT NULL COLLATE NOCASE UNIQUE,
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL CHECK(role IN ('viewer', 'operator', 'admin')),
    active        INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    last_login_at TEXT
);
CREATE TABLE IF NOT EXISTS auth_sessions (
    token_hash TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_auth_sessions_expiry ON auth_sessions(expires_at);
CREATE INDEX IF NOT EXISTS idx_auth_sessions_user ON auth_sessions(user_id);
"""

# Bumped when the on-disk schema or a one-time reconciliation step changes.
# Stored in `PRAGMA user_version` so _ensure_schema() can skip the expensive
# whole-table reconciliation on every connection once a DB is up to date.
_SCHEMA_VERSION = 4


class Store:
    def __init__(self, path: str | Path = "harken.db"):
        raw_path = str(path)
        self.path = raw_path if raw_path == ":memory:" else str(Path(raw_path).expanduser())
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, timeout=30.0)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA busy_timeout = 30000")
        self._conn.execute("PRAGMA foreign_keys = ON")
        if self.path != ":memory:":
            self._conn.execute("PRAGMA journal_mode = WAL")
        self._ensure_schema()
        self._conn.commit()

    def _ensure_schema(self) -> None:
        with closing(self._conn.cursor()) as cur:
            cur.execute("PRAGMA user_version")
            if cur.fetchone()[0] >= _SCHEMA_VERSION:
                # Already reconciled; skip the whole-table GROUP BY scan that
                # would otherwise run on every connection (once per web request).
                return
            cur.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'projects'")
            had_project_schema = cur.fetchone() is not None
            cur.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'mentions'")
            if cur.fetchone() is None:
                cur.executescript(
                    _CREATE_MENTIONS
                    + _INDEXES
                    + _ALERT_SCHEMA
                    + _TRACKING_SCHEMA
                    + _THRESHOLD_ALERT_SCHEMA
                    + _SOURCE_METRICS_SCHEMA
                    + _LEAD_SCHEMA
                    + _PROJECT_SCHEMA
                    + _AUTH_SCHEMA
                )
                cur.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
                return

            cur.execute("PRAGMA table_info(mentions)")
            primary_key = [
                row["name"]
                for row in sorted(cur.fetchall(), key=lambda row: row["pk"])
                if row["pk"]
            ]
            if primary_key == ["id"]:
                # v0.1 stored one query directly on a globally unique mention,
                # so the same post could not belong to two tracked keywords.
                cur.executescript(
                    "BEGIN IMMEDIATE;\n"
                    "ALTER TABLE mentions RENAME TO mentions_v1;\n"
                    "DROP INDEX IF EXISTS idx_query;\n"
                    "DROP INDEX IF EXISTS idx_created;\n"
                    + _CREATE_MENTIONS
                    + "INSERT INTO mentions SELECT * FROM mentions_v1;\n"
                    "DROP TABLE mentions_v1;\n"
                    "COMMIT;\n"
                )
            elif primary_key != ["id", "query"]:
                raise RuntimeError(f"Unsupported Harken database schema: primary key {primary_key}")
            cur.executescript(
                _INDEXES
                + _ALERT_SCHEMA
                + _TRACKING_SCHEMA
                + _THRESHOLD_ALERT_SCHEMA
                + _SOURCE_METRICS_SCHEMA
                + _LEAD_SCHEMA
                + _PROJECT_SCHEMA
                + _AUTH_SCHEMA
            )
            cur.execute("PRAGMA table_info(lead_analysis)")
            lead_columns = {row["name"] for row in cur.fetchall()}
            if "conversation" not in lead_columns:
                cur.execute(
                    "ALTER TABLE lead_analysis ADD COLUMN conversation TEXT NOT NULL DEFAULT '[]'"
                )
            cur.execute(
                """
                INSERT OR IGNORE INTO tracked_queries
                    (query, sources, created_at, updated_at, last_scanned_at)
                SELECT query, '[]', MIN(fetched_at), MAX(fetched_at), MAX(fetched_at)
                FROM mentions GROUP BY query
                """
            )
            if not had_project_schema:
                cur.execute(
                    """
                    INSERT OR IGNORE INTO project_queries (project_id, query, added_at)
                    SELECT ?, query, COALESCE(created_at, updated_at)
                    FROM tracked_queries
                    """,
                    (DEFAULT_PROJECT_ID,),
                )
            cur.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")

    def close(self) -> None:
        self._conn.close()

    def check(self) -> None:
        """Raise if SQLite cannot execute a read and a small write transaction."""
        with closing(self._conn.cursor()) as cur:
            cur.execute("SELECT 1")
            cur.execute("BEGIN IMMEDIATE")
            cur.execute("ROLLBACK")

    def backup(self, destination: str | Path, *, overwrite: bool = False) -> Path:
        """Create a transactionally consistent SQLite backup."""
        if self.path == ":memory:":
            source = None
        else:
            source = Path(self.path).resolve()
        target = Path(destination).expanduser().resolve()
        if source is not None and target == source:
            raise ValueError("backup destination must differ from the active database")
        if target.exists() and not overwrite:
            raise FileExistsError(f"backup already exists: {target}")
        target.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(target)) as destination_conn:
            self._conn.backup(destination_conn)
        return target

    def count_before(self, cutoff: datetime, query: str | None = None) -> int:
        """Count mentions older than an absolute timestamp."""
        where, args = _before_filter(cutoff, query)
        with closing(self._conn.cursor()) as cur:
            cur.execute(f"SELECT COUNT(*) AS n FROM mentions{where}", args)
            return cur.fetchone()["n"]

    def delete_before(self, cutoff: datetime, query: str | None = None) -> int:
        """Delete old mentions and their alert state in one transaction."""
        where, args = _before_filter(cutoff, query)
        alert_where, alert_args = _before_filter(cutoff, query, table="m")
        with closing(self._conn.cursor()) as cur:
            cur.execute("BEGIN IMMEDIATE")
            cur.execute(
                f"""
                DELETE FROM alert_outbox
                WHERE EXISTS (
                    SELECT 1 FROM mentions AS m{alert_where}
                    AND m.query = alert_outbox.query AND m.id = alert_outbox.mention_id
                )
                """,
                alert_args,
            )
            cur.execute(f"DELETE FROM mentions{where}", args)
            deleted = max(cur.rowcount, 0)
            self._conn.commit()
        return deleted

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- local accounts -----------------------------------------------------
    def create_user(self, username: str, password_hash: str, role: str = "viewer") -> dict:
        username = validate_username(username)
        role = validate_role(role)
        now = datetime.now(timezone.utc).isoformat()
        try:
            with closing(self._conn.cursor()) as cur:
                cur.execute("BEGIN IMMEDIATE")
                cur.execute("SELECT COUNT(*) AS n FROM users WHERE active = 1")
                if cur.fetchone()["n"] == 0 and role != "admin":
                    raise ValueError("the first active user must be an admin")
                cur.execute(
                    """
                    INSERT INTO users (username, password_hash, role, active, created_at, updated_at)
                    VALUES (?, ?, ?, 1, ?, ?)
                    """,
                    (username, password_hash, role, now, now),
                )
                user_id = cur.lastrowid
            self._conn.commit()
        except sqlite3.IntegrityError as exc:
            self._conn.rollback()
            raise ValueError(f"user already exists: {username}") from exc
        except Exception:
            self._conn.rollback()
            raise
        user = self.user(user_id)
        if user is None:  # pragma: no cover - defensive after committed insert
            raise RuntimeError("created user could not be read")
        return user

    def users(self) -> list[dict]:
        with closing(self._conn.cursor()) as cur:
            cur.execute(
                """
                SELECT id, username, role, active, created_at, updated_at, last_login_at
                FROM users ORDER BY username COLLATE NOCASE
                """
            )
            return [_user_view(row) for row in cur.fetchall()]

    def user(self, user_id: int) -> dict | None:
        with closing(self._conn.cursor()) as cur:
            cur.execute(
                """
                SELECT id, username, role, active, created_at, updated_at, last_login_at
                FROM users WHERE id = ?
                """,
                (user_id,),
            )
            row = cur.fetchone()
            return _user_view(row) if row else None

    def user_for_login(self, username: str) -> dict | None:
        with closing(self._conn.cursor()) as cur:
            cur.execute(
                """
                SELECT id, username, password_hash, role, active,
                       created_at, updated_at, last_login_at
                FROM users WHERE username = ? COLLATE NOCASE
                """,
                (username.strip(),),
            )
            row = cur.fetchone()
            return dict(row) if row else None

    def set_user_password(self, user_id: int, password_hash: str) -> bool:
        now = datetime.now(timezone.utc).isoformat()
        with closing(self._conn.cursor()) as cur:
            cur.execute(
                "UPDATE users SET password_hash = ?, updated_at = ? WHERE id = ?",
                (password_hash, now, user_id),
            )
            updated = bool(cur.rowcount)
            if updated:
                cur.execute("DELETE FROM auth_sessions WHERE user_id = ?", (user_id,))
        self._conn.commit()
        return updated

    def set_user_role(self, user_id: int, role: str) -> bool:
        role = validate_role(role)
        now = datetime.now(timezone.utc).isoformat()
        try:
            with closing(self._conn.cursor()) as cur:
                cur.execute("BEGIN IMMEDIATE")
                cur.execute("SELECT role, active FROM users WHERE id = ?", (user_id,))
                current = cur.fetchone()
                if current is None:
                    self._conn.rollback()
                    return False
                if current["role"] == "admin" and current["active"] and role != "admin":
                    self._ensure_another_admin(cur, user_id)
                cur.execute(
                    "UPDATE users SET role = ?, updated_at = ? WHERE id = ?",
                    (role, now, user_id),
                )
            self._conn.commit()
            return True
        except Exception:
            self._conn.rollback()
            raise

    def set_user_active(self, user_id: int, active: bool) -> bool:
        now = datetime.now(timezone.utc).isoformat()
        try:
            with closing(self._conn.cursor()) as cur:
                cur.execute("BEGIN IMMEDIATE")
                cur.execute("SELECT role, active FROM users WHERE id = ?", (user_id,))
                current = cur.fetchone()
                if current is None:
                    self._conn.rollback()
                    return False
                if current["role"] == "admin" and current["active"] and not active:
                    self._ensure_another_admin(cur, user_id)
                cur.execute(
                    "UPDATE users SET active = ?, updated_at = ? WHERE id = ?",
                    (int(active), now, user_id),
                )
                if not active:
                    cur.execute("DELETE FROM auth_sessions WHERE user_id = ?", (user_id,))
            self._conn.commit()
            return True
        except Exception:
            self._conn.rollback()
            raise

    def delete_user(self, user_id: int) -> bool:
        try:
            with closing(self._conn.cursor()) as cur:
                cur.execute("BEGIN IMMEDIATE")
                cur.execute("SELECT role, active FROM users WHERE id = ?", (user_id,))
                current = cur.fetchone()
                if current is None:
                    self._conn.rollback()
                    return False
                if current["role"] == "admin" and current["active"]:
                    self._ensure_another_admin(cur, user_id)
                cur.execute("DELETE FROM users WHERE id = ?", (user_id,))
            self._conn.commit()
            return True
        except Exception:
            self._conn.rollback()
            raise

    def create_session(
        self,
        user_id: int,
        token_hash: str,
        expires_at: datetime,
        *,
        now: datetime | None = None,
    ) -> None:
        created = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
        expiry = expires_at.astimezone(timezone.utc).isoformat()
        with closing(self._conn.cursor()) as cur:
            cur.execute("DELETE FROM auth_sessions WHERE expires_at <= ?", (created,))
            cur.execute(
                """
                INSERT INTO auth_sessions
                    (token_hash, user_id, created_at, expires_at, last_seen_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (token_hash, user_id, created, expiry, created),
            )
            # Bound stolen/forgotten browser sessions per account.
            cur.execute(
                """
                DELETE FROM auth_sessions WHERE token_hash IN (
                    SELECT token_hash FROM auth_sessions WHERE user_id = ?
                    ORDER BY created_at DESC LIMIT -1 OFFSET 20
                )
                """,
                (user_id,),
            )
            cur.execute(
                "UPDATE users SET last_login_at = ?, updated_at = ? WHERE id = ?",
                (created, created, user_id),
            )
        self._conn.commit()

    def session_user(self, token_hash: str, *, now: datetime | None = None) -> dict | None:
        current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
        with closing(self._conn.cursor()) as cur:
            cur.execute(
                """
                SELECT u.id, u.username, u.role, u.active,
                       u.created_at, u.updated_at, u.last_login_at
                FROM auth_sessions AS s
                JOIN users AS u ON u.id = s.user_id
                WHERE s.token_hash = ? AND s.expires_at > ? AND u.active = 1
                """,
                (token_hash, current),
            )
            row = cur.fetchone()
        return _user_view(row) if row else None

    def delete_session(self, token_hash: str) -> None:
        with closing(self._conn.cursor()) as cur:
            cur.execute("DELETE FROM auth_sessions WHERE token_hash = ?", (token_hash,))
        self._conn.commit()

    @staticmethod
    def _ensure_another_admin(cur: sqlite3.Cursor, excluded_user_id: int) -> None:
        cur.execute(
            """
            SELECT COUNT(*) AS n FROM users
            WHERE role = 'admin' AND active = 1 AND id != ?
            """,
            (excluded_user_id,),
        )
        if cur.fetchone()["n"] == 0:
            raise ValueError("cannot remove or demote the last active admin")

    # -- writes --------------------------------------------------------------
    def upsert(self, mentions: list[Mention], *, update_theme: bool = True) -> int:
        """Insert or replace mentions. Returns count of *new* rows.

        ``update_theme=False`` leaves an existing row's ``theme`` untouched. Use
        it for the pre-cluster ingest of freshly fetched mentions (which carry no
        theme), so re-ingesting cannot transiently null an already-computed
        label. The post-cluster write uses the default so it can both set and
        *clear* labels (a mention that falls out of every cluster becomes NULL).
        """
        now = datetime.now(timezone.utc).isoformat()
        new = 0
        with closing(self._conn.cursor()) as cur:
            observed_sources: dict[str, list[str]] = {}
            for mention in mentions:
                sources = observed_sources.setdefault(mention.query, [])
                if mention.source not in sources:
                    sources.append(mention.source)
            for query, sources in observed_sources.items():
                cur.execute(
                    """
                    INSERT OR IGNORE INTO tracked_queries
                        (query, sources, created_at, updated_at, last_scanned_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (query, json.dumps(sources), now, now, now),
                )
                if cur.rowcount:
                    cur.execute(
                        """
                        INSERT OR IGNORE INTO project_queries (project_id, query, added_at)
                        VALUES (?, ?, ?)
                        """,
                        (DEFAULT_PROJECT_ID, query, now),
                    )
            # When update_theme is False the theme column is left out of the
            # UPDATE, preserving the stored label; otherwise it is set from the
            # incoming value (which may be NULL to clear a de-clustered label).
            theme_update = "theme=excluded.theme,\n                        " if update_theme else ""
            upsert_sql = f"""
                INSERT INTO mentions
                    (id, source, query, author, title, text, url, created_at,
                     score, sentiment, sentiment_score, theme, fetched_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id, query) DO UPDATE SET
                    source=excluded.source,
                    author=excluded.author,
                    title=excluded.title,
                    text=excluded.text,
                    url=excluded.url,
                    created_at=excluded.created_at,
                    sentiment=excluded.sentiment,
                    sentiment_score=excluded.sentiment_score,
                    {theme_update}score=excluded.score,
                    fetched_at=excluded.fetched_at
            """
            for m in mentions:
                cur.execute("SELECT 1 FROM mentions WHERE id = ? AND query = ?", (m.id, m.query))
                existed = cur.fetchone() is not None
                cur.execute(
                    upsert_sql,
                    (
                        m.id,
                        m.source,
                        m.query,
                        m.author,
                        m.title,
                        m.text,
                        m.url,
                        m.created_at.isoformat(),
                        m.score,
                        m.sentiment.value if m.sentiment else None,
                        m.sentiment_score,
                        m.theme,
                        now,
                    ),
                )
                if not existed:
                    new += 1
        self._conn.commit()
        return new

    def save_lead_analysis(self, mentions: list[Mention]) -> int:
        """Persist lead-radar enrichment for mentions that were successfully classified."""
        now = datetime.now(timezone.utc).isoformat()
        saved = 0
        with closing(self._conn.cursor()) as cur:
            for mention in mentions:
                if mention.lead_relevant is None or mention.lead_score is None:
                    continue
                cur.execute(
                    """
                    INSERT INTO lead_analysis
                        (mention_id, query, relevant, score, category, reason,
                         suggested_reply, conversation, analyzed_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(mention_id, query) DO UPDATE SET
                        relevant = excluded.relevant,
                        score = excluded.score,
                        category = excluded.category,
                        reason = excluded.reason,
                        suggested_reply = excluded.suggested_reply,
                        conversation = excluded.conversation,
                        analyzed_at = excluded.analyzed_at
                    """,
                    (
                        mention.id,
                        mention.query,
                        int(mention.lead_relevant),
                        mention.lead_score,
                        mention.lead_category or "other",
                        mention.lead_reason or "",
                        mention.suggested_reply,
                        json.dumps(
                            [item.model_dump(mode="json") for item in mention.conversation],
                            ensure_ascii=False,
                        ),
                        now,
                    ),
                )
                saved += 1
        self._conn.commit()
        return saved

    def lead_analysis(self, query: str, mention_id: str) -> dict | None:
        """Return persisted lead enrichment for one mention."""
        with closing(self._conn.cursor()) as cur:
            cur.execute(
                """
                SELECT relevant, score, category, reason, suggested_reply, conversation, analyzed_at
                FROM lead_analysis
                WHERE query = ? AND mention_id = ?
                """,
                (query, mention_id),
            )
            row = cur.fetchone()
        if row is None:
            return None
        value = dict(row)
        value["relevant"] = bool(value["relevant"])
        try:
            value["conversation"] = [
                ConversationPost.model_validate(item)
                for item in json.loads(value.get("conversation") or "[]")
            ]
        except (TypeError, ValueError, json.JSONDecodeError):
            value["conversation"] = []
        return value

    def unique_leads(
        self,
        min_score: int = 70,
        limit: int = 100,
        queries: list[str] | None = None,
    ) -> list[dict]:
        """Return de-duplicated qualified leads, keeping the strongest classification."""
        if not 0 <= min_score <= 100:
            raise ValueError("min_score must be between 0 and 100")
        if limit < 1:
            raise ValueError("limit must be at least 1")

        selected_queries = list(
            dict.fromkeys(query.strip() for query in queries or [] if query.strip())
        )
        where = "WHERE l.relevant = 1"
        args: list = []
        if queries is not None:
            if not selected_queries:
                return []
            placeholders = ",".join("?" for _ in selected_queries)
            where += f" AND m.query IN ({placeholders})"
            args.extend(selected_queries)

        with closing(self._conn.cursor()) as cur:
            cur.execute(
                f"""
                SELECT
                    m.source,
                    m.id,
                    m.query,
                    m.author,
                    m.title,
                    m.text,
                    m.url,
                    m.created_at,
                    l.score,
                    l.category,
                    l.reason,
                    l.suggested_reply,
                    l.analyzed_at
                FROM lead_analysis AS l
                JOIN mentions AS m
                  ON m.id = l.mention_id
                 AND m.query = l.query
                {where}
                ORDER BY
                    l.score DESC,
                    l.analyzed_at DESC,
                    m.created_at DESC,
                    m.source,
                    m.id,
                    m.query COLLATE NOCASE
                """,
                args,
            )
            rows = [dict(row) for row in cur.fetchall()]

        grouped: dict[tuple[str, str], dict] = {}
        for row in rows:
            key = (row["source"], row["id"])
            existing = grouped.get(key)
            if existing is None:
                existing = {
                    "source": row["source"],
                    "id": row["id"],
                    "author": row["author"],
                    "title": row["title"],
                    "text": row["text"],
                    "url": row["url"],
                    "created_at": row["created_at"],
                    "score": row["score"],
                    "category": row["category"],
                    "reason": row["reason"],
                    "suggested_reply": row["suggested_reply"],
                    "best_query": row["query"],
                    "matched_queries": [],
                    "analyzed_at": row["analyzed_at"],
                }
                grouped[key] = existing
            if row["query"] not in existing["matched_queries"]:
                existing["matched_queries"].append(row["query"])

        qualified = [lead for lead in grouped.values() if lead["score"] >= min_score]
        qualified.sort(
            key=lambda lead: (lead["score"], lead["analyzed_at"], lead["created_at"]),
            reverse=True,
        )
        return qualified[:limit]

    def save_tracking(
        self, query: str, sources: list[str], *, project_id: int | None = None
    ) -> None:
        """Persist a keyword even when a scan returns no mentions."""
        query = query.strip()
        normalized = list(
            dict.fromkeys(source.strip().lower() for source in sources if source.strip())
        )
        if not query:
            raise ValueError("query must not be empty")
        if not normalized:
            raise ValueError("at least one source must be configured")
        now = datetime.now(timezone.utc).isoformat()
        with closing(self._conn.cursor()) as cur:
            if project_id is not None:
                cur.execute("SELECT 1 FROM projects WHERE id = ?", (project_id,))
                if cur.fetchone() is None:
                    raise ValueError(f"unknown project: {project_id}")
            cur.execute(
                """
                INSERT INTO tracked_queries (query, sources, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(query) DO UPDATE SET
                    sources = excluded.sources,
                    updated_at = excluded.updated_at
                """,
                (query, json.dumps(normalized), now, now),
            )
            selected_project_id = project_id
            if selected_project_id is None:
                cur.execute("SELECT 1 FROM project_queries WHERE query = ? LIMIT 1", (query,))
                if cur.fetchone() is None:
                    selected_project_id = DEFAULT_PROJECT_ID
            if selected_project_id is not None:
                cur.execute(
                    """
                    INSERT OR IGNORE INTO project_queries (project_id, query, added_at)
                    VALUES (?, ?, ?)
                    """,
                    (selected_project_id, query, now),
                )
        self._conn.commit()

    def create_project(self, name: str) -> dict:
        """Create a named keyword group and return its persisted representation."""
        normalized = " ".join(name.split())
        if not normalized:
            raise ValueError("project name must not be empty")
        if len(normalized) > 100:
            raise ValueError("project name must be at most 100 characters")
        now = datetime.now(timezone.utc).isoformat()
        try:
            with closing(self._conn.cursor()) as cur:
                cur.execute(
                    "INSERT INTO projects (name, created_at, updated_at) VALUES (?, ?, ?)",
                    (normalized, now, now),
                )
                project_id = cur.lastrowid
            self._conn.commit()
        except sqlite3.IntegrityError as exc:
            self._conn.rollback()
            raise ValueError(f'a project named "{normalized}" already exists') from exc
        return self.project(project_id) or {}

    def add_query_to_project(self, project_id: int, query: str) -> bool:
        """Associate an existing tracked keyword with a project."""
        normalized = query.strip()
        if not normalized:
            raise ValueError("query must not be empty")
        now = datetime.now(timezone.utc).isoformat()
        with closing(self._conn.cursor()) as cur:
            cur.execute("SELECT 1 FROM projects WHERE id = ?", (project_id,))
            if cur.fetchone() is None:
                raise ValueError(f"unknown project: {project_id}")
            cur.execute("SELECT 1 FROM tracked_queries WHERE query = ?", (normalized,))
            if cur.fetchone() is None:
                raise ValueError(f'unknown tracked query: "{normalized}"')
            cur.execute(
                """
                INSERT OR IGNORE INTO project_queries (project_id, query, added_at)
                VALUES (?, ?, ?)
                """,
                (project_id, normalized, now),
            )
            added = bool(cur.rowcount)
            if added:
                cur.execute("UPDATE projects SET updated_at = ? WHERE id = ?", (now, project_id))
        self._conn.commit()
        return added

    def remove_query_from_project(self, project_id: int, query: str) -> bool:
        """Remove only the grouping; the tracked keyword and mentions remain intact."""
        if project_id == DEFAULT_PROJECT_ID:
            raise ValueError("keywords cannot be removed from the Default project")
        now = datetime.now(timezone.utc).isoformat()
        with closing(self._conn.cursor()) as cur:
            cur.execute(
                "DELETE FROM project_queries WHERE project_id = ? AND query = ?",
                (project_id, query.strip()),
            )
            removed = bool(cur.rowcount)
            if removed:
                cur.execute("UPDATE projects SET updated_at = ? WHERE id = ?", (now, project_id))
        self._conn.commit()
        return removed

    def delete_project(self, project_id: int) -> bool:
        """Delete a named grouping without deleting its keywords or mentions."""
        if project_id == DEFAULT_PROJECT_ID:
            raise ValueError("the Default project cannot be deleted")
        with closing(self._conn.cursor()) as cur:
            cur.execute("DELETE FROM projects WHERE id = ?", (project_id,))
            deleted = bool(cur.rowcount)
        self._conn.commit()
        return deleted

    def record_source_success(
        self,
        query: str,
        source: str,
        mentions: list[Mention],
        *,
        mode: str,
        next_cursor: str | None,
        incremental_since: datetime | None = None,
    ) -> None:
        """Advance a source cursor after its fetched rows have been committed."""
        state = self.source_state(query, source)
        observed = [mention.created_at.astimezone(timezone.utc).isoformat() for mention in mentions]
        newest_at = _latest_timestamp(state.get("newest_at"), max(observed, default=None))
        oldest_at = _earliest_timestamp(state.get("oldest_at"), min(observed, default=None))

        incremental_cursor = state.get("incremental_cursor")
        incremental_since_value = state.get("incremental_since")
        backfill_cursor = state.get("backfill_cursor")
        backfill_complete = bool(state.get("backfill_complete"))

        if mode == "backfill":
            backfill_cursor = next_cursor
            backfill_complete = next_cursor is None
        elif state.get("newest_at") is None:
            # The first recent scan establishes both temporal bounds. Its next
            # page is historical, so it seeds the backfill cursor rather than
            # the forward/incremental continuation.
            backfill_cursor = next_cursor
            backfill_complete = next_cursor is None
            incremental_cursor = None
            incremental_since_value = None
        else:
            incremental_cursor = next_cursor
            incremental_since_value = (
                incremental_since.astimezone(timezone.utc).isoformat()
                if next_cursor and incremental_since
                else None
            )

        now = datetime.now(timezone.utc).isoformat()
        with closing(self._conn.cursor()) as cur:
            cur.execute(
                """
                INSERT INTO source_scan_state
                    (query, source, newest_at, oldest_at, incremental_cursor,
                     incremental_since, backfill_cursor, backfill_complete,
                     last_success_at, last_error)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                ON CONFLICT(query, source) DO UPDATE SET
                    newest_at = excluded.newest_at,
                    oldest_at = excluded.oldest_at,
                    incremental_cursor = excluded.incremental_cursor,
                    incremental_since = excluded.incremental_since,
                    backfill_cursor = excluded.backfill_cursor,
                    backfill_complete = excluded.backfill_complete,
                    last_success_at = excluded.last_success_at,
                    last_error = NULL
                """,
                (
                    query,
                    source,
                    newest_at,
                    oldest_at,
                    incremental_cursor,
                    incremental_since_value,
                    backfill_cursor,
                    int(backfill_complete),
                    now,
                ),
            )
            cur.execute(
                "UPDATE tracked_queries SET last_scanned_at = ?, updated_at = ? WHERE query = ?",
                (now, now, query),
            )
        self._conn.commit()

    def record_source_error(self, query: str, source: str, error: str) -> None:
        """Persist a safe, inspectable source error without moving any cursor."""
        with closing(self._conn.cursor()) as cur:
            cur.execute(
                """
                INSERT INTO source_scan_state (query, source, last_error)
                VALUES (?, ?, ?)
                ON CONFLICT(query, source) DO UPDATE SET last_error = excluded.last_error
                """,
                (query, source, error[:500]),
            )
        self._conn.commit()

    def record_source_metric(
        self,
        source: str,
        *,
        duration_seconds: float,
        fetched: int,
        pages: int,
        retries: int,
        error: str | None = None,
        now: datetime | None = None,
    ) -> None:
        """Atomically accumulate low-cardinality source telemetry."""
        if duration_seconds < 0 or min(fetched, pages, retries) < 0:
            raise ValueError("source metric values must not be negative")
        observed_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
        succeeded = error is None
        with closing(self._conn.cursor()) as cur:
            cur.execute(
                """
                INSERT INTO source_metrics
                    (source, scans_total, errors_total, fetched_total, pages_total,
                     retries_total, duration_seconds_total, last_duration_seconds,
                     last_fetched, last_pages, last_retries, last_success, last_scan_at,
                     last_success_at, last_error_at, last_error)
                VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source) DO UPDATE SET
                    scans_total = source_metrics.scans_total + 1,
                    errors_total = source_metrics.errors_total + excluded.errors_total,
                    fetched_total = source_metrics.fetched_total + excluded.fetched_total,
                    pages_total = source_metrics.pages_total + excluded.pages_total,
                    retries_total = source_metrics.retries_total + excluded.retries_total,
                    duration_seconds_total =
                        source_metrics.duration_seconds_total + excluded.duration_seconds_total,
                    last_duration_seconds = excluded.last_duration_seconds,
                    last_fetched = excluded.last_fetched,
                    last_pages = excluded.last_pages,
                    last_retries = excluded.last_retries,
                    last_success = excluded.last_success,
                    last_scan_at = excluded.last_scan_at,
                    last_success_at = COALESCE(
                        excluded.last_success_at, source_metrics.last_success_at
                    ),
                    last_error_at = COALESCE(excluded.last_error_at, source_metrics.last_error_at),
                    last_error = excluded.last_error
                """,
                (
                    source,
                    int(not succeeded),
                    fetched,
                    pages,
                    retries,
                    duration_seconds,
                    duration_seconds,
                    fetched,
                    pages,
                    retries,
                    int(succeeded),
                    observed_at,
                    observed_at if succeeded else None,
                    observed_at if not succeeded else None,
                    error[:500] if error else None,
                ),
            )
        self._conn.commit()

    def source_metrics(self) -> list[dict]:
        """Return durable per-source counters in stable label order."""
        with closing(self._conn.cursor()) as cur:
            cur.execute("SELECT * FROM source_metrics ORDER BY source")
            return [dict(row) for row in cur.fetchall()]

    def existing_ids(self, query: str, ids: list[str]) -> set[str]:
        """Return IDs already stored for a query, chunked below SQLite's variable limit."""
        unique = list(dict.fromkeys(ids))
        found: set[str] = set()
        with closing(self._conn.cursor()) as cur:
            for start in range(0, len(unique), 900):
                chunk = unique[start : start + 900]
                if not chunk:
                    continue
                placeholders = ",".join("?" for _ in chunk)
                cur.execute(
                    f"SELECT id FROM mentions WHERE query = ? AND id IN ({placeholders})",
                    [query, *chunk],
                )
                found.update(row["id"] for row in cur.fetchall())
        return found

    def enqueue_alerts(
        self,
        mentions: list[Mention],
        target_key: str,
        *,
        dedupe_across_queries: bool = False,
    ) -> int:
        """Add mentions to the durable delivery outbox. Returns newly queued rows.

        Lead-radar targets can opt into cross-query de-duplication so one source
        post matching several tracked keywords is delivered only once to the
        same destination. Standard Harken alerts keep their per-query behavior.
        """
        now = datetime.now(timezone.utc).isoformat()
        queued = 0
        with closing(self._conn.cursor()) as cur:
            for mention in mentions:
                if dedupe_across_queries:
                    cur.execute(
                        """
                        SELECT 1 FROM alert_outbox
                        WHERE mention_id = ? AND target_key = ?
                        LIMIT 1
                        """,
                        (mention.id, target_key),
                    )
                    if cur.fetchone() is not None:
                        continue
                cur.execute(
                    """
                    INSERT OR IGNORE INTO alert_outbox
                        (query, mention_id, target_key, enqueued_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (mention.query, mention.id, target_key, now),
                )
                queued += max(cur.rowcount, 0)
        self._conn.commit()
        return queued

    def pending_alerts(self, query: str, target_key: str, limit: int = 100) -> list[Mention]:
        """Return undelivered mentions for a target, oldest enqueued first."""
        if limit < 1:
            raise ValueError("limit must be at least 1")
        with closing(self._conn.cursor()) as cur:
            cur.execute(
                """
                SELECT m.*,
                       l.relevant AS lead_relevant,
                       l.score AS lead_score,
                       l.category AS lead_category,
                       l.reason AS lead_reason,
                       l.suggested_reply AS suggested_reply,
                       l.conversation AS lead_conversation
                FROM alert_outbox AS a
                JOIN mentions AS m ON m.query = a.query AND m.id = a.mention_id
                LEFT JOIN lead_analysis AS l
                  ON l.query = m.query AND l.mention_id = m.id
                WHERE a.query = ? AND a.target_key = ? AND a.delivered_at IS NULL
                ORDER BY a.enqueued_at, a.mention_id
                LIMIT ?
                """,
                (query, target_key, limit),
            )
            return [_row_to_mention(row) for row in cur.fetchall()]

    def pending_alerts_for_target(self, target_key: str, limit: int = 100) -> list[Mention]:
        """Return undelivered mentions across queries for one delivery target."""
        if limit < 1:
            raise ValueError("limit must be at least 1")
        with closing(self._conn.cursor()) as cur:
            cur.execute(
                """
                SELECT m.*,
                       l.relevant AS lead_relevant,
                       l.score AS lead_score,
                       l.category AS lead_category,
                       l.reason AS lead_reason,
                       l.suggested_reply AS suggested_reply,
                       l.conversation AS lead_conversation
                FROM alert_outbox AS a
                JOIN mentions AS m ON m.query = a.query AND m.id = a.mention_id
                LEFT JOIN lead_analysis AS l
                  ON l.query = m.query AND l.mention_id = m.id
                WHERE a.target_key = ? AND a.delivered_at IS NULL
                ORDER BY a.enqueued_at, a.mention_id
                LIMIT ?
                """,
                (target_key, limit),
            )
            return [_row_to_mention(row) for row in cur.fetchall()]

    def mark_alerts_delivered(self, query: str, ids: list[str], target_key: str) -> None:
        """Mark one successfully delivered batch."""
        self._update_alert_batch(query, ids, target_key, delivered=True)

    def mark_alerts_failed(self, query: str, ids: list[str], target_key: str, error: str) -> None:
        """Record a sanitized error while leaving the batch pending for retry."""
        self._update_alert_batch(query, ids, target_key, delivered=False, error=error[:500])

    def pending_alert_count(self, query: str, target_key: str) -> int:
        with closing(self._conn.cursor()) as cur:
            cur.execute(
                """
                SELECT COUNT(*) AS n FROM alert_outbox
                WHERE query = ? AND target_key = ? AND delivered_at IS NULL
                """,
                (query, target_key),
            )
            return cur.fetchone()["n"]

    def activate_threshold_alert(
        self,
        query: str,
        event_type: str,
        target_key: str,
        text: str,
        payload: dict,
        *,
        cooldown_hours: int,
        now: datetime | None = None,
    ) -> int | None:
        """Open one alert episode, or reuse the active episode without duplicating it."""
        current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        current_iso = current.isoformat()
        with closing(self._conn.cursor()) as cur:
            cur.execute(
                """
                SELECT id, delivered_at FROM threshold_alerts
                WHERE query = ? AND event_type = ? AND target_key = ? AND cleared_at IS NULL
                """,
                (query, event_type, target_key),
            )
            active = cur.fetchone()
            if active is not None:
                if active["delivered_at"] is None:
                    cur.execute(
                        "UPDATE threshold_alerts SET text = ?, payload = ? WHERE id = ?",
                        (text, json.dumps(payload), active["id"]),
                    )
                    self._conn.commit()
                return active["id"]

            if cooldown_hours:
                cur.execute(
                    """
                    SELECT MAX(delivered_at) AS delivered_at FROM threshold_alerts
                    WHERE query = ? AND event_type = ? AND target_key = ?
                    """,
                    (query, event_type, target_key),
                )
                delivered_at = cur.fetchone()["delivered_at"]
                if delivered_at:
                    last_delivery = datetime.fromisoformat(delivered_at)
                    if current - last_delivery < timedelta(hours=cooldown_hours):
                        return None

            cur.execute(
                """
                INSERT INTO threshold_alerts
                    (query, event_type, target_key, text, payload, triggered_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (query, event_type, target_key, text, json.dumps(payload), current_iso),
            )
            alert_id = cur.lastrowid
        self._conn.commit()
        return alert_id

    def clear_threshold_alert(
        self, query: str, event_type: str, target_key: str, *, now: datetime | None = None
    ) -> None:
        """Clear an active episode so a future crossing can re-arm it."""
        cleared_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
        with closing(self._conn.cursor()) as cur:
            cur.execute(
                """
                UPDATE threshold_alerts SET cleared_at = ?
                WHERE query = ? AND event_type = ? AND target_key = ? AND cleared_at IS NULL
                """,
                (cleared_at, query, event_type, target_key),
            )
        self._conn.commit()

    def pending_threshold_alerts(self, query: str, target_key: str) -> list[dict]:
        with closing(self._conn.cursor()) as cur:
            cur.execute(
                """
                SELECT * FROM threshold_alerts
                WHERE query = ? AND target_key = ?
                  AND delivered_at IS NULL AND cleared_at IS NULL
                ORDER BY triggered_at, id
                """,
                (query, target_key),
            )
            rows = [dict(row) for row in cur.fetchall()]
        for row in rows:
            row["payload"] = json.loads(row["payload"])
        return rows

    def mark_threshold_alert_delivered(self, alert_id: int, *, now: datetime | None = None) -> None:
        self._mark_threshold_alert(alert_id, delivered=True, now=now)

    def mark_threshold_alert_failed(self, alert_id: int, error: str) -> None:
        self._mark_threshold_alert(alert_id, delivered=False, error=error[:500])

    def threshold_alert_pending_count(self, query: str, target_key: str) -> int:
        with closing(self._conn.cursor()) as cur:
            cur.execute(
                """
                SELECT COUNT(*) AS n FROM threshold_alerts
                WHERE query = ? AND target_key = ?
                  AND delivered_at IS NULL AND cleared_at IS NULL
                """,
                (query, target_key),
            )
            return cur.fetchone()["n"]

    def operational_stats(self) -> dict[str, int]:
        """Small persisted counters suitable for health dashboards and metrics."""
        with closing(self._conn.cursor()) as cur:
            cur.execute("SELECT COUNT(*) AS n FROM mentions")
            mentions = cur.fetchone()["n"]
            cur.execute("SELECT COUNT(*) AS n FROM tracked_queries")
            queries = cur.fetchone()["n"]
            cur.execute("SELECT COUNT(*) AS n FROM alert_outbox WHERE delivered_at IS NULL")
            alerts_pending = cur.fetchone()["n"]
            cur.execute("SELECT COUNT(*) AS n FROM alert_outbox WHERE delivered_at IS NOT NULL")
            alerts_delivered = cur.fetchone()["n"]
            cur.execute(
                """
                SELECT COUNT(*) AS n FROM threshold_alerts
                WHERE delivered_at IS NULL AND cleared_at IS NULL
                """
            )
            threshold_alerts_pending = cur.fetchone()["n"]
            cur.execute("SELECT COUNT(*) AS n FROM threshold_alerts WHERE delivered_at IS NOT NULL")
            threshold_alerts_delivered = cur.fetchone()["n"]
        return {
            "mentions": mentions,
            "queries": queries,
            "alerts_pending": alerts_pending,
            "alerts_delivered": alerts_delivered,
            "threshold_alerts_pending": threshold_alerts_pending,
            "threshold_alerts_delivered": threshold_alerts_delivered,
        }

    def _mark_threshold_alert(
        self,
        alert_id: int,
        *,
        delivered: bool,
        error: str | None = None,
        now: datetime | None = None,
    ) -> None:
        delivered_at = (
            (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
            if delivered
            else None
        )
        with closing(self._conn.cursor()) as cur:
            cur.execute(
                """
                UPDATE threshold_alerts
                SET attempts = attempts + 1, delivered_at = ?, last_error = ?
                WHERE id = ? AND delivered_at IS NULL AND cleared_at IS NULL
                """,
                (delivered_at, None if delivered else error, alert_id),
            )
        self._conn.commit()

    def _update_alert_batch(
        self,
        query: str,
        ids: list[str],
        target_key: str,
        *,
        delivered: bool,
        error: str | None = None,
    ) -> None:
        unique = list(dict.fromkeys(ids))
        now = datetime.now(timezone.utc).isoformat()
        with closing(self._conn.cursor()) as cur:
            for start in range(0, len(unique), 900):
                chunk = unique[start : start + 900]
                if not chunk:
                    continue
                placeholders = ",".join("?" for _ in chunk)
                delivered_at = now if delivered else None
                cur.execute(
                    f"""
                    UPDATE alert_outbox
                    SET attempts = attempts + 1, delivered_at = ?, last_error = ?
                    WHERE query = ? AND target_key = ? AND delivered_at IS NULL
                      AND mention_id IN ({placeholders})
                    """,
                    [delivered_at, None if delivered else error, query, target_key, *chunk],
                )
        self._conn.commit()

    # -- reads ---------------------------------------------------------------
    def projects(self) -> list[dict]:
        """List project groups with keyword and mention totals."""
        with closing(self._conn.cursor()) as cur:
            cur.execute(
                """
                SELECT p.id, p.name, p.created_at, p.updated_at,
                       COUNT(DISTINCT pq.query) AS query_count,
                       COUNT(m.id) AS mention_count
                FROM projects AS p
                LEFT JOIN project_queries AS pq ON pq.project_id = p.id
                LEFT JOIN mentions AS m ON m.query = pq.query
                GROUP BY p.id
                ORDER BY CASE WHEN p.id = ? THEN 0 ELSE 1 END,
                         p.name COLLATE NOCASE
                """,
                (DEFAULT_PROJECT_ID,),
            )
            return [dict(row) for row in cur.fetchall()]

    def project(self, project_id: int) -> dict | None:
        """Return one project and its ordered member keywords."""
        with closing(self._conn.cursor()) as cur:
            cur.execute(
                """
                SELECT p.id, p.name, p.created_at, p.updated_at,
                       COUNT(DISTINCT pq.query) AS query_count,
                       COUNT(m.id) AS mention_count
                FROM projects AS p
                LEFT JOIN project_queries AS pq ON pq.project_id = p.id
                LEFT JOIN mentions AS m ON m.query = pq.query
                WHERE p.id = ?
                GROUP BY p.id
                """,
                (project_id,),
            )
            row = cur.fetchone()
        if row is None:
            return None
        value = dict(row)
        value["queries"] = self.queries(project_id=project_id)
        return value

    def alert_metrics(
        self,
        query: str,
        *,
        now: datetime | None = None,
        window_hours: int = 24,
        baseline_windows: int = 7,
    ) -> dict:
        """Current-window volume/sentiment and the preceding complete-window baseline."""
        if window_hours < 1 or baseline_windows < 1:
            raise ValueError("alert windows must be at least 1")
        current_time = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        current_start = current_time - timedelta(hours=window_hours)
        baseline_start = current_start - timedelta(hours=window_hours * baseline_windows)
        with closing(self._conn.cursor()) as cur:
            cur.execute(
                """
                SELECT created_at, sentiment FROM mentions
                WHERE query = ? AND datetime(created_at) >= datetime(?)
                  AND datetime(created_at) <= datetime(?)
                """,
                (query, baseline_start.isoformat(), current_time.isoformat()),
            )
            rows = cur.fetchall()

        current_sentiments: list[str | None] = []
        baseline_sentiments: list[str | None] = []
        for row in rows:
            created_at = datetime.fromisoformat(row["created_at"])
            if created_at >= current_start:
                current_sentiments.append(row["sentiment"])
            else:
                baseline_sentiments.append(row["sentiment"])
        baseline_count = len(baseline_sentiments)
        return {
            "current_count": len(current_sentiments),
            "baseline_count": baseline_count,
            "baseline_average": baseline_count / baseline_windows,
            "current_net_sentiment": _net_sentiment(current_sentiments),
            "baseline_net_sentiment": _net_sentiment(baseline_sentiments),
        }

    def tracking(self, query: str) -> dict | None:
        with closing(self._conn.cursor()) as cur:
            cur.execute("SELECT * FROM tracked_queries WHERE query = ?", (query,))
            row = cur.fetchone()
        if row is None:
            return None
        value = dict(row)
        try:
            value["sources"] = json.loads(value["sources"])
        except (TypeError, json.JSONDecodeError):
            value["sources"] = []
        return value

    def source_state(self, query: str, source: str) -> dict:
        with closing(self._conn.cursor()) as cur:
            cur.execute(
                "SELECT * FROM source_scan_state WHERE query = ? AND source = ?",
                (query, source),
            )
            row = cur.fetchone()
        return dict(row) if row is not None else {}

    def source_states(self, query: str) -> list[dict]:
        with closing(self._conn.cursor()) as cur:
            cur.execute("SELECT * FROM source_scan_state WHERE query = ? ORDER BY source", (query,))
            return [dict(row) for row in cur.fetchall()]

    def mentions(
        self,
        query: str | None = None,
        project_id: int | None = None,
        source: str | None = None,
        sentiment: Sentiment | None = None,
        limit: int | None = 500,
    ) -> list[Mention]:
        if limit is not None and limit < 1:
            raise ValueError("limit must be at least 1")
        if query and project_id is not None:
            raise ValueError("query and project_id are mutually exclusive")
        sql = "SELECT * FROM mentions WHERE 1=1"
        args: list = []
        if query:
            sql += " AND query = ?"
            args.append(query)
        if project_id is not None:
            sql += " AND query IN (SELECT query FROM project_queries WHERE project_id = ?)"
            args.append(project_id)
        if source:
            sql += " AND source = ?"
            args.append(source)
        if sentiment:
            sql += " AND sentiment = ?"
            args.append(sentiment.value)
        sql += " ORDER BY created_at DESC"
        if limit is not None:
            sql += " LIMIT ?"
            args.append(limit)
        with closing(self._conn.cursor()) as cur:
            cur.execute(sql, args)
            return [_row_to_mention(r) for r in cur.fetchall()]

    def queries(self, project_id: int | None = None) -> list[str]:
        with closing(self._conn.cursor()) as cur:
            sql = """
                SELECT t.query
                FROM tracked_queries AS t
                LEFT JOIN mentions AS m ON m.query = t.query
                """
            args: list = []
            if project_id is not None:
                sql += " JOIN project_queries AS pq ON pq.query = t.query AND pq.project_id = ?"
                args.append(project_id)
            sql += """
                GROUP BY t.query
                ORDER BY COALESCE(MAX(m.created_at), t.updated_at) DESC,
                         t.query COLLATE NOCASE
                """
            cur.execute(sql, args)
            return [r["query"] for r in cur.fetchall()]

    def summary(self, query: str | None = None, project_id: int | None = None) -> dict:
        where, args = _scope_filter(query, project_id)
        with closing(self._conn.cursor()) as cur:
            cur.execute(f"SELECT COUNT(*) AS n FROM mentions{where}", args)
            total = cur.fetchone()["n"]
            cur.execute(
                f"SELECT COALESCE(sentiment, 'neutral') AS label, COUNT(*) AS n "
                f"FROM mentions{where} GROUP BY label ORDER BY n DESC, label",
                args,
            )
            by_sentiment = {row["label"]: row["n"] for row in cur.fetchall()}
            cur.execute(
                f"SELECT source, COUNT(*) AS n FROM mentions{where} "
                "GROUP BY source ORDER BY n DESC, source",
                args,
            )
            by_source = {row["source"]: row["n"] for row in cur.fetchall()}
            cur.execute(
                f"SELECT substr(created_at, 1, 10) AS day, COUNT(*) AS n "
                f"FROM mentions{where} GROUP BY day ORDER BY day",
                args,
            )
            by_day = {row["day"]: row["n"] for row in cur.fetchall()}
        return {
            "total": total,
            "by_sentiment": by_sentiment,
            "by_source": by_source,
            "by_day": by_day,
        }

    def timeseries(self, query: str | None = None, project_id: int | None = None) -> list[dict]:
        """Per-day sentiment breakdown, oldest first, for the trend chart.

        Each entry: ``{"date": "YYYY-MM-DD", "positive": n, "neutral": n,
        "negative": n, "total": n}``.
        """
        where, args = _scope_filter(query, project_id)
        with closing(self._conn.cursor()) as cur:
            cur.execute(
                f"""
                SELECT substr(created_at, 1, 10) AS date,
                       COALESCE(SUM(sentiment = 'positive'), 0) AS positive,
                       COALESCE(SUM(sentiment = 'negative'), 0) AS negative,
                       COALESCE(SUM(sentiment = 'neutral' OR sentiment IS NULL), 0) AS neutral,
                       COUNT(*) AS total
                FROM mentions{where}
                GROUP BY date
                ORDER BY date
                """,
                args,
            )
            return [dict(row) for row in cur.fetchall()]

    def themes(
        self,
        query: str | None = None,
        project_id: int | None = None,
        limit: int = 8,
    ) -> list[dict]:
        """Theme counts over the complete result set, not only the visible feed page."""
        if limit < 1:
            raise ValueError("limit must be at least 1")
        where, args = _scope_filter(query, project_id)
        condition = "theme IS NOT NULL AND theme != ''"
        sql = f"SELECT theme AS label, COUNT(*) AS count FROM mentions{where}"
        sql += (" AND " if where else " WHERE ") + condition
        sql += " GROUP BY theme ORDER BY count DESC, theme LIMIT ?"
        with closing(self._conn.cursor()) as cur:
            cur.execute(sql, [*args, limit])
            return [dict(row) for row in cur.fetchall()]

    def net_sentiment(self, query: str | None = None, project_id: int | None = None) -> float:
        """Net sentiment in [-1, 1]: (positive - negative) / total."""
        s = self.summary(query=query, project_id=project_id)
        bs = s["by_sentiment"]
        pos, neg = bs.get("positive", 0), bs.get("negative", 0)
        total = s["total"] or 1
        return round((pos - neg) / total, 3)


def _scope_filter(query: str | None, project_id: int | None) -> tuple[str, list]:
    if query and project_id is not None:
        raise ValueError("query and project_id are mutually exclusive")
    if query:
        return " WHERE query = ?", [query]
    if project_id is not None:
        return (
            " WHERE query IN (SELECT query FROM project_queries WHERE project_id = ?)",
            [project_id],
        )
    return "", []


def _before_filter(
    cutoff: datetime, query: str | None, table: str | None = None
) -> tuple[str, list[str]]:
    prefix = f"{table}." if table else ""
    where = f" WHERE datetime({prefix}created_at) < datetime(?)"
    args = [cutoff.isoformat()]
    if query:
        where += f" AND {prefix}query = ?"
        args.append(query)
    return where, args


def _row_to_mention(r: sqlite3.Row) -> Mention:
    keys = set(r.keys())
    lead_relevant = None
    if "lead_relevant" in keys and r["lead_relevant"] is not None:
        lead_relevant = bool(r["lead_relevant"])
    conversation: list[ConversationPost] = []
    if "lead_conversation" in keys and r["lead_conversation"]:
        try:
            conversation = [
                ConversationPost.model_validate(item) for item in json.loads(r["lead_conversation"])
            ]
        except (TypeError, ValueError, json.JSONDecodeError):
            conversation = []
    return Mention(
        id=r["id"],
        source=r["source"],
        query=r["query"],
        author=r["author"],
        title=r["title"],
        text=r["text"] or "",
        url=r["url"],
        created_at=datetime.fromisoformat(r["created_at"]),
        score=r["score"],
        sentiment=Sentiment(r["sentiment"]) if r["sentiment"] else None,
        sentiment_score=r["sentiment_score"],
        theme=r["theme"],
        lead_relevant=lead_relevant,
        lead_score=r["lead_score"] if "lead_score" in keys else None,
        lead_category=r["lead_category"] if "lead_category" in keys else None,
        lead_reason=r["lead_reason"] if "lead_reason" in keys else None,
        suggested_reply=r["suggested_reply"] if "suggested_reply" in keys else None,
        conversation=conversation,
    )


def _user_view(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "username": row["username"],
        "role": row["role"],
        "active": bool(row["active"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "last_login_at": row["last_login_at"],
    }


def _latest_timestamp(left: str | None, right: str | None) -> str | None:
    values = [value for value in (left, right) if value]
    return max(values, key=datetime.fromisoformat) if values else None


def _earliest_timestamp(left: str | None, right: str | None) -> str | None:
    values = [value for value in (left, right) if value]
    return min(values, key=datetime.fromisoformat) if values else None


def _net_sentiment(sentiments: list[str | None]) -> float | None:
    if not sentiments:
        return None
    positive = sum(value == Sentiment.POSITIVE.value for value in sentiments)
    negative = sum(value == Sentiment.NEGATIVE.value for value in sentiments)
    return round((positive - negative) / len(sentiments), 3)

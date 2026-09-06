from __future__ import annotations

import sqlite3
import statistics
import hashlib
import json
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from pathlib import Path


class Database:
    GLOBAL_SETTING_KEYS = {
        "speed_rule_mode", "speed_min_channels", "missed_min_publishers",
        "miss_grace_minutes", "speed_rank_weight", "speed_time_cap_minutes",
        "speed_confidence_k", "similarity_threshold", "match_window_hours",
        "match_fragment_min_words", "match_fragment_threshold",
        "match_numeric_boost",
    }
    def __init__(self, path: Path, owner_id: int | None = None):
        self.owner_id = owner_id
        self._user_id = ContextVar("database_user_id", default=owner_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript("""
        CREATE TABLE IF NOT EXISTS posts (
            id INTEGER PRIMARY KEY, channel TEXT NOT NULL, message_id INTEGER NOT NULL,
            published_at TEXT NOT NULL, text TEXT NOT NULL, normalized TEXT NOT NULL,
            link TEXT NOT NULL, cluster_id INTEGER,
            reaction_count INTEGER NOT NULL DEFAULT 0,
            forward_count INTEGER NOT NULL DEFAULT 0,
            UNIQUE(channel, message_id)
        );
        CREATE TABLE IF NOT EXISTS clusters (
            id INTEGER PRIMARY KEY, representative TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_posts_cluster ON posts(cluster_id);
        CREATE INDEX IF NOT EXISTS idx_posts_time ON posts(published_at);
        CREATE INDEX IF NOT EXISTS idx_posts_channel_time_lower
            ON posts(lower(channel), published_at);
        CREATE TABLE IF NOT EXISTS watched_channels (
            channel TEXT PRIMARY KEY, title TEXT, added_at TEXT NOT NULL,
            last_message_id INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS viral_channels (
            channel TEXT PRIMARY KEY, title TEXT, added_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS proofreading_channels (
            source_channel TEXT PRIMARY KEY, source_title TEXT,
            destination_chat_id INTEGER NOT NULL, destination_title TEXT,
            destination_invite TEXT NOT NULL DEFAULT '', added_at TEXT NOT NULL,
            last_message_id INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY, value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS bot_users (
            user_id INTEGER PRIMARY KEY, authorized_at TEXT NOT NULL, is_owner INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS access_invites (
            code_hash TEXT PRIMARY KEY, created_at TEXT NOT NULL,
            used_by INTEGER, used_at TEXT
        );
        CREATE TABLE IF NOT EXISTS user_settings (
            user_id INTEGER NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL,
            PRIMARY KEY(user_id, key)
        );
        CREATE TABLE IF NOT EXISTS user_watched_channels (
            user_id INTEGER NOT NULL, channel TEXT NOT NULL, title TEXT, added_at TEXT NOT NULL,
            last_message_id INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(user_id, channel)
        );
        CREATE TABLE IF NOT EXISTS user_viral_channels (
            user_id INTEGER NOT NULL, channel TEXT NOT NULL, title TEXT, added_at TEXT NOT NULL,
            PRIMARY KEY(user_id, channel)
        );
        CREATE TABLE IF NOT EXISTS user_proofreading_channels (
            user_id INTEGER NOT NULL, source_channel TEXT NOT NULL, source_title TEXT,
            destination_chat_id INTEGER NOT NULL, destination_title TEXT,
            destination_invite TEXT NOT NULL DEFAULT '', added_at TEXT NOT NULL,
            last_message_id INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(user_id, source_channel)
        );
        CREATE TABLE IF NOT EXISTS analysis_channels (
            source_channel TEXT PRIMARY KEY, source_title TEXT,
            destination_chat_id INTEGER NOT NULL, destination_title TEXT,
            destination_invite TEXT NOT NULL DEFAULT '', ai_enabled INTEGER NOT NULL DEFAULT 0,
            added_at TEXT NOT NULL, last_message_id INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS user_analysis_channels (
            user_id INTEGER NOT NULL, source_channel TEXT NOT NULL, source_title TEXT,
            destination_chat_id INTEGER NOT NULL, destination_title TEXT,
            destination_invite TEXT NOT NULL DEFAULT '', ai_enabled INTEGER NOT NULL DEFAULT 0,
            added_at TEXT NOT NULL, last_message_id INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(user_id, source_channel)
        );
        CREATE TABLE IF NOT EXISTS proofreading_posts (
            source_channel TEXT NOT NULL, message_id INTEGER NOT NULL,
            published_at TEXT NOT NULL, text TEXT NOT NULL, normalized TEXT NOT NULL,
            link TEXT NOT NULL, PRIMARY KEY(source_channel, message_id)
        );
        CREATE INDEX IF NOT EXISTS idx_proofreading_posts_time ON proofreading_posts(published_at);
        CREATE TABLE IF NOT EXISTS viral_alerts (
            channel TEXT NOT NULL, message_id INTEGER NOT NULL,
            alerted_at TEXT NOT NULL,
            PRIMARY KEY(channel, message_id)
        );
        CREATE TABLE IF NOT EXISTS interaction_snapshots (
            channel TEXT NOT NULL, message_id INTEGER NOT NULL,
            phase_minutes INTEGER NOT NULL,
            reaction_count INTEGER NOT NULL, forward_count INTEGER NOT NULL,
            captured_at TEXT NOT NULL,
            PRIMARY KEY(channel, message_id, phase_minutes)
        );
        CREATE INDEX IF NOT EXISTS idx_interaction_snapshots_baseline
            ON interaction_snapshots(channel, phase_minutes, captured_at);
        CREATE INDEX IF NOT EXISTS idx_interaction_snapshots_baseline_lower
            ON interaction_snapshots(lower(channel), phase_minutes, captured_at DESC);
        CREATE INDEX IF NOT EXISTS idx_viral_channels_lower
            ON viral_channels(lower(channel));
        CREATE INDEX IF NOT EXISTS idx_viral_alerts_channel_lower
            ON viral_alerts(lower(channel), message_id);
        CREATE TABLE IF NOT EXISTS viral_baselines (
            channel TEXT NOT NULL, phase_minutes INTEGER NOT NULL,
            median_reactions REAL NOT NULL, median_forwards REAL NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(channel, phase_minutes)
        );
        """)
        self._ensure_column("posts", "reaction_count", "INTEGER NOT NULL DEFAULT 0")
        self._ensure_column("posts", "forward_count", "INTEGER NOT NULL DEFAULT 0")
        self._ensure_column("posts", "media_type", "TEXT NOT NULL DEFAULT 'text'")
        self._ensure_column("posts", "view_count", "INTEGER NOT NULL DEFAULT 0")
        self._ensure_column("posts", "reply_count", "INTEGER NOT NULL DEFAULT 0")
        self._ensure_column("posts", "reaction_breakdown", "TEXT NOT NULL DEFAULT '{}'")
        self._ensure_column("viral_alerts", "destination_message_id", "INTEGER")
        if owner_id is not None:
            with self.conn:
                self.conn.execute(
                    "INSERT OR IGNORE INTO bot_users(user_id,authorized_at,is_owner) VALUES(?,?,1)",
                    (owner_id, datetime.now(timezone.utc).isoformat()),
                )
                # Upgrade an existing authorized owner row created by older
                # versions so owner-only controls remain available.
                self.conn.execute(
                    "UPDATE bot_users SET is_owner=1 WHERE user_id=?", (owner_id,)
                )

    def use_user(self, user_id: int) -> None:
        self._user_id.set(user_id)

    def current_user_id(self) -> int | None:
        return self._user_id.get()

    def authorize_user(self, user_id: int) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT OR IGNORE INTO bot_users(user_id,authorized_at,is_owner) VALUES(?,?,0)",
                (user_id, datetime.now(timezone.utc).isoformat()),
            )

    def create_access_invite(self, code: str) -> None:
        code_hash = hashlib.sha256(code.encode("utf-8")).hexdigest()
        with self.conn:
            self.conn.execute(
                "INSERT INTO access_invites(code_hash,created_at) VALUES(?,?)",
                (code_hash, datetime.now(timezone.utc).isoformat()),
            )

    def consume_access_invite(self, code: str, user_id: int) -> bool:
        code_hash = hashlib.sha256(code.encode("utf-8")).hexdigest()
        now = datetime.now(timezone.utc).isoformat()
        with self.conn:
            claimed = self.conn.execute(
                "UPDATE access_invites SET used_by=?,used_at=? WHERE code_hash=? AND used_by IS NULL",
                (user_id, now, code_hash),
            )
            if claimed.rowcount != 1:
                return False
            self.conn.execute(
                "INSERT OR IGNORE INTO bot_users(user_id,authorized_at,is_owner) VALUES(?,?,0)",
                (user_id, now),
            )
        return True

    def is_authorized(self, user_id: int) -> bool:
        return self.conn.execute("SELECT 1 FROM bot_users WHERE user_id=?", (user_id,)).fetchone() is not None

    def is_owner(self, user_id: int) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM bot_users WHERE user_id=? AND is_owner=1", (user_id,)
        ).fetchone() is not None

    def authorized_users(self):
        return self.conn.execute("SELECT * FROM bot_users ORDER BY authorized_at").fetchall()

    def revoke_user(self, user_id: int) -> bool:
        """Revoke a user's access; the owner can never be removed."""
        if user_id == self.owner_id:
            return False
        with self.conn:
            result = self.conn.execute("DELETE FROM bot_users WHERE user_id=? AND is_owner=0", (user_id,))
        return result.rowcount == 1

    def _ensure_column(self, table: str, column: str, definition: str) -> None:
        columns = {row["name"] for row in self.conn.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            with self.conn:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def close(self) -> None:
        self.conn.close()

    def recent_clusters(self, hours: int):
        since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        return self.conn.execute(
            "SELECT c.id, c.representative FROM clusters c WHERE c.created_at >= ? ORDER BY c.id DESC", (since,)
        ).fetchall()

    def add_post(self, channel: str, message_id: int, published_at: datetime, text: str,
                 normalized: str, link: str, cluster_id: int | None) -> bool:
        try:
            with self.conn:
                self.conn.execute(
                    "INSERT INTO posts(channel,message_id,published_at,text,normalized,link,cluster_id) VALUES(?,?,?,?,?,?,?)",
                    (channel, message_id, published_at.astimezone(timezone.utc).isoformat(), text, normalized, link, cluster_id),
                )
            return True
        except sqlite3.IntegrityError:
            return False

    def create_cluster(self, representative: str, created_at: datetime) -> int:
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO clusters(representative,created_at) VALUES(?,?)",
                (representative, created_at.astimezone(timezone.utc).isoformat()),
            )
        return int(cur.lastrowid)

    def attach_cluster(self, channel: str, message_id: int, cluster_id: int) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE posts SET cluster_id=? WHERE channel=? AND message_id=?", (cluster_id, channel, message_id)
            )

    def posts_between(self, start: datetime, end: datetime):
        return self.conn.execute(
            "SELECT * FROM posts WHERE published_at>=? AND published_at<? ORDER BY published_at",
            (start.astimezone(timezone.utc).isoformat(), end.astimezone(timezone.utc).isoformat()),
        ).fetchall()

    def channel_posts_between(self, channel: str, start: datetime, end: datetime):
        return self.conn.execute(
            "SELECT * FROM posts WHERE lower(channel)=lower(?) AND published_at>=? AND published_at<? ORDER BY published_at",
            (channel, start.astimezone(timezone.utc).isoformat(), end.astimezone(timezone.utc).isoformat()),
        ).fetchall()

    def upsert_interaction_post(self, channel: str, message_id: int, published_at: datetime,
                                text: str, link: str, reaction_count: int,
                                forward_count: int, media_type: str = "text",
                                view_count: int = 0, reply_count: int = 0,
                                reaction_breakdown: dict | None = None) -> None:
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO posts(channel,message_id,published_at,text,normalized,link,cluster_id,
                                  reaction_count,forward_count,media_type,view_count,reply_count,reaction_breakdown)
                VALUES(?,?,?,?,?,?,NULL,?,?,?,?,?,?)
                ON CONFLICT(channel,message_id) DO UPDATE SET
                    text=excluded.text, link=excluded.link,
                    reaction_count=excluded.reaction_count,
                    forward_count=excluded.forward_count,
                    media_type=excluded.media_type,
                    view_count=excluded.view_count,
                    reply_count=excluded.reply_count,
                    reaction_breakdown=excluded.reaction_breakdown
                """,
                (channel, message_id, published_at.astimezone(timezone.utc).isoformat(), text, "", link,
                 max(0, reaction_count), max(0, forward_count), media_type or "text",
                 max(0, view_count), max(0, reply_count),
                 json.dumps(reaction_breakdown or {}, ensure_ascii=False)),
            )

    def posts_due_for_snapshot(self, now: datetime, phase_minutes: int, grace_minutes: int = 5):
        """Posts whose age is inside the capture window for a viral scan phase."""
        latest_publish = (now.astimezone(timezone.utc) - timedelta(minutes=phase_minutes)).isoformat()
        earliest_publish = (now.astimezone(timezone.utc) - timedelta(minutes=phase_minutes + grace_minutes)).isoformat()
        return self.conn.execute(
            """
            SELECT p.* FROM posts p
            JOIN viral_channels v ON lower(v.channel)=lower(p.channel)
            LEFT JOIN interaction_snapshots s
              ON lower(s.channel)=lower(p.channel) AND s.message_id=p.message_id AND s.phase_minutes=?
            LEFT JOIN viral_alerts a
              ON lower(a.channel)=lower(p.channel) AND a.message_id=p.message_id
            WHERE p.published_at>? AND p.published_at<=?
              AND s.message_id IS NULL AND a.message_id IS NULL
            ORDER BY p.published_at
            """,
            (phase_minutes, earliest_publish, latest_publish),
        ).fetchall()

    def interaction_baseline(self, channel: str, phase_minutes: int, sample_size: int = 30):
        """Return the shared cached median for a channel/phase.

        Baselines are global by channel, not per user. Once a complete baseline
        exists, all users must reuse it instead of recalculating the median.
        """
        cached = self.conn.execute(
            """
            SELECT median_reactions, median_forwards FROM viral_baselines
            WHERE lower(channel)=lower(?) AND phase_minutes=?
            ORDER BY updated_at DESC LIMIT 1
            """,
            (channel, phase_minutes),
        ).fetchone()
        if cached is not None:
            return cached["median_reactions"], cached["median_forwards"], sample_size

        rows = self.conn.execute(
            """
            SELECT reaction_count, forward_count FROM interaction_snapshots
            WHERE lower(channel)=lower(?) AND phase_minutes=?
            ORDER BY captured_at DESC LIMIT ?
            """,
            (channel, phase_minutes, sample_size),
        ).fetchall()
        if len(rows) >= sample_size:
            median_reactions = float(statistics.median(row["reaction_count"] for row in rows))
            median_forwards = float(statistics.median(row["forward_count"] for row in rows))
            with self.conn:
                self.conn.execute(
                    """
                    INSERT INTO viral_baselines
                        (channel,phase_minutes,median_reactions,median_forwards,updated_at)
                    VALUES(?,?,?,?,?)
                    ON CONFLICT(channel,phase_minutes) DO UPDATE SET
                        median_reactions=excluded.median_reactions,
                        median_forwards=excluded.median_forwards,
                        updated_at=excluded.updated_at
                    """,
                    (channel, phase_minutes, median_reactions, median_forwards,
                     datetime.now(timezone.utc).isoformat()),
                )
            return median_reactions, median_forwards, sample_size

        if not rows:
            return None
        return 0.0, 0.0, len(rows)

    def interaction_history(self, channel: str, phase_minutes: int,
                            sample_size: int = 60) -> tuple[list[int], list[int]]:
        """Recent same-channel/same-age counts used for percentile scoring."""
        rows = self.conn.execute(
            """
            SELECT reaction_count, forward_count FROM interaction_snapshots
            WHERE lower(channel)=lower(?) AND phase_minutes=?
            ORDER BY captured_at DESC LIMIT ?
            """,
            (channel, phase_minutes, sample_size),
        ).fetchall()
        return (
            [max(0, int(row["reaction_count"] or 0)) for row in rows],
            [max(0, int(row["forward_count"] or 0)) for row in rows],
        )

    def save_interaction_snapshot(self, channel: str, message_id: int, phase_minutes: int,
                                  reaction_count: int, forward_count: int,
                                  captured_at: datetime) -> None:
        with self.conn:
            self.conn.execute(
                """
                INSERT OR IGNORE INTO interaction_snapshots
                    (channel,message_id,phase_minutes,reaction_count,forward_count,captured_at)
                VALUES(?,?,?,?,?,?)
                """,
                (channel, message_id, phase_minutes, max(0, reaction_count), max(0, forward_count),
                 captured_at.astimezone(timezone.utc).isoformat()),
            )

    def recent_viral_alert_candidates(self, since: datetime):
        return self.conn.execute(
            """
            SELECT a.channel, a.message_id, a.destination_message_id, a.alerted_at,
                   p.text, p.link
            FROM viral_alerts a
            JOIN posts p
              ON lower(p.channel)=lower(a.channel) AND p.message_id=a.message_id
            WHERE a.alerted_at>=? AND a.destination_message_id IS NOT NULL
            ORDER BY a.alerted_at
            """,
            (since.astimezone(timezone.utc).isoformat(),),
        ).fetchall()

    def mark_viral_alerted(self, channel: str, message_id: int,
                           destination_message_id: int | None = None) -> None:
        with self.conn:
            self.conn.execute(
                """
                INSERT OR IGNORE INTO viral_alerts
                    (channel,message_id,alerted_at,destination_message_id)
                VALUES(?,?,?,?)
                """,
                (channel, message_id, datetime.now(timezone.utc).isoformat(), destination_message_id),
            )

    def add_channel(self, channel: str, title: str = "") -> bool:
        user_id = self.current_user_id()
        if user_id != self.owner_id:
            try:
                with self.conn:
                    self.conn.execute(
                        "INSERT INTO user_watched_channels(user_id,channel,title,added_at) VALUES(?,?,?,?)",
                        (user_id, channel, title, datetime.now(timezone.utc).isoformat()),
                    )
                return True
            except sqlite3.IntegrityError:
                return False
        try:
            with self.conn:
                self.conn.execute(
                    "INSERT INTO watched_channels(channel,title,added_at) VALUES(?,?,?)",
                    (channel, title, datetime.now(timezone.utc).isoformat()),
                )
            return True
        except sqlite3.IntegrityError:
            return False

    def remove_channel(self, channel: str) -> bool:
        user_id = self.current_user_id()
        if user_id != self.owner_id:
            with self.conn:
                cur = self.conn.execute("DELETE FROM user_watched_channels WHERE user_id=? AND lower(channel)=lower(?)", (user_id, channel))
            return cur.rowcount > 0
        with self.conn:
            cur = self.conn.execute("DELETE FROM watched_channels WHERE lower(channel)=lower(?)", (channel,))
        return cur.rowcount > 0

    def add_viral_channel(self, channel: str, title: str = "") -> bool:
        user_id = self.current_user_id()
        if user_id != self.owner_id:
            try:
                with self.conn:
                    self.conn.execute("INSERT INTO user_viral_channels(user_id,channel,title,added_at) VALUES(?,?,?,?)", (user_id, channel, title, datetime.now(timezone.utc).isoformat()))
                return True
            except sqlite3.IntegrityError:
                return False
        try:
            with self.conn:
                self.conn.execute(
                    "INSERT INTO viral_channels(channel,title,added_at) VALUES(?,?,?)",
                    (channel, title, datetime.now(timezone.utc).isoformat()),
                )
            return True
        except sqlite3.IntegrityError:
            return False

    def remove_viral_channel(self, channel: str) -> bool:
        user_id = self.current_user_id()
        if user_id != self.owner_id:
            with self.conn:
                cur = self.conn.execute("DELETE FROM user_viral_channels WHERE user_id=? AND lower(channel)=lower(?)", (user_id, channel))
            return cur.rowcount > 0
        with self.conn:
            cur = self.conn.execute("DELETE FROM viral_channels WHERE lower(channel)=lower(?)", (channel,))
        return cur.rowcount > 0

    def viral_channels(self):
        user_id = self.current_user_id()
        if user_id != self.owner_id:
            return self.conn.execute("SELECT * FROM user_viral_channels WHERE user_id=? ORDER BY added_at", (user_id,)).fetchall()
        return self.conn.execute("SELECT * FROM viral_channels ORDER BY added_at").fetchall()

    def add_proofreading_channel(self, source_channel: str, source_title: str,
                                 destination_chat_id: int, destination_title: str = "",
                                 destination_invite: str = "") -> bool:
        user_id = self.current_user_id()
        if user_id != self.owner_id:
            if self.conn.execute("SELECT count(*) FROM user_proofreading_channels WHERE user_id=?", (user_id,)).fetchone()[0] >= 5:
                return False
            try:
                with self.conn:
                    self.conn.execute("""INSERT INTO user_proofreading_channels
                        (user_id,source_channel,source_title,destination_chat_id,destination_title,destination_invite,added_at)
                        VALUES(?,?,?,?,?,?,?)""", (user_id, source_channel, source_title, destination_chat_id, destination_title, destination_invite, datetime.now(timezone.utc).isoformat()))
                return True
            except sqlite3.IntegrityError:
                return False
        if self.conn.execute("SELECT count(*) FROM proofreading_channels").fetchone()[0] >= 5:
            return False
        try:
            with self.conn:
                self.conn.execute(
                    """INSERT INTO proofreading_channels
                       (source_channel,source_title,destination_chat_id,destination_title,destination_invite,added_at)
                       VALUES(?,?,?,?,?,?)""",
                    (source_channel, source_title, destination_chat_id, destination_title,
                     destination_invite, datetime.now(timezone.utc).isoformat()),
                )
            return True
        except sqlite3.IntegrityError:
            return False

    def add_analysis_channel(self, source_channel: str, source_title: str,
                             destination_chat_id: int, destination_title: str = "",
                             destination_invite: str = "") -> str:
        user_id = self.current_user_id()
        table = "user_analysis_channels" if user_id != self.owner_id else "analysis_channels"
        count_params = (user_id,) if user_id != self.owner_id else ()
        count_where = "user_id=?" if user_id != self.owner_id else "1=1"
        if self.conn.execute(f"SELECT count(*) FROM {table} WHERE {count_where}", count_params).fetchone()[0] >= 5:
            return "limit"
        params = (user_id, source_channel, source_title, destination_chat_id, destination_title,
                  destination_invite, datetime.now(timezone.utc).isoformat()) if user_id != self.owner_id else (
                      source_channel, source_title, destination_chat_id, destination_title,
                      destination_invite, datetime.now(timezone.utc).isoformat())
        try:
            with self.conn:
                if user_id != self.owner_id:
                    self.conn.execute(
                        f"""INSERT INTO {table}
                        (user_id,source_channel,source_title,destination_chat_id,destination_title,destination_invite,added_at)
                        VALUES(?,?,?,?,?,?,?)""", params)
                else:
                    self.conn.execute(
                        f"""INSERT INTO {table}
                        (source_channel,source_title,destination_chat_id,destination_title,destination_invite,added_at)
                        VALUES(?,?,?,?,?,?)""", params)
            return "added"
        except sqlite3.IntegrityError:
            return "duplicate"

    def remove_analysis_channel(self, source_channel: str) -> bool:
        user_id = self.current_user_id()
        table = "user_analysis_channels" if user_id != self.owner_id else "analysis_channels"
        where = "user_id=? AND lower(source_channel)=lower(?)" if user_id != self.owner_id else "lower(source_channel)=lower(?)"
        params = (user_id, source_channel) if user_id != self.owner_id else (source_channel,)
        with self.conn:
            cur = self.conn.execute(f"DELETE FROM {table} WHERE {where}", params)
        return cur.rowcount > 0

    def analysis_channels(self):
        user_id = self.current_user_id()
        if user_id != self.owner_id:
            return self.conn.execute("SELECT * FROM user_analysis_channels WHERE user_id=? ORDER BY added_at", (user_id,)).fetchall()
        return self.conn.execute("SELECT * FROM analysis_channels ORDER BY added_at").fetchall()

    def all_analysis_channels(self):
        return self.conn.execute("""SELECT ? AS user_id,source_channel,source_title,destination_chat_id,
            destination_title,destination_invite,ai_enabled,added_at,last_message_id
            FROM analysis_channels CROSS JOIN (SELECT NULL AS user_id)
            UNION ALL
            SELECT user_id,source_channel,source_title,destination_chat_id,
            destination_title,destination_invite,ai_enabled,added_at,last_message_id
            FROM user_analysis_channels ORDER BY added_at""", (self.owner_id,)).fetchall()

    def set_analysis_ai(self, source_channel: str, enabled: bool) -> bool:
        user_id = self.current_user_id()
        table = "user_analysis_channels" if user_id != self.owner_id else "analysis_channels"
        where = "user_id=? AND lower(source_channel)=lower(?)" if user_id != self.owner_id else "lower(source_channel)=lower(?)"
        params = (1 if enabled else 0, user_id, source_channel) if user_id != self.owner_id else (1 if enabled else 0, source_channel)
        with self.conn:
            cur = self.conn.execute(f"UPDATE {table} SET ai_enabled=? WHERE {where}", params)
        return cur.rowcount > 0

    def count_analysis_ai(self) -> int:
        user_id = self.current_user_id()
        table = "user_analysis_channels" if user_id != self.owner_id else "analysis_channels"
        where = "user_id=?" if user_id != self.owner_id else "1=1"
        params = (user_id,) if user_id != self.owner_id else ()
        return int(self.conn.execute(f"SELECT count(*) FROM {table} WHERE {where} AND ai_enabled=1", params).fetchone()[0])

    def remove_proofreading_channel(self, source_channel: str) -> bool:
        user_id = self.current_user_id()
        if user_id != self.owner_id:
            with self.conn:
                cur = self.conn.execute("DELETE FROM user_proofreading_channels WHERE user_id=? AND lower(source_channel)=lower(?)", (user_id, source_channel))
            return cur.rowcount > 0
        with self.conn:
            cur = self.conn.execute(
                "DELETE FROM proofreading_channels WHERE lower(source_channel)=lower(?)",
                (source_channel,),
            )
        return cur.rowcount > 0

    def proofreading_channels(self):
        user_id = self.current_user_id()
        if user_id != self.owner_id:
            return self.conn.execute("SELECT * FROM user_proofreading_channels WHERE user_id=? ORDER BY added_at", (user_id,)).fetchall()
        return self.conn.execute("SELECT * FROM proofreading_channels ORDER BY added_at").fetchall()

    def update_proofreading_cursor(self, source_channel: str, message_id: int,
                                   user_id: int | None = None) -> None:
        with self.conn:
            if user_id is None or user_id == self.owner_id:
                self.conn.execute(
                    """UPDATE proofreading_channels
                       SET last_message_id=max(last_message_id, ?) WHERE lower(source_channel)=lower(?)""",
                    (message_id, source_channel),
                )
            else:
                self.conn.execute(
                    """UPDATE user_proofreading_channels SET last_message_id=max(last_message_id, ?)
                       WHERE user_id=? AND lower(source_channel)=lower(?)""",
                    (message_id, user_id, source_channel),
                )

    def recent_proofreading_posts(self, since: datetime):
        return self.conn.execute(
            "SELECT * FROM proofreading_posts WHERE published_at>=? ORDER BY published_at",
            (since.astimezone(timezone.utc).isoformat(),),
        ).fetchall()

    def add_proofreading_post(self, source_channel: str, message_id: int,
                              published_at: datetime, text: str, normalized: str, link: str) -> bool:
        with self.conn:
            cur = self.conn.execute(
                """INSERT OR IGNORE INTO proofreading_posts
                   (source_channel,message_id,published_at,text,normalized,link)
                   VALUES(?,?,?,?,?,?)""",
                (source_channel, message_id, published_at.astimezone(timezone.utc).isoformat(),
                 text, normalized, link),
            )
        return cur.rowcount > 0
    def channels(self):
        user_id = self.current_user_id()
        if user_id != self.owner_id:
            return self.conn.execute("SELECT * FROM user_watched_channels WHERE user_id=? ORDER BY added_at", (user_id,)).fetchall()
        return self.conn.execute("SELECT * FROM watched_channels ORDER BY added_at").fetchall()

    def all_channels(self):
        return self.conn.execute("""SELECT channel,title,added_at,last_message_id FROM watched_channels
            UNION SELECT channel,title,added_at,last_message_id FROM user_watched_channels
            UNION SELECT source_channel AS channel,source_title AS title,added_at,last_message_id FROM analysis_channels
            UNION SELECT source_channel AS channel,source_title AS title,added_at,last_message_id FROM user_analysis_channels
            ORDER BY added_at""").fetchall()

    def all_proofreading_channels(self):
        return self.conn.execute("""SELECT ? AS user_id,source_channel,source_title,destination_chat_id,destination_title,destination_invite,added_at,last_message_id FROM proofreading_channels
            UNION ALL SELECT user_id,source_channel,source_title,destination_chat_id,destination_title,destination_invite,added_at,last_message_id
            FROM user_proofreading_channels ORDER BY added_at""", (self.owner_id,)).fetchall()

    def update_channel_cursor(self, channel: str, message_id: int) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE watched_channels SET last_message_id=max(last_message_id, ?) WHERE channel=?",
                (message_id, channel),
            )
            self.conn.execute(
                "UPDATE user_watched_channels SET last_message_id=max(last_message_id, ?) WHERE lower(channel)=lower(?)",
                (message_id, channel),
            )
            self.conn.execute(
                "UPDATE analysis_channels SET last_message_id=max(last_message_id, ?) WHERE lower(source_channel)=lower(?)",
                (message_id, channel),
            )
            self.conn.execute(
                "UPDATE user_analysis_channels SET last_message_id=max(last_message_id, ?) WHERE lower(source_channel)=lower(?)",
                (message_id, channel),
            )

    def set_setting(self, key: str, value: str) -> None:
        user_id = self.current_user_id()
        if user_id != self.owner_id:
            with self.conn:
                self.conn.execute("INSERT INTO user_settings(user_id,key,value) VALUES(?,?,?) ON CONFLICT(user_id,key) DO UPDATE SET value=excluded.value", (user_id, key, value))
            return
        with self.conn:
            self.conn.execute(
                "INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def get_int_setting(self, key: str, default: int) -> int:
        user_id = self.current_user_id()
        row = self.conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone() if (
            user_id == self.owner_id or key in self.GLOBAL_SETTING_KEYS
        ) else self.conn.execute("SELECT value FROM user_settings WHERE user_id=? AND key=?", (user_id, key)).fetchone()
        try:
            return int(row["value"]) if row else default
        except (TypeError, ValueError):
            return default

    def get_setting(self, key: str, default: str = "") -> str:
        user_id = self.current_user_id()
        row = self.conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone() if (
            user_id == self.owner_id or key in self.GLOBAL_SETTING_KEYS
        ) else self.conn.execute("SELECT value FROM user_settings WHERE user_id=? AND key=?", (user_id, key)).fetchone()
        return str(row["value"]) if row else default

    def set_global_setting(self, key: str, value: str) -> None:
        """Store a setting shared by every user; intended for owner controls."""
        with self.conn:
            self.conn.execute(
                "INSERT INTO settings(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def get_global_setting(self, key: str, default: str = "") -> str:
        row = self.conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return str(row["value"]) if row else default

    def get_global_int_setting(self, key: str, default: int) -> int:
        try:
            return int(self.get_global_setting(key, str(default)))
        except (TypeError, ValueError):
            return default

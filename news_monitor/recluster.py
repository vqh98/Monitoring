from __future__ import annotations

from .textmatch import news_match, signature


def recluster_all(db, threshold: float) -> int:
    rows = db.conn.execute("SELECT * FROM posts ORDER BY published_at, id").fetchall()
    clusters: list[tuple[int, str]] = []
    with db.conn:
        db.conn.execute("DELETE FROM clusters")
        db.conn.execute("UPDATE posts SET cluster_id=NULL")
        for row in rows:
            normalized = signature(row["text"])
            best_id, best_score = None, 0.0
            for cluster_id, representative in clusters:
                matched, score = news_match(normalized, representative, threshold)
                if matched and score > best_score:
                    best_id, best_score = cluster_id, score
            if best_id is None:
                cur = db.conn.execute(
                    "INSERT INTO clusters(representative,created_at) VALUES(?,?)",
                    (normalized, row["published_at"]),
                )
                best_id = int(cur.lastrowid)
                clusters.append((best_id, normalized))
            db.conn.execute(
                "UPDATE posts SET normalized=?,cluster_id=? WHERE id=?",
                (normalized, best_id, row["id"]),
            )
    return len(clusters)

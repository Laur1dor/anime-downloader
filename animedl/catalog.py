"""Small persistent discovery catalogue; title similarity never defines identity."""
from __future__ import annotations

import json
import secrets
import sqlite3
import time
import unicodedata
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .models import TitleInfo


def normalize_name(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold().replace("ё", "е")
    return " ".join("".join(c if c.isalnum() else " " for c in value).split())


@dataclass(frozen=True)
class CatalogHit:
    site: str
    url: str
    title: str
    aliases: tuple[str, ...] = ()
    poster: str = ""
    mal_id: int | None = None


def _url(value: str) -> str:
    if not isinstance(value, str) or len(value) > 2048 or any(ord(c) < 32 for c in value):
        raise ValueError("Invalid catalogue URL")
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password:
        raise ValueError("Invalid catalogue URL")
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if not k.casefold().startswith("utm_")]
    return urlunsplit((parts.scheme, parts.netloc.lower(), parts.path, urlencode(query), ""))


def _hit(value, aliases=()) -> CatalogHit:
    name = " ".join(str(value.title).split())[:300]
    if not name:
        raise ValueError("Empty catalogue title")
    names = tuple(dict.fromkeys(" ".join(str(a).split())[:300]
                               for a in (*getattr(value, "aliases", ()), *aliases) if a))[:40]
    poster = getattr(value, "poster", "") or ""
    try:
        poster = _url(poster) if poster else ""
    except ValueError:
        poster = ""
    mal_id = getattr(value, "mal_id", None)
    if type(mal_id) is not int or mal_id <= 0:
        mal_id = None
    return CatalogHit(str(value.site)[:80], _url(value.url), name, names, poster, mal_id)


class CatalogStore:
    SESSION_TTL = 30 * 60
    EVENT_RETENTION = 90 * 86400

    def __init__(self, path: str | Path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS titles (
                url TEXT PRIMARY KEY, site TEXT NOT NULL, title TEXT NOT NULL,
                aliases TEXT NOT NULL, poster TEXT NOT NULL, mal_id INTEGER,
                updated_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS names (
                url TEXT NOT NULL, name TEXT NOT NULL, PRIMARY KEY(url, name));
            CREATE INDEX IF NOT EXISTS names_name ON names(name);
            CREATE TABLE IF NOT EXISTS sessions (
                token TEXT PRIMARY KEY, user_id INTEGER NOT NULL,
                results TEXT NOT NULL, expires_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS events (
                user_id INTEGER NOT NULL, action TEXT NOT NULL,
                site TEXT NOT NULL, created_at REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS events_time ON events(created_at);
        """)

    def remember(self, title: TitleInfo | CatalogHit, aliases=()) -> CatalogHit:
        hit = _hit(title, aliases)
        previous = self.db.execute("SELECT * FROM titles WHERE url=?", (hit.url,)).fetchone()
        if previous:
            old_aliases = [*json.loads(previous["aliases"]), previous["title"]]
            # Keep already learned names first; repeated discovery must not erase them.
            merged_aliases = tuple(dict.fromkeys([*old_aliases, *hit.aliases]))[:256]
            hit = CatalogHit(hit.site, hit.url, hit.title, merged_aliases,
                             hit.poster or previous["poster"], hit.mal_id or previous["mal_id"])
        with self.db:
            self.db.execute("""INSERT INTO titles VALUES (?,?,?,?,?,?,?)
                ON CONFLICT(url) DO UPDATE SET site=excluded.site,title=excluded.title,
                aliases=excluded.aliases,poster=excluded.poster,mal_id=excluded.mal_id,
                updated_at=excluded.updated_at""",
                (hit.url, hit.site, hit.title, json.dumps(hit.aliases, ensure_ascii=False),
                 hit.poster, hit.mal_id, time.time()))
            self.db.execute("DELETE FROM names WHERE url=?", (hit.url,))
            self.db.executemany("INSERT OR IGNORE INTO names VALUES (?,?)",
                                [(hit.url, normalize_name(n)) for n in (hit.title, *hit.aliases)])
        return hit

    def search(self, query: str, limit: int = 12) -> list[CatalogHit]:
        query = normalize_name(query[:200])
        if not query:
            return []
        words = query.split()[:20]
        # Numeric identifiers are whole tokens: season 3 must not match season 13.
        clauses = " AND ".join("(' ' || n.name || ' ') LIKE ?" if word.isdecimal()
                               else "n.name LIKE ?" for word in words)
        rows = self.db.execute(f"""SELECT t.*, MIN(CASE WHEN n.name=? THEN 0
            WHEN n.name LIKE ? THEN 1 ELSE 2 END) AS rank FROM titles t
            JOIN names n ON n.url=t.url WHERE {clauses}
            GROUP BY t.url ORDER BY rank,t.updated_at DESC,t.url LIMIT ?""",
            (query, query + "%", *("% " + word + " %" if word.isdecimal() else "%" + word + "%"
                                   for word in words), max(1, min(int(limit), 50))))
        return [CatalogHit(r["site"], r["url"], r["title"], tuple(json.loads(r["aliases"])),
                           r["poster"], r["mal_id"]) for r in rows]

    def store_results(self, user_id: int, hits) -> str:
        results = [asdict(_hit(h)) for h in list(hits)[:50]]
        token = secrets.token_hex(8)
        with self.db:
            self.db.execute("DELETE FROM sessions WHERE expires_at<=?", (time.time(),))
            self.db.execute("INSERT INTO sessions VALUES (?,?,?,?)",
                            (token, user_id, json.dumps(results, ensure_ascii=False), time.time() + self.SESSION_TTL))
        return token

    def get_result(self, user_id: int, token: str, index: int) -> CatalogHit | None:
        if type(index) is not int or index < 0:
            return None
        row = self.db.execute("SELECT results FROM sessions WHERE token=? AND user_id=? AND expires_at>?",
                              (token, user_id, time.time())).fetchone()
        if not row:
            return None
        results = json.loads(row["results"])
        if index >= len(results):
            return None
        data = results[index]
        data["aliases"] = tuple(data["aliases"])
        return CatalogHit(**data)

    def event(self, user_id: int, action: str, site: str = "", **details) -> None:
        # Intentionally exclude queries, URLs, tokens, messages and arbitrary details.
        if action not in {"search", "selection", "no_results", "provider_error"}:
            raise ValueError("Unknown catalogue event")
        with self.db:
            self.db.execute("DELETE FROM events WHERE created_at<?", (time.time() - self.EVENT_RETENTION,))
            self.db.execute("INSERT INTO events VALUES (?,?,?,?)",
                            (user_id, action, str(site)[:80], time.time()))

    def stats(self) -> dict[str, int]:
        since = time.time() - 7 * 86400
        counts = dict(self.db.execute("SELECT action,COUNT(*) FROM events WHERE created_at>=? GROUP BY action", (since,)))
        users = self.db.execute("SELECT COUNT(DISTINCT user_id) FROM events WHERE created_at>=?", (since,)).fetchone()[0]
        return {"users": users, "searches": counts.get("search", 0),
                "selections": counts.get("selection", 0), "no_results": counts.get("no_results", 0),
                "provider_errors": counts.get("provider_error", 0), "events": sum(counts.values())}

    def close(self) -> None:
        self.db.close()

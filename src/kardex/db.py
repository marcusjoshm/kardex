"""Database access: connection, validation, and the item operations."""

import json
import os
import re
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path

from .schema import COLLECTIONS, GENERATED, SCHEMA

DEFAULT_DB = Path("~/.local/share/kardex/kardex.db").expanduser()
SORTS = {"name": "i.name COLLATE NOCASE", "id": "i.id", "added": "i.added", "updated": "i.updated"}
SORTS.update({key: f"i.{col}" for key, (col, _) in GENERATED.items()})

_SELECT = """
SELECT i.id, i.name, i.collection, i.category, l.name AS location, i.quantity,
       i.notes, i.photo, i.attrs, i.added, i.updated
FROM items i LEFT JOIN locations l ON l.id = i.location_id
"""


class KardexError(Exception):
    pass


def db_path(path: str | os.PathLike | None = None) -> Path:
    return Path(path or os.environ.get("KARDEX_DB") or DEFAULT_DB).expanduser()


def connect(path: str | os.PathLike | None = None) -> sqlite3.Connection:
    p = db_path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _row(r: sqlite3.Row) -> dict:
    d = dict(r)
    d["attrs"] = json.loads(d["attrs"])
    return d


def check_collection(collection: str) -> str:
    collection = collection.strip().lower()
    if collection not in COLLECTIONS:
        raise KardexError(f"unknown collection '{collection}' (known: {', '.join(COLLECTIONS)})")
    return collection


def normalize_category(category: str | None) -> str | None:
    if category is None:
        return None
    parts = [p.strip() for p in category.split("/") if p.strip()]
    return "/".join(parts) or None


def parse_attrs(collection: str, pairs: list[str], allow_unset: bool = False) -> dict:
    """Turn ['key=value', ...] into a validated dict; with allow_unset, 'key=' maps to None."""
    keys = COLLECTIONS[collection]
    out: dict = {}
    for pair in pairs or []:
        key, sep, value = pair.partition("=")
        key, value = key.strip(), value.strip()
        if not sep:
            raise KardexError(f"attribute '{pair}' must look like key=value")
        if key not in keys:
            raise KardexError(f"'{key}' is not an attribute of {collection} (known: {', '.join(keys)})")
        if value == "":
            if not allow_unset:
                raise KardexError(f"attribute '{key}' has no value")
            out[key] = None
            continue
        kind = keys[key]
        try:
            if kind == "int":
                out[key] = int(value)
            elif kind == "num":
                f = float(value)
                out[key] = int(f) if f.is_integer() else f
            elif kind == "date":
                out[key] = date.fromisoformat(value).isoformat()
            else:
                out[key] = value
        except ValueError:
            raise KardexError(f"attribute '{key}' must be {kind}: '{value}'") from None
    return out


def _location_id(conn: sqlite3.Connection, name: str | None) -> int | None:
    if name is None:
        return None
    name = name.strip()
    if not name:
        return None
    conn.execute("INSERT OR IGNORE INTO locations(name) VALUES (?)", (name,))
    return conn.execute("SELECT id FROM locations WHERE name = ?", (name,)).fetchone()["id"]


def _check_quantity(quantity: int) -> int:
    if quantity < 0:
        raise KardexError("quantity cannot be negative")
    return quantity


def get(conn: sqlite3.Connection, item_id: int) -> dict:
    r = conn.execute(_SELECT + "WHERE i.id = ?", (item_id,)).fetchone()
    if r is None:
        raise KardexError(f"no item with id {item_id}")
    return _row(r)


def add(conn, collection, name, *, category=None, location=None, quantity=1,
        notes=None, photo=None, attrs=None) -> dict:
    collection = check_collection(collection)
    name = (name or "").strip()
    if not name:
        raise KardexError("name is required")
    now = _now()
    with conn:
        cur = conn.execute(
            "INSERT INTO items(name, collection, category, location_id, quantity, notes, photo,"
            " attrs, added, updated) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (name, collection, normalize_category(category), _location_id(conn, location),
             _check_quantity(quantity), notes, photo,
             json.dumps(attrs or {}, sort_keys=True), now, now),
        )
    return get(conn, cur.lastrowid)


def edit(conn, item_id, *, name=None, category=None, quantity=None, notes=None,
         photo=None, attrs=None, location=None) -> dict:
    """Change the fields that are not None. Empty string clears category, notes, photo, location.
    attrs maps key -> value, with None removing the key."""
    item = get(conn, item_id)
    sets, args = [], []
    if name is not None:
        if not name.strip():
            raise KardexError("name cannot be empty")
        sets.append("name = ?"); args.append(name.strip())
    if category is not None:
        sets.append("category = ?"); args.append(normalize_category(category))
    if quantity is not None:
        sets.append("quantity = ?"); args.append(_check_quantity(quantity))
    if notes is not None:
        sets.append("notes = ?"); args.append(notes or None)
    if photo is not None:
        sets.append("photo = ?"); args.append(photo or None)
    if attrs:
        merged = {**item["attrs"], **attrs}
        merged = {k: v for k, v in merged.items() if v is not None}
        sets.append("attrs = ?"); args.append(json.dumps(merged, sort_keys=True))
    with conn:
        if location is not None:
            sets.append("location_id = ?"); args.append(_location_id(conn, location))
        if sets:
            sets.append("updated = ?"); args.append(_now())
            conn.execute(f"UPDATE items SET {', '.join(sets)} WHERE id = ?", (*args, item_id))
    return get(conn, item_id)


def move(conn, item_id, location) -> dict:
    if not (location or "").strip():
        raise KardexError("location is required")
    return edit(conn, item_id, location=location)


def remove(conn, item_id) -> dict:
    item = get(conn, item_id)
    with conn:
        conn.execute("DELETE FROM items WHERE id = ?", (item_id,))
    return item


def fts_query(text: str) -> str:
    """Make free text safe for FTS5: every word must match, as a prefix."""
    words = re.findall(r"\w+", text)
    return " ".join(f'"{w}"*' for w in words)


def find(conn, query=None, *, collection=None, category=None, location=None,
         sort=None, desc=False, limit=None) -> list[dict]:
    where, args = [], []
    sql = _SELECT
    order = "i.collection, i.category, i.name COLLATE NOCASE, i.id"
    if query and (q := fts_query(query)):
        sql += " JOIN items_fts f ON f.rowid = i.id"
        where.append("items_fts MATCH ?"); args.append(q)
        order = "rank, i.id"
    if collection:
        where.append("i.collection = ?"); args.append(check_collection(collection))
    if category:
        c = normalize_category(category)
        where.append("(i.category = ? OR i.category LIKE ? ESCAPE '\\')")
        args += [c, re.sub(r"([%_\\])", r"\\\1", c) + "/%"]
    if location:
        where.append("l.name = ?"); args.append(location.strip())
    if sort:
        if sort not in SORTS:
            raise KardexError(f"cannot sort by '{sort}' (use: {', '.join(SORTS)})")
        col = SORTS[sort]
        order = f"{col} IS NULL, {col} {'DESC' if desc else 'ASC'}, i.id"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += f" ORDER BY {order}"
    if limit:
        sql += " LIMIT ?"; args.append(limit)
    return [_row(r) for r in conn.execute(sql, args)]


def collections(conn) -> list[dict]:
    counts = {r["collection"]: (r["n"], r["q"]) for r in conn.execute(
        "SELECT collection, count(*) n, sum(quantity) q FROM items GROUP BY collection")}
    return [{"collection": c, "items": counts.get(c, (0, 0))[0],
             "quantity": counts.get(c, (0, 0))[1] or 0, "attrs": keys}
            for c, keys in COLLECTIONS.items()]


def locations(conn) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT l.name, count(i.id) AS items FROM locations l"
        " LEFT JOIN items i ON i.location_id = l.id GROUP BY l.id ORDER BY l.name COLLATE NOCASE")]

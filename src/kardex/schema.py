"""Collections, their attribute keys, and the SQL schema."""

# attribute key -> type: "str", "int", "num" or "date"
COLLECTIONS: dict[str, dict[str, str]] = {
    "records": {"artist": "str", "label": "str", "year": "int", "format": "str", "pressing": "str",
                "catno": "str", "barcode": "str", "country": "str",
                "discogs_release_id": "int", "discogs_instance_id": "int", "photo_back": "str"},
    "movies": {"director": "str", "year": "int", "format": "str"},
    "music": {"artist": "str", "album": "str", "year": "int", "format": "str"},
    "kitchen": {"brand": "str", "material": "str"},
    "clothes": {"brand": "str", "size": "str", "color": "str"},
    "gear": {"weight": "num", "last_used": "date"},
}

# Generated, indexed columns over attrs: sort field -> (column, SQL type).
GENERATED = {
    "artist": ("a_artist", "TEXT COLLATE NOCASE"),
    "year": ("a_year", "INTEGER"),
    "label": ("a_label", "TEXT COLLATE NOCASE"),
    "director": ("a_director", "TEXT COLLATE NOCASE"),
    "discogs_release_id": ("a_discogs_release_id", "INTEGER"),
}


def generated_column_sql(key: str) -> str:
    """The definition of one generated column. They are all VIRTUAL so that `migrate` can
    ALTER TABLE ADD them to an existing database (SQLite cannot add a STORED one)."""
    col, typ = GENERATED[key]
    return f"{col} {typ} GENERATED ALWAYS AS (json_extract(attrs, '$.{key}')) VIRTUAL"


_collections_sql = ", ".join(f"'{c}'" for c in COLLECTIONS)
_generated_sql = ",\n".join(f"    {generated_column_sql(key)}" for key in GENERATED)

# Run after `migrate`, since an older database may lack a column until then.
INDEXES = "\n".join(
    f"CREATE INDEX IF NOT EXISTS idx_items_{col} ON items({col});" for col, _ in GENERATED.values()
)

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS locations (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE
);

CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    collection TEXT NOT NULL CHECK (collection IN ({_collections_sql})),
    category TEXT,
    location_id INTEGER REFERENCES locations(id),
    quantity INTEGER NOT NULL DEFAULT 1 CHECK (quantity >= 0),
    notes TEXT,
    photo TEXT,
    attrs TEXT NOT NULL DEFAULT '{{}}' CHECK (json_valid(attrs)),
    added TEXT NOT NULL,
    updated TEXT NOT NULL,
{_generated_sql}
);
CREATE INDEX IF NOT EXISTS idx_items_collection ON items(collection, category);
CREATE INDEX IF NOT EXISTS idx_items_location ON items(location_id);

CREATE VIRTUAL TABLE IF NOT EXISTS items_fts USING fts5(
    name, notes, category, location, attrs, tokenize = 'unicode61 remove_diacritics 2'
);

CREATE TRIGGER IF NOT EXISTS items_ai AFTER INSERT ON items BEGIN
    INSERT INTO items_fts(rowid, name, notes, category, location, attrs)
    VALUES (new.id, new.name, new.notes, new.category,
            (SELECT name FROM locations WHERE id = new.location_id),
            (SELECT group_concat(value, ' ') FROM json_each(new.attrs)));
END;

CREATE TRIGGER IF NOT EXISTS items_ad AFTER DELETE ON items BEGIN
    DELETE FROM items_fts WHERE rowid = old.id;
END;

CREATE TRIGGER IF NOT EXISTS items_au AFTER UPDATE ON items BEGIN
    DELETE FROM items_fts WHERE rowid = old.id;
    INSERT INTO items_fts(rowid, name, notes, category, location, attrs)
    VALUES (new.id, new.name, new.notes, new.category,
            (SELECT name FROM locations WHERE id = new.location_id),
            (SELECT group_concat(value, ' ') FROM json_each(new.attrs)));
END;

CREATE TRIGGER IF NOT EXISTS locations_au AFTER UPDATE OF name ON locations BEGIN
    UPDATE items_fts SET location = new.name
    WHERE rowid IN (SELECT id FROM items WHERE location_id = new.id);
END;
"""

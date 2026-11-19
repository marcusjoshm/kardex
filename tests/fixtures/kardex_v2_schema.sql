-- The kardex v2 schema (commit 516ddce), before the Discogs columns. Used by the migration test.
CREATE TABLE locations (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE
);
CREATE TABLE items (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    collection TEXT NOT NULL CHECK (collection IN ('records', 'movies', 'music', 'kitchen', 'clothes', 'gear')),
    category TEXT,
    location_id INTEGER REFERENCES locations(id),
    quantity INTEGER NOT NULL DEFAULT 1 CHECK (quantity >= 0),
    notes TEXT,
    photo TEXT,
    attrs TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(attrs)),
    added TEXT NOT NULL,
    updated TEXT NOT NULL,
    a_artist TEXT COLLATE NOCASE GENERATED ALWAYS AS (json_extract(attrs, '$.artist')) VIRTUAL,
    a_year INTEGER GENERATED ALWAYS AS (json_extract(attrs, '$.year')) VIRTUAL,
    a_label TEXT COLLATE NOCASE GENERATED ALWAYS AS (json_extract(attrs, '$.label')) VIRTUAL,
    a_director TEXT COLLATE NOCASE GENERATED ALWAYS AS (json_extract(attrs, '$.director')) VIRTUAL
);
CREATE INDEX idx_items_collection ON items(collection, category);
CREATE INDEX idx_items_location ON items(location_id);
CREATE INDEX idx_items_a_artist ON items(a_artist);
CREATE INDEX idx_items_a_year ON items(a_year);
CREATE INDEX idx_items_a_label ON items(a_label);
CREATE INDEX idx_items_a_director ON items(a_director);
CREATE VIRTUAL TABLE items_fts USING fts5(
    name, notes, category, location, attrs, tokenize = 'unicode61 remove_diacritics 2'
);
CREATE TRIGGER items_ai AFTER INSERT ON items BEGIN
    INSERT INTO items_fts(rowid, name, notes, category, location, attrs)
    VALUES (new.id, new.name, new.notes, new.category,
            (SELECT name FROM locations WHERE id = new.location_id),
            (SELECT group_concat(value, ' ') FROM json_each(new.attrs)));
END;
CREATE TRIGGER items_ad AFTER DELETE ON items BEGIN
    DELETE FROM items_fts WHERE rowid = old.id;
END;
CREATE TRIGGER items_au AFTER UPDATE ON items BEGIN
    DELETE FROM items_fts WHERE rowid = old.id;
    INSERT INTO items_fts(rowid, name, notes, category, location, attrs)
    VALUES (new.id, new.name, new.notes, new.category,
            (SELECT name FROM locations WHERE id = new.location_id),
            (SELECT group_concat(value, ' ') FROM json_each(new.attrs)));
END;
CREATE TRIGGER locations_au AFTER UPDATE OF name ON locations BEGIN
    UPDATE items_fts SET location = new.name
    WHERE rowid IN (SELECT id FROM items WHERE location_id = new.id);
END;

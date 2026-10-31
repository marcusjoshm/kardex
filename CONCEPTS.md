# Concepts

Shared domain vocabulary for this project: entities and the words they go by.
Glossary only, not a spec; the tables are in `SCHEMA.md`.

## Relationships

An Item belongs to exactly one Collection, at most one Category within it, and is kept
at most one Location. Categories nest into one another without limit; Locations never
nest. Categories belong to their Collection, while Locations are shared: Items in
unrelated Collections can name the same Location.

## Inventory

### Item
One thing the owner possesses: one row per record, movie or garment. A quantity above 1
is only for interchangeable things, like a set of glasses.

Every Item carries shared fields (name, collection, category, location, quantity, notes,
photo, timestamps) and its Collection's Attributes. Unlike the first version, where a
record's artist lived in the notes, the artist is now a field that can be sorted on.

### Collection
One of the fixed top-level groupings: records, kitchen, clothes, movies, music, gear. A
Collection decides which Attributes an Item may have. It is a value on the Item, not a
node in the Category tree.

### Category
A path under a Collection, such as `jazz/hard-bop`, that nests to any depth. It is text on
the Item, so finding a Category's Items, sub-categories included, is a prefix match.
An Item may have no Category.

### Attribute
A field only some Collections care about (artist, label, year, format, pressing for
records; director for movies; weight for gear), stored in the Item's `attrs` JSON. Each
Collection has a fixed set of keys. The ones that get sorted on are also indexed columns.

### Location
A flat label naming where an Item is kept. Locations do not nest; they are one vocabulary
of place names shared across all Collections, created on first use and reused by name.

## Access

### Peems
The only interface. Kardex has no web app, API server or accounts; Peems runs the `kardex`
CLI, or `sqlite3` directly, and brings in metadata from outside.

### Export
The plain-text copy of the whole database (JSONL per Collection) kept in git for backup.

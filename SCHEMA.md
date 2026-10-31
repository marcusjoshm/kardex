# Kardex schema

How Peems (or anyone) uses the database. One SQLite file, path in `KARDEX_DB`
(default `~/.local/share/kardex/kardex.db`). The CLI creates it on first use. You may use
the `kardex` CLI or run `sqlite3` directly; the CLI adds validation and consistent
conventions, not a layer you must go through. Direct SQL is fine for reads and for
anything the CLI lacks; if you insert by hand, follow the conventions below.

## Tables

### `items`
One row per record, movie or garment. A quantity above 1 only for interchangeable things (glasses).

| column | meaning |
|---|---|
| `id` | integer primary key |
| `name` | what it is called (album title, movie title, garment) |
| `collection` | `records`, `kitchen`, `clothes`, `movies`, `music` or `gear` (CHECK-enforced) |
| `category` | nesting path under the collection, `/`-separated, e.g. `jazz/hard-bop`; NULL if none |
| `location_id` | foreign key to `locations`; NULL if unknown |
| `quantity` | integer >= 0, default 1 |
| `notes` | free text |
| `photo` | optional path to a photo |
| `attrs` | JSON object of collection attributes (below); must be valid JSON |
| `added`, `updated` | UTC ISO 8601 text, `2026-10-31T12:00:00Z`; set `updated` on every change |
| `a_artist`, `a_year`, `a_label`, `a_director` | generated from `attrs`, indexed; read-only. Sort on these |

### `locations`
`id`, `name` (unique, case-insensitive). A flat vocabulary shared by every collection; it
never nests. Created on first use. Reuse an existing name rather than inventing a variant
(`kardex locations` lists them).

### `items_fts`
FTS5 table over `name`, `notes`, `category`, `location` and the values of `attrs`, with
`rowid = items.id`. Triggers keep it in sync on insert, update and delete of items and on
renames of locations; do not write to it. Search by any word:

```sql
SELECT i.* FROM items i JOIN items_fts f ON f.rowid = i.id
WHERE items_fts MATCH '"miles"* "blue"*' ORDER BY rank;
```

Quote each word and add `*` for a prefix match, as above; bare punctuation breaks FTS syntax.

## Collections and attribute keys

Keys are lowercase. The CLI rejects other keys and wrong types; they are stored in `attrs`.

| collection | attribute keys |
|---|---|
| `records` | `artist`, `label`, `year` (int), `format`, `pressing` |
| `movies` | `director`, `year` (int), `format` |
| `music` | `artist`, `album`, `year` (int), `format` |
| `kitchen` | `brand`, `material` |
| `clothes` | `brand`, `size`, `color` |
| `gear` | `weight` (grams, number), `last_used` (`YYYY-MM-DD`) |

### records (fully worked out)

One row per physical record. `name` is the album title. `artist` is how it is credited on
the sleeve. `format` is the physical format as written on the release: `LP`, `EP`, `7"`,
`12"`, `10"`, `CD`, `cassette`. `pressing` says which copy it is when that matters
(`1st stereo`, `2019 reissue`, `Japan OBI`). `label` is the label name; put the catalog
number in `notes`. `category` is a genre path such as `jazz/hard-bop` or `rock`.

### The others (conventions)

- `movies`: `name` is the title; `format` is `DVD`, `Blu-ray`, `4K`, `VHS`; `category` a genre.
- `music`: digital or non-record music; `name` is the track or album, `album` set when `name` is a track.
- `kitchen`, `clothes`: few attributes; use `quantity` for sets of glasses or socks, one row per garment.
- `gear`: `weight` in grams, `last_used` as an ISO date; `category` like `camping/shelter`.

Adding a key to a collection means editing `COLLECTIONS` in `src/kardex/schema.py`; add a
generated column there too if it will be sorted on.

## CLI examples

All commands take `--json` (before or after the command) and `--db PATH`.

```sh
kardex add records "Kind of Blue" --category jazz/modal --location "Living room shelf" \
    --attr artist="Miles Davis" --attr label=Columbia --attr year=1959 --attr format=LP --notes "Mono reissue"
kardex find "miles davis" --json                  # full-text search
kardex find --collection records --category jazz --sort year --desc
kardex find --location "Living room shelf" --limit 20
kardex show 12
kardex edit 12 --attr pressing="1st stereo" --attr label=    # KEY= removes an attribute
kardex edit 12 --notes ""                                    # empty value clears a field
kardex move 12 Attic
kardex remove 12
kardex collections        # counts and attribute keys
kardex locations          # names and item counts
kardex export             # writes export/<collection>.jsonl
```

`find --json` returns a list of items; each has `id, name, collection, category, location`
(a name), `quantity, notes, photo, attrs` (an object), `added, updated`. Errors exit 1 with
a message on stderr (`{"error": ...}` with `--json`).

## SQL examples

```sql
-- records on a label, oldest first
SELECT id, name, a_artist, a_year FROM items WHERE collection = 'records' AND a_label = 'Blue Note' ORDER BY a_year;

-- everything in a place
SELECT i.id, i.name, i.collection FROM items i JOIN locations l ON l.id = i.location_id WHERE l.name = 'Attic';

-- count by collection
SELECT collection, count(*), sum(quantity) FROM items GROUP BY collection;

-- an attribute without a generated column
SELECT name, json_extract(attrs, '$.pressing') FROM items WHERE json_extract(attrs, '$.pressing') IS NOT NULL;

-- a hand insert (create the location first, set both timestamps)
INSERT OR IGNORE INTO locations(name) VALUES ('Attic');
INSERT INTO items(name, collection, location_id, attrs, added, updated)
VALUES ('Alien', 'movies', (SELECT id FROM locations WHERE name = 'Attic'),
        '{"director":"Ridley Scott","year":1979,"format":"Blu-ray"}',
        strftime('%Y-%m-%dT%H:%M:%SZ','now'), strftime('%Y-%m-%dT%H:%M:%SZ','now'));
```

## Export

`kardex export` writes `export/records.jsonl`, `export/movies.jsonl` and so on (all six, empty
ones included). One JSON object per line, keys sorted, items in id order, so a git diff shows
only the items that changed. Commit `export/` for backup. It is a one-way dump; the SQLite
file is the source of truth. Set `KARDEX_EXPORT` or pass `--dir` to write elsewhere.

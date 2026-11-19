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
| `photo` | optional path to a photo; for a record added through Discogs, the copy of the front cover (see Photos) |
| `attrs` | JSON object of collection attributes (below); must be valid JSON |
| `added`, `updated` | UTC ISO 8601 text, `2026-10-31T12:00:00Z`; set `updated` on every change |
| `a_artist`, `a_year`, `a_label`, `a_director`, `a_discogs_release_id` | generated from `attrs` (VIRTUAL), indexed; read-only. Sort and look up on these |

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
| `records` | `artist`, `label`, `year` (int), `format`, `pressing`, `catno`, `barcode`, `country`, `discogs_release_id` (int), `discogs_instance_id` (int), `photo_back` |
| `movies` | `director`, `year` (int), `format` |
| `music` | `artist`, `album`, `year` (int), `format` |
| `kitchen` | `brand`, `material` |
| `clothes` | `brand`, `size`, `color` |
| `gear` | `weight` (grams, number), `last_used` (`YYYY-MM-DD`) |

### records (fully worked out)

One row per physical record. `name` is the album title. `artist` is how it is credited on
the sleeve. `format` is the physical format as written on the release: `LP`, `EP`, `7"`,
`12"`, `10"`, `CD`, `cassette`. `pressing` says which copy it is when that matters
(`1st stereo`, `2019 reissue`, `Japan OBI`). `label` is the label name. `category` is a genre
path such as `jazz/hard-bop` or `rock`.

The keys added for Discogs intake (all optional, and settable by hand with `--attr`):

| key | meaning |
|---|---|
| `catno` | catalog number as printed, e.g. `CL 1355` (the first label's, from Discogs) |
| `barcode` | the barcode's digits only, e.g. `074646493517` |
| `country` | country of the pressing as Discogs names it: `US`, `UK`, `Japan`, `Europe` |
| `discogs_release_id` | the Discogs release (one specific pressing), `https://www.discogs.com/release/<id>`; generated column `a_discogs_release_id`, indexed |
| `discogs_instance_id` | this copy's instance in the Discogs collection; absent when added with `--no-discogs` |
| `photo_back` | path to the copy of the back-cover photo (the front is in `photo`) |

`kardex discogs add` fills them from the release: `artist` as credited (Discogs' `(2)` suffix
dropped), `label` and `catno` from the first label, `year`, `format` (`LP`, `2xLP`, `7"`, ...),
and `pressing` from the country, the format descriptions that say which copy it is
(`Mono`, `Reissue`, `180 Gram`, ...) and the format's free text (`6-Eye`), plus `Reissue` or
`Repress` when the release notes say so. Example: `US, Mono, 6-Eye`. Records added before
this keep the catalog number in `notes`.

### The others (conventions)

- `movies`: `name` is the title; `format` is `DVD`, `Blu-ray`, `4K`, `VHS`; `category` a genre.
- `music`: digital or non-record music; `name` is the track or album, `album` set when `name` is a track.
- `kitchen`, `clothes`: few attributes; use `quantity` for sets of glasses or socks, one row per garment.
- `gear`: `weight` in grams, `last_used` as an ISO date; `category` like `camping/shelter`.

Adding a key to a collection means editing `COLLECTIONS` in `src/kardex/schema.py`; add a
generated column (`GENERATED`) there too if it will be sorted or looked up on. Generated
columns are VIRTUAL: on opening, `kardex` adds any that an older database lacks with
`ALTER TABLE ADD COLUMN` (SQLite cannot add a STORED one) and creates their indexes, so an
existing database migrates in place and keeps its rows.

## Photos

Records added with `kardex discogs add --front PATH --back PATH` get copies of the cover photos
in `$KARDEX_PHOTOS` (default `~/.local/share/kardex/photos/`), named by item id:
`<id>-front.<ext>` and `<id>-back.<ext>`, extension lowercased (`12-front.jpg`,
`12-back.heic`). The originals are left where they were. `photo` holds the absolute path of
the front copy and `attrs.photo_back` that of the back. Removing an item does not delete its
photos. The photos are not in the export; back up the folder separately.

## Discogs

`kardex discogs` finds a record on Discogs, adds it to the Discogs collection and to kardex.
It needs `DISCOGS_TOKEN`, a Discogs personal access token (Settings > Developers); the
username comes from the token. Requests send `User-Agent: kardex/2 +https://github.com/marcusjoshm/kardex`,
stay under Discogs' 60 requests a minute (one a second when fewer than 5 remain in the window,
a minute's wait when none do) and retry a `429` with backoff.

The intake loop: photograph the front and back of the sleeve; read the barcode digits,
catalog number, label and country off the back; `match`; then `add` the release id.

```sh
kardex discogs match --barcode "0 74646 49351 7" --artist "Miles Davis" --title "Kind of Blue"
kardex discogs match --catno "CL 1355" --label Columbia --country US --year 1959 --json
kardex discogs add 9100001 --front ~/in/IMG_0001.jpg --back ~/in/IMG_0002.jpg \
    --location "Living room shelf" --category jazz/modal --json
kardex discogs add 9100001 --copy            # a second copy of a release kardex already has
kardex discogs add 9100001 --no-discogs      # kardex only; nothing written to Discogs
kardex discogs sync                          # Discogs vs kardex, read-only
```

**`match`** takes any subset of `--barcode --catno --label --artist --title --country --year`
(at least one of the first five; country and year only rank). It searches Discogs for vinyl
releases with the strongest identifiers first: the barcode, then catno (with the label if
given), then artist+title, and stops after the first search that gives a confident match.
Each candidate scores one point per given field it agrees with (barcode digits, ignoring a
leading 0; catno ignoring spaces and punctuation; label, artist and title ignoring case,
punctuation, a leading "The" and suffixes like "Records"; country with aliases such as
`USA`=`US`; year exactly). Candidates are sorted by score, then by how many Discogs users
have them. **Confident** means exactly one candidate agrees on the barcode or on
catno+label, it is the top candidate, and no other candidate has as high a score. Several
pressings that share a catalog number are therefore not confident even when the country
breaks the tie: that is the short list to show Joshua. It prints the top `--limit` (5)
candidates with release id, artist, title, label, catno, country, year, format, the fields
that agree and the `confident` flag. `--json` returns
`{"confident", "release_id" (null unless confident), "searched", "given", "candidates": [{"release_id",
"artist", "title", "label", "catno", "country", "year", "format", "score", "agrees", "confident", "uri"}]}`.

**`add RELEASE_ID`** takes `--front --back --location --category --notes --no-discogs --copy`.
It refuses a release kardex already has (same `discogs_release_id`) unless `--copy` is given,
and checks the photo paths, before any request. It fetches the release; unless `--no-discogs`,
it adds the release to the Discogs collection (folder Uncategorized) only if the collection
has no instance of it that another kardex row already claims, and stores that instance id. It
then adds one `records` item, named by the release title, with the attributes above, and
copies the photos. `--json` prints the item.

**`sync`** lists the whole Discogs collection and compares it with kardex's records by
release id, copy for copy: what is in Discogs and not kardex, what is in kardex and not
Discogs, and records with no `discogs_release_id`. It changes nothing.

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

-- is this pressing already here?
SELECT id, name, photo, json_extract(attrs, '$.photo_back') FROM items WHERE a_discogs_release_id = 9100001;

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

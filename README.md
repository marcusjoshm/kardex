# Kardex

A light database of what I own and where it is kept: records, kitchen items, clothes,
movies, music and, later, adventure gear. Named after the card-file system for stock control.

One SQLite file and a small CLI. No server, no web app, no sign-in: Peems (the agent in
WOPR) adds, finds and moves items for me, looks up metadata from Discogs, MusicBrainz or
TMDB, and answers questions from the database. Kardex was PRIMS, a Flask web app; that
code is the git tag `v1-web`.

## Use

```sh
uv sync
uv run kardex add records "Kind of Blue" --location "Living room shelf" \
    --attr artist="Miles Davis" --attr year=1959 --attr format=LP
uv run kardex find "miles davis"
uv run kardex find --collection records --sort year --json
uv run kardex export        # plain-text backup into export/
uv run pytest
```

### Records from Discogs

I photograph the front and back of each sleeve; Peems reads the photos (barcode digits,
catalog number, label, country) and runs:

```sh
export DISCOGS_TOKEN=...    # Discogs personal access token
uv run kardex discogs match --barcode "0 74646 49351 7" --catno "CK 64935" --label Columbia
uv run kardex discogs add 9100003 --front IMG_0001.jpg --back IMG_0002.jpg --location "Living room shelf"
uv run kardex discogs sync  # what Discogs has that kardex lacks, and the other way round
```

`match` searches by barcode, then catalog number and label, then artist and title, ranks the
pressings by how many of the given fields agree and says whether one is a `confident` match;
if not, Peems sends me the short list. `add` puts the release in my Discogs collection
(unless it is already there, or with `--no-discogs`) and adds one record to kardex with its
artist, label, catno, barcode, country, year, format, pressing and Discogs ids, refusing a
release already in kardex unless `--copy`. The two photos are copied into `$KARDEX_PHOTOS`
(default `~/.local/share/kardex/photos/`) as `<id>-front.<ext>` and `<id>-back.<ext>`; the
item's `photo` is the front, `attrs.photo_back` the back. The tests use recorded Discogs
responses in `tests/fixtures/discogs/` and never reach the network.

The database is `$KARDEX_DB`, default `~/.local/share/kardex/kardex.db`.

Commands: `add`, `find`, `show`, `edit`, `move`, `remove`, `collections`, `locations`,
`export`, and `discogs match|add|sync`. Every command takes `--json`. The CLI validates (known collections, attribute
keys per collection, locations created on first use); it does not hide SQL, and `sqlite3`
works directly on the file.

[`SCHEMA.md`](SCHEMA.md) has the tables, the collections and their attribute keys, and
example CLI and SQL calls. [`CONCEPTS.md`](CONCEPTS.md) has the vocabulary.

## Layout

```
src/kardex/   schema.py (collections, SQL), db.py (operations, migration), cli.py, export.py,
              discogs.py (Discogs client, match ranking, add, sync)
tests/        pytest; fixtures/discogs/ has the recorded Discogs responses
export/       JSONL backup, one file per collection (written by `kardex export`)
```

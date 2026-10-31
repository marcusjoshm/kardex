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

The database is `$KARDEX_DB`, default `~/.local/share/kardex/kardex.db`.

Commands: `add`, `find`, `show`, `edit`, `move`, `remove`, `collections`, `locations`,
`export`. Every command takes `--json`. The CLI validates (known collections, attribute
keys per collection, locations created on first use); it does not hide SQL, and `sqlite3`
works directly on the file.

[`SCHEMA.md`](SCHEMA.md) has the tables, the collections and their attribute keys, and
example CLI and SQL calls. [`CONCEPTS.md`](CONCEPTS.md) has the vocabulary.

## Layout

```
src/kardex/   schema.py (collections, SQL), db.py (operations), cli.py, export.py
tests/        pytest
export/       JSONL backup, one file per collection (written by `kardex export`)
```

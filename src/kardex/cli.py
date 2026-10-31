"""The kardex command line."""

import argparse
import json
import sys

from . import db
from .export import export as run_export


def _fmt_item(i: dict) -> str:
    where = f" @ {i['location']}" if i["location"] else ""
    qty = f" x{i['quantity']}" if i["quantity"] != 1 else ""
    cat = f" [{i['category']}]" if i["category"] else ""
    attrs = ", ".join(f"{k}={v}" for k, v in sorted(i["attrs"].items()))
    return f"#{i['id']} {i['collection']}{cat} {i['name']}{qty}{where}" + (f" ({attrs})" if attrs else "")


def _show(i: dict) -> str:
    lines = [_fmt_item(i)]
    for key in ("notes", "photo", "added", "updated"):
        if i[key]:
            lines.append(f"  {key}: {i[key]}")
    return "\n".join(lines)


def _parser() -> argparse.ArgumentParser:
    # --db and --json are accepted before or after the command.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", default=argparse.SUPPRESS, help="database file (default: $KARDEX_DB)")
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="JSON output")

    p = argparse.ArgumentParser(prog="kardex", description="What I own, and where it is.", parents=[common])
    sub = p.add_subparsers(dest="cmd", required=True)

    def fields(sp, adding):
        sp.add_argument("--category", help="path under the collection, e.g. jazz/blue-note")
        sp.add_argument("--qty", type=int, help="count; only for interchangeable things")
        sp.add_argument("--notes")
        sp.add_argument("--photo", help="path to a photo")
        sp.add_argument("--attr", action="append", default=[], metavar="KEY=VALUE",
                        help="collection attribute" + ("" if adding else "; KEY= removes it"))

    a = sub.add_parser("add", parents=[common], help="add an item")
    a.add_argument("collection"); a.add_argument("name")
    a.add_argument("--location"); fields(a, True)

    f = sub.add_parser("find", parents=[common], help="search and list items")
    f.add_argument("query", nargs="?", help="words to search for")
    f.add_argument("--collection"); f.add_argument("--category"); f.add_argument("--location")
    f.add_argument("--sort", help=f"one of: {', '.join(db.SORTS)}")
    f.add_argument("--desc", action="store_true"); f.add_argument("--limit", type=int)

    s = sub.add_parser("show", parents=[common], help="show one item"); s.add_argument("id", type=int)

    e = sub.add_parser("edit", parents=[common], help="change an item; empty value clears a field")
    e.add_argument("id", type=int); e.add_argument("--name"); e.add_argument("--location")
    fields(e, False)

    m = sub.add_parser("move", parents=[common], help="set an item's location")
    m.add_argument("id", type=int); m.add_argument("location")

    r = sub.add_parser("remove", parents=[common], help="delete an item"); r.add_argument("id", type=int)

    sub.add_parser("collections", parents=[common], help="list collections and their attribute keys")
    sub.add_parser("locations", parents=[common], help="list locations with item counts")

    x = sub.add_parser("export", parents=[common], help="write the database as JSONL")
    x.add_argument("--dir", help="output folder (default: export/ in the repo, or $KARDEX_EXPORT)")
    return p


def _run(args, conn):
    """Return (data for --json, text for people)."""
    c = args.cmd
    if c == "add":
        item = db.add(conn, args.collection, args.name, category=args.category, location=args.location,
                      quantity=1 if args.qty is None else args.qty, notes=args.notes, photo=args.photo,
                      attrs=db.parse_attrs(db.check_collection(args.collection), args.attr))
        return item, _fmt_item(item)
    if c == "find":
        items = db.find(conn, args.query, collection=args.collection, category=args.category,
                        location=args.location, sort=args.sort, desc=args.desc, limit=args.limit)
        return items, "\n".join(map(_fmt_item, items)) or "no matches"
    if c == "show":
        item = db.get(conn, args.id)
        return item, _show(item)
    if c == "edit":
        current = db.get(conn, args.id)
        item = db.edit(conn, args.id, name=args.name, category=args.category, quantity=args.qty,
                       notes=args.notes, photo=args.photo, location=args.location,
                       attrs=db.parse_attrs(current["collection"], args.attr, allow_unset=True))
        return item, _fmt_item(item)
    if c == "move":
        item = db.move(conn, args.id, args.location)
        return item, _fmt_item(item)
    if c == "remove":
        item = db.remove(conn, args.id)
        return item, "removed " + _fmt_item(item)
    if c == "collections":
        rows = db.collections(conn)
        return rows, "\n".join(f"{r['collection']}: {r['items']} items; attrs: {', '.join(r['attrs'])}" for r in rows)
    if c == "locations":
        rows = db.locations(conn)
        return rows, "\n".join(f"{r['name']}: {r['items']}" for r in rows) or "no locations"
    counts = run_export(conn, args.dir)
    return counts, "\n".join(f"{k}: {v}" for k, v in counts.items())


def main(argv=None) -> int:
    args = _parser().parse_args(argv)
    as_json = getattr(args, "json", False)
    try:
        conn = db.connect(getattr(args, "db", None))
        try:
            data, text = _run(args, conn)
        finally:
            conn.close()
    except db.KardexError as e:
        print(json.dumps({"error": str(e)}) if as_json else f"kardex: {e}", file=sys.stderr)
        return 1
    print(json.dumps(data, ensure_ascii=False, sort_keys=True) if as_json else text)
    return 0


if __name__ == "__main__":
    sys.exit(main())

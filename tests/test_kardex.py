import json
import sqlite3

import pytest

from kardex import cli, db
from kardex.export import export

SEED = [
    ("Kind of Blue", dict(category="jazz/modal", location="Living room shelf", notes="Mono reissue",
                          attrs=dict(artist="Miles Davis", label="Columbia", year=1959, format="LP"))),
    ("Blue Train", dict(category="jazz/hard-bop", location="Living room shelf",
                        attrs=dict(artist="John Coltrane", label="Blue Note", year=1958, format="LP",
                                   pressing="1st stereo"))),
    ("Rumours", dict(category="rock", location="Bedroom crate",
                     attrs=dict(artist="Fleetwood Mac", label="Warner", year=1977, format="LP"))),
]


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "k.db")
    for name, kw in SEED:
        db.add(c, "records", name, **kw)
    yield c
    c.close()


def names(items):
    return [i["name"] for i in items]


def test_add_and_get(conn):
    item = db.get(conn, 1)
    assert item["name"] == "Kind of Blue" and item["attrs"]["year"] == 1959
    assert item["location"] == "Living room shelf" and item["quantity"] == 1
    assert item["added"] == item["updated"]


def test_validation(conn):
    with pytest.raises(db.KardexError, match="unknown collection"):
        db.add(conn, "cars", "x")
    with pytest.raises(db.KardexError, match="not an attribute"):
        db.parse_attrs("records", ["director=Kubrick"])
    with pytest.raises(db.KardexError, match="must be int"):
        db.parse_attrs("records", ["year=nineteen"])
    with pytest.raises(db.KardexError, match="negative"):
        db.add(conn, "kitchen", "glass", quantity=-1)
    assert db.parse_attrs("gear", ["weight=850", "last_used=2026-10-01"]) == {"weight": 850, "last_used": "2026-10-01"}
    with pytest.raises(db.KardexError):
        db.parse_attrs("gear", ["last_used=yesterday"])


def test_fts_finds_by_any_field(conn):
    assert names(db.find(conn, "miles")) == ["Kind of Blue"]            # attr
    assert names(db.find(conn, "mono")) == ["Kind of Blue"]             # notes
    assert names(db.find(conn, "bedroom")) == ["Rumours"]               # location
    assert names(db.find(conn, "hard-bop")) == ["Blue Train"]           # category
    assert sorted(names(db.find(conn, "blue"))) == ["Blue Train", "Kind of Blue"]
    assert names(db.find(conn, 'blue note "')) == ["Blue Train"]        # punctuation is safe
    assert db.find(conn, "nothing-here") == []


def test_fts_follows_edits_moves_and_removal(conn):
    db.edit(conn, 3, notes="signed copy", attrs={"artist": "Stevie Nicks"})
    assert names(db.find(conn, "signed")) == ["Rumours"]
    assert names(db.find(conn, "nicks")) == ["Rumours"]
    assert db.find(conn, "fleetwood") == []
    db.move(conn, 3, "Attic")
    assert names(db.find(conn, "attic")) == ["Rumours"]
    assert db.find(conn, "bedroom") == []
    conn.execute("UPDATE locations SET name = 'Garage' WHERE name = 'Attic'")  # raw SQL stays in sync
    assert names(db.find(conn, "garage")) == ["Rumours"]
    db.remove(conn, 3)
    assert db.find(conn, "garage") == []
    assert conn.execute("SELECT count(*) FROM items_fts").fetchone()[0] == 2


def test_sorting_and_filters(conn):
    assert names(db.find(conn, collection="records", sort="year")) == ["Blue Train", "Kind of Blue", "Rumours"]
    assert names(db.find(conn, sort="artist"))[0] == "Rumours"
    assert names(db.find(conn, category="jazz")) == ["Blue Train", "Kind of Blue"]
    assert names(db.find(conn, category="jazz/modal")) == ["Kind of Blue"]
    assert db.find(conn, category="jaz") == []
    assert names(db.find(conn, location="bedroom crate")) == ["Rumours"]
    db.add(conn, "movies", "Alien", attrs={"director": "Ridley Scott", "year": 1979})
    assert names(db.find(conn, collection="movies", sort="director")) == ["Alien"]
    with pytest.raises(db.KardexError):
        db.find(conn, sort="bogus")


def test_year_is_indexed_and_generated(conn):
    plan = conn.execute("EXPLAIN QUERY PLAN SELECT * FROM items WHERE a_year = 1959").fetchall()
    assert "idx_items_a_year" in " ".join(str(tuple(r)) for r in plan)


def test_edit_unset_and_clear(conn):
    db.edit(conn, 2, attrs={"pressing": None}, notes="", category="")
    item = db.get(conn, 2)
    assert "pressing" not in item["attrs"] and item["notes"] is None and item["category"] is None
    item = db.edit(conn, 2, quantity=2)
    assert item["quantity"] == 2


def test_locations_created_on_first_use_case_insensitive(conn):
    db.add(conn, "kitchen", "Pint glass", quantity=6, location="living room shelf")
    rows = {r["name"]: r["items"] for r in db.locations(conn)}
    assert rows == {"Bedroom crate": 1, "Living room shelf": 3}
    assert {c["collection"]: c["quantity"] for c in db.collections(conn)}["kitchen"] == 6


def test_export_is_deterministic(conn, tmp_path):
    out = tmp_path / "export"
    assert export(conn, out)["records"] == 3
    first = {p.name: p.read_bytes() for p in out.iterdir()}
    export(conn, out)
    assert first == {p.name: p.read_bytes() for p in out.iterdir()}
    assert sorted(first) == ["clothes.jsonl", "gear.jsonl", "kitchen.jsonl", "movies.jsonl",
                             "music.jsonl", "records.jsonl"]
    lines = first["records.jsonl"].decode().splitlines()
    assert [json.loads(l)["id"] for l in lines] == [1, 2, 3]
    assert lines[0] == json.dumps(json.loads(lines[0]), sort_keys=True, ensure_ascii=False)
    assert first["gear.jsonl"] == b""


def test_cli_json_roundtrip(tmp_path, capsys, monkeypatch):
    path = str(tmp_path / "c.db")
    monkeypatch.setenv("KARDEX_DB", path)

    def run(*argv):
        code = cli.main(list(argv))
        return code, capsys.readouterr()

    code, out = run("add", "records", "Kind of Blue", "--json", "--location", "Shelf",
                    "--attr", "artist=Miles Davis", "--attr", "year=1959")
    assert code == 0 and json.loads(out.out)["id"] == 1
    code, out = run("--json", "find", "davis")
    assert [i["name"] for i in json.loads(out.out)] == ["Kind of Blue"]
    code, out = run("move", "1", "Attic", "--json")
    assert json.loads(out.out)["location"] == "Attic"
    code, out = run("edit", "1", "--attr", "year=", "--json")
    assert "year" not in json.loads(out.out)["attrs"]
    code, out = run("collections", "--json")
    assert json.loads(out.out)[0]["collection"] == "records"
    code, out = run("locations")
    assert "Attic: 1" in out.out
    code, out = run("export", "--dir", str(tmp_path / "x"))
    assert code == 0 and (tmp_path / "x" / "records.jsonl").exists()
    code, out = run("show", "99", "--json")
    assert code == 1 and "no item" in json.loads(out.err)["error"]
    code, out = run("remove", "1")
    assert code == 0 and run("find")[1].out.strip() == "no matches"
    assert sqlite3.connect(path).execute("select count(*) from items").fetchone()[0] == 0

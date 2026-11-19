"""Discogs intake, against recorded fixtures: the client, match ranking, add, sync, migration."""
import json
import sqlite3
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import pytest

from kardex import cli, db, discogs

FIXTURES = Path(__file__).parent / "fixtures"
USER = "/users/example_digger"


def fixture(name: str) -> bytes:
    return (FIXTURES / "discogs" / name).read_bytes()


class FakeDiscogs:
    """Stands in for Client._send. Routes are (method, path, {param: value}, response), where
    response is a fixture name, or (status, fixture name[, headers]), or a list of those served
    in turn (the last one repeats). First matching route wins."""

    def __init__(self, routes, remaining="59"):
        self.routes = list(routes)
        self.remaining = remaining
        self.calls = []

    def __call__(self, client, method, url, headers, body):
        u = urlsplit(url)
        params = dict(parse_qsl(u.query))
        self.calls.append({"method": method, "path": u.path, "params": params, "headers": headers, "url": url})
        for m, path, want, resp in self.routes:
            if m == method and path == u.path and all(params.get(k) == v for k, v in want.items()):
                if isinstance(resp, list):
                    resp = resp.pop(0) if len(resp) > 1 else resp[0]
                status, name, extra = (200, resp, {}) if isinstance(resp, str) else (resp + ({},))[:3]
                return status, {"X-Discogs-Ratelimit-Remaining": self.remaining, **extra}, fixture(name)
        return 404, {}, fixture("error_404.json")

    def paths(self, method=None):
        return [c["path"] for c in self.calls if method in (None, c["method"])]


@pytest.fixture
def fake(monkeypatch):
    def install(routes, **kw):
        f = FakeDiscogs(routes, **kw)
        monkeypatch.setattr(discogs.Client, "_send", lambda self, *a: f(self, *a))
        return f
    return install


@pytest.fixture
def client():
    slept = []
    c = discogs.Client("test-token", sleep=slept.append)
    c.slept = slept
    return c


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "k.db")
    yield c
    c.close()


@pytest.fixture
def photos(tmp_path, monkeypatch):
    monkeypatch.setenv("KARDEX_PHOTOS", str(tmp_path / "photos"))
    src = tmp_path / "phone"
    src.mkdir()
    (src / "IMG_0001.JPG").write_bytes(b"front-bytes")
    (src / "IMG_0002.heic").write_bytes(b"back-bytes")
    return src / "IMG_0001.JPG", src / "IMG_0002.heic"


SEARCH = "/database/search"
ADD_ROUTES = [
    ("GET", "/oauth/identity", {}, "identity.json"),
    ("GET", "/releases/9100001", {}, "release_9100001.json"),
    ("GET", "/releases/9100003", {}, "release_9100003.json"),
]


# -- the client ---------------------------------------------------------------------------------

def test_client_sends_token_user_agent_and_vinyl_search(fake, client):
    f = fake([("GET", SEARCH, {"barcode": "074646493517"}, "search_barcode.json")])
    results = client.search(barcode="074646493517")
    assert [r["id"] for r in results] == [9100003]
    call = f.calls[0]
    assert call["url"].startswith("https://api.discogs.com/database/search?")
    assert call["headers"]["Authorization"] == "Discogs token=test-token"
    assert call["headers"]["User-Agent"] == "kardex/2 +https://github.com/marcusjoshm/kardex"
    assert call["params"] == {"type": "release", "format": "Vinyl", "barcode": "074646493517", "per_page": "50"}


def test_client_identity_add_and_collection_paths(fake, client):
    f = fake([("GET", "/oauth/identity", {}, "identity.json"),
              ("POST", f"{USER}/collection/folders/1/releases/9100001", {}, (201, "add_to_collection.json")),
              ("GET", f"{USER}/collection/releases/9100001", {}, "collection_release_9100001.json")])
    assert client.username() == "example_digger"
    assert client.add_to_collection(9100001) == 777001
    assert [r["instance_id"] for r in client.collection_instances(9100001)] == [555001]
    assert client.collection_instances(9100002) == []          # 404 means not in the collection
    assert f.paths().count("/oauth/identity") == 1               # the username is cached


def test_client_needs_a_token(monkeypatch, fake):
    fake([])
    monkeypatch.delenv("DISCOGS_TOKEN", raising=False)
    with pytest.raises(discogs.DiscogsError, match="DISCOGS_TOKEN"):
        discogs.Client().identity()
    monkeypatch.setenv("DISCOGS_TOKEN", "from-env")
    assert discogs.Client().token == "from-env"


def test_client_reports_http_errors(fake, client):
    fake([])
    with pytest.raises(discogs.DiscogsError, match="HTTP 404: The requested resource was not found"):
        client.release(1)


def test_client_survives_a_non_json_error_page(fake, client, monkeypatch):
    fake([])
    monkeypatch.setattr(discogs.Client, "_send", lambda self, *a: (502, {}, b"<html>Bad Gateway</html>"))
    with pytest.raises(discogs.DiscogsError, match="HTTP 502"):
        client.identity()


def test_client_backs_off_on_429_then_succeeds(fake, client):
    too_fast = (429, "error_429.json", {"Retry-After": "7"})
    f = fake([("GET", "/oauth/identity", {}, [too_fast, too_fast, "identity.json"])])
    assert client.identity()["username"] == "example_digger"
    assert client.slept == [7.0, 7.0] and len(f.calls) == 3 and client.remaining == 59


def test_client_gives_up_after_repeated_429(fake, client):
    fake([("GET", "/oauth/identity", {}, (429, "error_429.json"))])
    with pytest.raises(discogs.DiscogsError, match="rate limit"):
        client.identity()
    assert client.slept == [2.0, 4.0, 8.0, 16.0]


def test_client_paces_by_ratelimit_remaining(fake, client):
    f = fake([("GET", "/oauth/identity", {}, "identity.json")], remaining="3")
    client.identity(); client.identity()
    assert client.slept == [1.0]                   # few left: one request a second
    f.remaining = "0"
    client.identity(); client.identity()
    assert client.slept == [1.0, 1.0, 60.0]        # none left: wait for the window to roll over


# -- match ranking ------------------------------------------------------------------------------

def test_match_barcode_is_confident(fake, client):
    f = fake([("GET", SEARCH, {"barcode": "074646493517"}, "search_barcode.json")])
    res = discogs.match(client, barcode="0 74646 49351 7", artist="Miles Davis", title="Kind of Blue")
    assert res["confident"] and res["release_id"] == 9100003
    top = res["candidates"][0]
    assert top["agrees"] == ["barcode", "artist", "title"] and top["confident"]
    assert top["label"] == "Columbia" and top["catno"] == "CK 64935" and top["country"] == "US"
    assert top["year"] == 1997 and top["format"].startswith("Vinyl, LP")
    assert res["searched"] == ["barcode"] and len(f.calls) == 1     # stopped at the first pass


def test_match_falls_through_strongest_first(fake, client):
    f = fake([("GET", SEARCH, {"barcode": "123456789012"}, "search_empty.json"),
              ("GET", SEARCH, {"catno": "CS 8163", "label": "Columbia"}, "search_catno_unique.json")])
    res = discogs.match(client, barcode="123456789012", catno="CS 8163", label="Columbia",
                        artist="Miles Davis", title="Kind Of Blue")
    assert [c["params"].get("barcode") or c["params"].get("catno") for c in f.calls] == ["123456789012", "CS 8163"]
    assert res["searched"] == ["barcode", "catno+label"]
    assert res["confident"] and res["release_id"] == 9100006


def test_match_several_pressings_is_not_confident(fake, client):
    fake([("GET", SEARCH, {"catno": "CL-1355", "label": "Columbia Records"}, "search_catno_label.json")])
    res = discogs.match(client, catno="CL-1355", label="Columbia Records", country="Canada", year=1959)
    ids = [c["release_id"] for c in res["candidates"]]
    # Canada agrees on four fields, the US 1959 on three, the 1964 repress on two, the sampler on one.
    assert ids == [9100005, 9100001, 9100002, 9100009]
    assert [c["score"] for c in res["candidates"]] == [4, 3, 2, 1]
    # Three pressings agree on catno+label, so it is a short list to pick from, not a match.
    assert not res["confident"] and res["release_id"] is None
    assert not any(c["confident"] for c in res["candidates"])


def test_match_tie_is_not_confident(client):
    rows = json.loads(fixture("search_barcode.json"))["results"]
    twin = dict(rows[0], id=9100004)                  # two releases sharing one barcode
    cands, confident = discogs.rank(rows + [twin], {"barcode": "074646493517"})
    assert not confident and [c["score"] for c in cands] == [1, 1]
    # A strong candidate outscored by a weak one is not confident either.
    rows = json.loads(fixture("search_catno_label.json"))["results"]
    cands, confident = discogs.rank(rows[3:] + rows[:1], {"catno": "CL 1355", "label": "Fontana"})
    assert confident and cands[0]["release_id"] == 9100009


def test_match_artist_title_only_ranks_without_confidence(fake, client):
    fake([("GET", SEARCH, {"artist": "Miles Davis", "release_title": "Kind of Blue"}, "search_artist_title.json")])
    res = discogs.match(client, artist="Miles Davis", title="Kind of Blue", country="Japan", limit=2)
    assert [c["release_id"] for c in res["candidates"]] == [9100008, 9100001]
    assert res["candidates"][0]["artist"] == "Miles Davis"       # the '(2)' is dropped
    assert not res["confident"] and len(res["candidates"]) == 2


def test_match_needs_something(client):
    with pytest.raises(db.KardexError, match="at least one"):
        discogs.match(client, year=1959)


# -- add ----------------------------------------------------------------------------------------

def test_add_new_release_adds_to_discogs_and_kardex_with_photos(fake, client, conn, photos, tmp_path):
    front, back = photos
    f = fake(ADD_ROUTES + [
        ("GET", f"{USER}/collection/releases/9100001", {}, "collection_release_none.json"),
        ("POST", f"{USER}/collection/folders/1/releases/9100001", {}, (201, "add_to_collection.json"))])
    item = discogs.add(conn, client, 9100001, front=front, back=back, location="Living room shelf",
                       category="jazz/modal", notes="Cover has ring wear")
    assert f.paths("POST") == [f"{USER}/collection/folders/1/releases/9100001"]
    assert item["name"] == "Kind Of Blue" and item["collection"] == "records"
    assert item["location"] == "Living room shelf" and item["category"] == "jazz/modal"
    a = item["attrs"]
    assert {k: v for k, v in a.items() if k != "photo_back"} == {
        "artist": "Miles Davis", "label": "Columbia", "catno": "CL 1355", "country": "US", "year": 1959,
        "format": "LP", "pressing": "US, Mono, 6-Eye", "discogs_release_id": 9100001,
        "discogs_instance_id": 777001}
    photos_dir = tmp_path / "photos"
    assert item["photo"] == str((photos_dir / f"{item['id']}-front.jpg").resolve())
    assert a["photo_back"] == str((photos_dir / f"{item['id']}-back.heic").resolve())
    assert Path(item["photo"]).read_bytes() == b"front-bytes"
    assert Path(a["photo_back"]).read_bytes() == b"back-bytes"
    assert front.exists()                                          # copied, not moved
    assert [i["id"] for i in db.find(conn, "cl 1355")] == [item["id"]]   # catno is searchable
    assert conn.execute("SELECT a_discogs_release_id FROM items").fetchone()[0] == 9100001


def test_add_already_in_collection_does_not_post(fake, client, conn):
    f = fake(ADD_ROUTES + [("GET", f"{USER}/collection/releases/9100001", {}, "collection_release_9100001.json")])
    item = discogs.add(conn, client, 9100001)
    assert f.paths("POST") == []
    assert item["attrs"]["discogs_instance_id"] == 555001 and item["photo"] is None


def test_add_reissue_attrs(fake, client, conn):
    fake(ADD_ROUTES)
    item = discogs.add(conn, client, 9100003, no_discogs=True)
    a = item["attrs"]
    assert a["barcode"] == "074646493517" and a["catno"] == "CK 64935" and a["format"] == "2xLP"
    assert a["pressing"] == "US, Reissue, Remastered, 180 Gram" and a["year"] == 1997


def test_add_no_discogs_skips_the_remote_write(fake, client, conn):
    f = fake(ADD_ROUTES)
    item = discogs.add(conn, client, 9100001, no_discogs=True)
    assert f.paths() == ["/releases/9100001"]                       # only the read
    assert "discogs_instance_id" not in item["attrs"]


def test_add_refuses_duplicate_unless_copy(fake, client, conn):
    f = fake(ADD_ROUTES + [
        ("GET", f"{USER}/collection/releases/9100001", {}, "collection_release_9100001.json"),
        ("POST", f"{USER}/collection/folders/1/releases/9100001", {}, (201, "add_to_collection.json"))])
    first = discogs.add(conn, client, 9100001)
    calls = len(f.calls)
    with pytest.raises(db.KardexError, match=f"already in kardex as #{first['id']}.*--copy"):
        discogs.add(conn, client, 9100001)
    assert len(f.calls) == calls                                     # refused before any request
    second = discogs.add(conn, client, 9100001, copy=True)
    assert second["id"] != first["id"]
    # The one Discogs instance belongs to the first copy, so the second gets a new one.
    assert (first["attrs"]["discogs_instance_id"], second["attrs"]["discogs_instance_id"]) == (555001, 777001)
    assert f.paths("POST") == [f"{USER}/collection/folders/1/releases/9100001"]
    assert discogs.in_kardex(conn, 9100001) == [first["id"], second["id"]]


def test_add_missing_photo_fails_before_any_write(fake, client, conn, tmp_path):
    f = fake(ADD_ROUTES)
    with pytest.raises(db.KardexError, match="no photo"):
        discogs.add(conn, client, 9100001, front=tmp_path / "nope.jpg")
    assert f.calls == [] and db.find(conn) == []


# -- sync and the CLI ---------------------------------------------------------------------------

def test_sync_compares_both_ways(fake, client, conn):
    fake([("GET", "/oauth/identity", {}, "identity.json"),
          ("GET", f"{USER}/collection/folders/0/releases", {"page": "1"}, "collection_page1.json"),
          ("GET", f"{USER}/collection/folders/0/releases", {"page": "2"}, "collection_page2.json")])
    db.add(conn, "records", "Kind Of Blue", attrs={"discogs_release_id": 9100001})
    db.add(conn, "records", "Blue Train", attrs={"discogs_release_id": 9200000})
    db.add(conn, "records", "Rumours", attrs={"artist": "Fleetwood Mac"})
    res = discogs.sync(conn, client)
    assert [r["release_id"] for r in res["discogs_only"]] == [9100006, 9100007]
    assert res["discogs_only"][1] == {"release_id": 9100007, "instance_id": 555003, "artist": "Miles Davis",
                                      "title": "Kind Of Blue", "year": 1971, "label": "CBS", "catno": "S 62066"}
    assert [i["name"] for i in res["kardex_only"]] == ["Blue Train"]
    assert [i["name"] for i in res["unlinked"]] == ["Rumours"]
    assert db.find(conn, collection="records", sort="id")[0]["attrs"] == {"discogs_release_id": 9100001}


def test_cli_discogs_match_and_add(fake, tmp_path, capsys, monkeypatch, photos):
    monkeypatch.setenv("KARDEX_DB", str(tmp_path / "c.db"))
    monkeypatch.setenv("DISCOGS_TOKEN", "test-token")
    fake(ADD_ROUTES + [("GET", SEARCH, {"barcode": "074646493517"}, "search_barcode.json"),
                       ("GET", f"{USER}/collection/releases/9100003", {}, "collection_release_none.json"),
                       ("POST", f"{USER}/collection/folders/1/releases/9100003", {}, (201, "add_to_collection.json"))])

    def run(*argv):
        code = cli.main(list(argv))
        return code, capsys.readouterr()

    code, out = run("discogs", "match", "--barcode", "074646493517", "--json")
    res = json.loads(out.out)
    assert code == 0 and res["confident"] and res["release_id"] == 9100003
    code, out = run("discogs", "match", "--barcode", "074646493517")
    assert out.out.startswith("confident: 9100003") and "*confident*" in out.out
    front, back = photos
    code, out = run("--json", "discogs", "add", "9100003", "--front", str(front), "--back", str(back),
                    "--location", "Shelf")
    item = json.loads(out.out)
    assert code == 0 and item["attrs"]["discogs_instance_id"] == 777001 and item["photo"].endswith("1-front.jpg")
    code, out = run("discogs", "add", "9100003", "--json")
    assert code == 1 and "--copy" in json.loads(out.err)["error"]
    code, out = run("find", "--sort", "discogs_release_id", "--json")
    assert [i["id"] for i in json.loads(out.out)] == [1]


def test_cli_discogs_without_token(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("KARDEX_DB", str(tmp_path / "c.db"))
    monkeypatch.delenv("DISCOGS_TOKEN", raising=False)
    assert cli.main(["discogs", "match", "--artist", "Miles Davis"]) == 1
    assert "DISCOGS_TOKEN" in capsys.readouterr().err


# -- migration ----------------------------------------------------------------------------------

def test_old_database_migrates_without_losing_rows(tmp_path):
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript((FIXTURES / "kardex_v2_schema.sql").read_text())
    old.execute("INSERT INTO locations(name) VALUES ('Shelf')")
    old.execute("INSERT INTO items(name, collection, location_id, attrs, added, updated) VALUES"
                " ('Kind of Blue', 'records', 1, '{\"artist\": \"Miles Davis\", \"year\": 1959}', 't', 't'),"
                " ('Alien', 'movies', NULL, '{\"director\": \"Ridley Scott\"}', 't', 't')")
    old.commit()
    assert "a_discogs_release_id" not in {r[1] for r in old.execute("PRAGMA table_xinfo(items)")}
    old.close()

    conn = db.connect(path)
    cols = {r["name"]: r["hidden"] for r in conn.execute("PRAGMA table_xinfo(items)")}
    assert cols["a_discogs_release_id"] == 2                        # a VIRTUAL generated column
    assert [i["name"] for i in db.find(conn, sort="id")] == ["Kind of Blue", "Alien"]
    assert db.find(conn, "miles")[0]["attrs"] == {"artist": "Miles Davis", "year": 1959}
    db.edit(conn, 1, attrs={"discogs_release_id": 9100001})
    plan = conn.execute("EXPLAIN QUERY PLAN SELECT id FROM items WHERE a_discogs_release_id = 9100001").fetchall()
    assert "idx_items_a_discogs_release_id" in " ".join(str(tuple(r)) for r in plan)
    assert discogs.in_kardex(conn, 9100001) == [1]
    assert db.migrate(conn) == []                                   # idempotent
    conn.close()
    assert db.connect(path).execute("SELECT count(*) FROM items").fetchone()[0] == 2

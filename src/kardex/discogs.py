"""Discogs intake: a small API client, release matching, and adding a record to kardex.

Standard library only. Every HTTP call goes through `Client._send`, the one function that
touches the network; the tests replace it with recorded fixtures.
"""

import json
import os
import re
import shutil
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import db

API = "https://api.discogs.com"
USER_AGENT = "kardex/2 +https://github.com/marcusjoshm/kardex"
DEFAULT_PHOTOS = Path("~/.local/share/kardex/photos").expanduser()

# Discogs allows 60 authenticated requests per minute, counted over a moving 60 s window.
WINDOW = 60.0
LOW_REMAINING = 5      # below this many requests left in the window, pace one per second
MAX_RETRIES = 4        # on 429 Too Many Requests


class DiscogsError(db.KardexError):
    pass


class Client:
    """The Discogs calls kardex needs. The token is a personal access token, from
    `DISCOGS_TOKEN` unless given; it is only required when a request is made."""

    def __init__(self, token: str | None = None, *, base: str = API, sleep=time.sleep):
        self.token = token if token is not None else os.environ.get("DISCOGS_TOKEN", "")
        self.base = base.rstrip("/")
        self.sleep = sleep
        self.remaining: int | None = None   # X-Discogs-Ratelimit-Remaining from the last response
        self._username: str | None = None

    # -- transport ------------------------------------------------------------------------

    def _send(self, method: str, url: str, headers: dict, body: bytes | None):
        """Make one HTTP request. Returns (status, headers, body bytes); never raises on an
        HTTP error status. The only function here that uses the network."""
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers or {}), e.read()
        except OSError as e:   # URLError, timeouts, refused connections
            raise DiscogsError(f"cannot reach Discogs: {getattr(e, 'reason', e)}") from None

    def request(self, method: str, path: str, params: dict | None = None, data: dict | None = None):
        """Call the API and return the decoded JSON. Paces itself by the rate-limit headers
        and retries a 429 with backoff."""
        if not self.token:
            raise DiscogsError("DISCOGS_TOKEN is not set (a Discogs personal access token)")
        query = {k: v for k, v in (params or {}).items() if v not in (None, "")}
        url = self.base + path + ("?" + urllib.parse.urlencode(query) if query else "")
        headers = {"Authorization": f"Discogs token={self.token}", "User-Agent": USER_AGENT,
                   "Accept": "application/json"}
        body = None
        if data is not None:
            body = json.dumps(data).encode()
            headers["Content-Type"] = "application/json"
        if method == "POST" and body is None:
            body = b""

        for attempt in range(MAX_RETRIES + 1):
            self._pace()
            status, resp_headers, raw = self._send(method, url, headers, body)
            self._note_limit(resp_headers)
            if status != 429:
                break
            if attempt == MAX_RETRIES:
                raise DiscogsError("Discogs rate limit: still 429 after retries")
            retry_after = _header(resp_headers, "Retry-After")
            wait = float(retry_after) if retry_after and retry_after.isdigit() else 2.0 ** (attempt + 1)
            self.sleep(wait)
            self.remaining = None

        try:
            payload = json.loads(raw) if raw and raw.strip() else {}
        except ValueError:
            if status < 400:
                raise DiscogsError(f"Discogs {method} {path}: response is not JSON") from None
            payload = {}
        if status >= 400:
            message = payload.get("message") if isinstance(payload, dict) else None
            raise DiscogsError(f"Discogs {method} {path}: HTTP {status}" + (f": {message}" if message else ""))
        return payload

    def _pace(self):
        if self.remaining is None:
            return
        if self.remaining <= 0:
            self.sleep(WINDOW)          # the window has to roll over
            self.remaining = None
        elif self.remaining < LOW_REMAINING:
            self.sleep(WINDOW / 60)     # one request a second keeps us under 60/min

    def _note_limit(self, headers: dict):
        value = _header(headers, "X-Discogs-Ratelimit-Remaining")
        if value is not None and value.strip().lstrip("-").isdigit():
            self.remaining = int(value)

    # -- calls ----------------------------------------------------------------------------

    def identity(self) -> dict:
        return self.request("GET", "/oauth/identity")

    def username(self) -> str:
        if self._username is None:
            self._username = self.identity()["username"]
        return self._username

    def search(self, *, barcode=None, catno=None, label=None, artist=None, release_title=None,
               per_page: int = 50) -> list[dict]:
        """Database search for vinyl releases; returns the `results` list (first page)."""
        params = dict(type="release", format="Vinyl", barcode=barcode, catno=catno, label=label,
                      artist=artist, release_title=release_title, per_page=per_page)
        return self.request("GET", "/database/search", params).get("results", [])

    def release(self, release_id: int) -> dict:
        return self.request("GET", f"/releases/{int(release_id)}")

    def collection_instances(self, release_id: int) -> list[dict]:
        """The instances of a release in the user's collection (empty if it is not there)."""
        try:
            data = self.request("GET", f"/users/{_q(self.username())}/collection/releases/{int(release_id)}")
        except DiscogsError as e:
            if "HTTP 404" in str(e):
                return []
            raise
        return data.get("releases", [])

    def add_to_collection(self, release_id: int, folder_id: int = 1) -> int:
        """Add a release to a collection folder (1 is Uncategorized); returns the instance_id."""
        data = self.request("POST", f"/users/{_q(self.username())}/collection/folders/{int(folder_id)}"
                                    f"/releases/{int(release_id)}")
        return int(data["instance_id"])

    def collection(self, per_page: int = 100) -> list[dict]:
        """Every instance in the user's collection (folder 0 is All), all pages."""
        out, page = [], 1
        while True:
            data = self.request("GET", f"/users/{_q(self.username())}/collection/folders/0/releases",
                                {"page": page, "per_page": per_page})
            out += data.get("releases", [])
            pages = data.get("pagination", {}).get("pages", 1)
            if page >= pages:
                return out
            page += 1


def _header(headers: dict, name: str) -> str | None:
    for k, v in (headers or {}).items():
        if k.lower() == name.lower():
            return v
    return None


def _q(s: str) -> str:
    return urllib.parse.quote(s, safe="")


# -- normalizing ------------------------------------------------------------------------------

COUNTRIES = {"usa": "us", "united states": "us", "united states of america": "us", "america": "us",
             "united kingdom": "uk", "england": "uk", "great britain": "uk", "gb": "uk",
             "deutschland": "germany", "de": "germany", "jp": "japan", "nippon": "japan",
             "fr": "france", "ca": "canada", "eu": "europe"}


def digits(s) -> str:
    return re.sub(r"\D", "", str(s or ""))


def clean_name(s: str) -> str:
    """Discogs display name to sleeve name: drop the '(2)' disambiguation and the ANV '*'."""
    return re.sub(r"\s*\(\d+\)$", "", (s or "").strip()).rstrip("*").strip()


def _norm_text(s) -> str:
    s = clean_name(str(s or "")).casefold().replace("&", " and ")
    s = re.sub(r"^the\s+", "", s)
    return re.sub(r"[\W_]+", " ", s).strip()


def _norm_catno(s) -> str:
    return re.sub(r"[\W_]+", "", str(s or "")).casefold()


def _norm_label(s) -> str:
    s = _norm_text(s)
    return re.sub(r"\s+(records|recordings|record company|music|inc|ltd)$", "", s).strip()


def _norm_country(s) -> str:
    s = str(s or "").strip().casefold()
    return COUNTRIES.get(s, s)


def barcode_agrees(given: str, candidates) -> bool:
    g = digits(given)
    if not g:
        return False
    for c in candidates or []:
        d = digits(c)
        # UPC-A (12 digits) and EAN-13 differ by a leading 0.
        if d and (d == g or d.lstrip("0") == g.lstrip("0")):
            return True
    return False


def _text_agrees(given, value, norm=_norm_text) -> bool:
    g, v = norm(given), norm(value)
    return bool(g and v) and (g == v or (len(g) >= 3 and g in v) or (len(v) >= 3 and v in g))


# -- matching -----------------------------------------------------------------------------------

FIELDS = ("barcode", "catno", "label", "artist", "title", "country", "year")


def candidate(result: dict) -> dict:
    """A search result in kardex's terms."""
    artist, sep, title = (result.get("title") or "").partition(" - ")
    if not sep:
        artist, title = "", artist
    labels = list(dict.fromkeys(clean_name(l) for l in result.get("label") or []))
    year = result.get("year")
    return {
        "release_id": result.get("id"),
        "artist": clean_name(artist),
        "title": title.strip(),
        "label": labels[0] if labels else None,
        "labels": labels,
        "catno": result.get("catno"),
        "country": result.get("country"),
        "year": int(year) if str(year or "").isdigit() and int(year) > 0 else None,
        "format": ", ".join(result.get("format") or []),
        "barcodes": result.get("barcode") or [],
        "have": (result.get("community") or {}).get("have", 0),
        "uri": result.get("uri"),
    }


def agreement(c: dict, given: dict) -> list[str]:
    """The given fields this candidate agrees with."""
    agree = []
    if given.get("barcode") and barcode_agrees(given["barcode"], c["barcodes"]):
        agree.append("barcode")
    if given.get("catno") and any(_norm_catno(given["catno"]) == _norm_catno(x)
                                  for x in re.split(r",\s*", c["catno"] or "") if x):
        agree.append("catno")
    if given.get("label") and any(_text_agrees(given["label"], l, _norm_label) for l in c["labels"]):
        agree.append("label")
    if given.get("artist") and _text_agrees(given["artist"], c["artist"]):
        agree.append("artist")
    if given.get("title") and _text_agrees(given["title"], c["title"]):
        agree.append("title")
    if given.get("country") and _norm_country(given["country"]) == _norm_country(c["country"]):
        agree.append("country")
    if given.get("year") and c["year"] == int(given["year"]):
        agree.append("year")
    return agree


def _strong(agree: list[str]) -> bool:
    return "barcode" in agree or ("catno" in agree and "label" in agree)


def rank(results: list[dict], given: dict) -> tuple[list[dict], bool]:
    """Rank search results by how many given fields agree. Confident means exactly one
    candidate agrees on the barcode or on catno+label, and no other candidate scores as high."""
    seen, cands = set(), []
    for r in results:
        if r.get("id") in seen or r.get("type", "release") != "release":
            continue
        seen.add(r.get("id"))
        c = candidate(r)
        c["agrees"] = agreement(c, given)
        c["score"] = len(c["agrees"])
        c["confident"] = False
        cands.append(c)
    cands.sort(key=lambda c: (-c["score"], not _strong(c["agrees"]), -(c["have"] or 0), c["release_id"] or 0))
    strong = [c for c in cands if _strong(c["agrees"])]
    confident = (len(strong) == 1 and strong[0] is cands[0]
                 and all(c["score"] < cands[0]["score"] for c in cands[1:]))
    if confident:
        cands[0]["confident"] = True
    return cands, confident


def match(client: Client, *, barcode=None, catno=None, label=None, artist=None, title=None,
          country=None, year=None, limit: int = 5) -> dict:
    """Find the release for what was read off a sleeve. Searches the strongest identifiers
    first (barcode, then catno+label, then artist+title) and stops once one is confident."""
    given = {k: v for k, v in dict(barcode=barcode, catno=catno, label=label, artist=artist, title=title,
                                   country=country, year=year).items() if v not in (None, "")}
    passes = []
    if digits(barcode):
        passes.append(("barcode", dict(barcode=digits(barcode))))
    if catno:
        passes.append(("catno+label" if label else "catno", dict(catno=catno, label=label)))
    if artist or title:
        passes.append(("artist+title", dict(artist=artist, release_title=title)))
    elif label and not catno:
        passes.append(("label", dict(label=label)))
    if not passes:
        raise db.KardexError("give at least one of --barcode, --catno, --label, --artist, --title "
                             "(--country and --year only rank)")

    results, searched = [], []
    cands, confident = [], False
    for name, params in passes:
        searched.append(name)
        results += client.search(**params)
        cands, confident = rank(results, given)
        if confident:
            break
    return {"given": given, "searched": searched, "confident": confident,
            "release_id": cands[0]["release_id"] if confident else None,
            "candidates": [_public(c) for c in cands[:limit]]}


def _public(c: dict) -> dict:
    keys = ("release_id", "artist", "title", "label", "catno", "country", "year", "format",
            "score", "agrees", "confident", "uri")
    return {k: c.get(k) for k in keys}


# -- adding a record ----------------------------------------------------------------------------

SIZES = ("LP", "EP", '12"', '10"', '7"', "Single")
NOT_PRESSING = {"LP", "EP", "Album", "Single", "Mini-Album", "Compilation", '12"', '10"', '7"',
                "33 ⅓ RPM", "45 RPM", "78 RPM"}


def photos_dir() -> Path:
    return Path(os.environ.get("KARDEX_PHOTOS") or DEFAULT_PHOTOS).expanduser()


def _artist(release: dict) -> str | None:
    parts = []
    for a in release.get("artists") or []:
        parts.append(clean_name(a.get("anv") or a.get("name") or ""))
        join = (a.get("join") or "").strip()
        if join:
            parts.append(join if join == "," else f" {join}")
        parts.append(" ")
    text = re.sub(r"\s+,", ",", "".join(parts)).strip()
    return text or clean_name(release.get("artists_sort") or "") or None


def _format(formats: list[dict]) -> str | None:
    if not formats:
        return None
    f = formats[0]
    desc = f.get("descriptions") or []
    size = next((s for s in SIZES if s in desc), None) or f.get("name")
    qty = str(f.get("qty") or "1")
    return f"{qty}x{size}" if qty.isdigit() and int(qty) > 1 else size


def _pressing(release: dict) -> str | None:
    parts = [release.get("country")] if release.get("country") else []
    for f in release.get("formats") or []:
        parts += [d for d in f.get("descriptions") or [] if d not in NOT_PRESSING]
        if f.get("text"):
            parts.append(f["text"])
    notes = (release.get("notes") or "").casefold()
    for word, tag in (("reissue", "Reissue"), ("repress", "Repress")):
        if word in notes and tag not in parts:
            parts.append(tag)
    parts = list(dict.fromkeys(p for p in parts if p))
    return ", ".join(parts) or None


def release_attrs(release: dict) -> dict:
    """kardex `records` attributes from a Discogs release."""
    labels = release.get("labels") or []
    label = clean_name(labels[0].get("name") or "") or None if labels else None
    catno = labels[0].get("catno") if labels else None
    barcode = next((i.get("value") for i in release.get("identifiers") or []
                    if (i.get("type") or "").lower() == "barcode" and i.get("value")), None)
    year = release.get("year")
    attrs = {
        "artist": _artist(release),
        "label": label,
        "catno": catno if catno and catno.lower() != "none" else None,
        "barcode": (digits(barcode) or barcode.strip()) if barcode else None,
        "country": release.get("country"),
        "year": int(year) if isinstance(year, int) and year > 0 else None,
        "format": _format(release.get("formats") or []),
        "pressing": _pressing(release),
        "discogs_release_id": int(release["id"]),
    }
    return {k: v for k, v in attrs.items() if v not in (None, "")}


def in_kardex(conn, release_id: int) -> list[int]:
    return [r["id"] for r in conn.execute(
        "SELECT id FROM items WHERE collection = 'records' AND a_discogs_release_id = ? ORDER BY id",
        (int(release_id),))]


def _photo(path) -> Path | None:
    if not path:
        return None
    p = Path(path).expanduser()
    if not p.is_file():
        raise db.KardexError(f"no photo at {p}")
    return p


def _copy_photo(src: Path, item_id: int, side: str) -> str:
    out = photos_dir()
    out.mkdir(parents=True, exist_ok=True)
    dest = out / f"{item_id}-{side}{src.suffix.lower() or '.jpg'}"
    shutil.copy2(src, dest)
    return str(dest.resolve())


def add(conn, client: Client, release_id: int, *, front=None, back=None, location=None,
        category=None, notes=None, no_discogs: bool = False, copy: bool = False) -> dict:
    """Add a release to the Discogs collection (unless it is there already, or `no_discogs`)
    and as one `records` item in kardex, with copies of the cover photos."""
    release_id = int(release_id)
    existing = in_kardex(conn, release_id)
    if existing and not copy:
        raise db.KardexError(f"release {release_id} is already in kardex as #{existing[0]}; "
                             "pass --copy to add a second copy")
    front, back = _photo(front), _photo(back)

    release = client.release(release_id)
    attrs = release_attrs(release)
    if not no_discogs:
        # Reuse a collection instance no kardex row claims yet; add one if there is none.
        claimed = {r["attrs"].get("discogs_instance_id") for r in
                   (db.get(conn, i) for i in existing)}
        free = [int(r["instance_id"]) for r in client.collection_instances(release_id)
                if int(r["instance_id"]) not in claimed]
        attrs["discogs_instance_id"] = free[0] if free else client.add_to_collection(release_id)

    item = db.add(conn, "records", release.get("title") or f"Discogs release {release_id}",
                  category=category, location=location, notes=notes, attrs=attrs)
    try:
        photo = _copy_photo(front, item["id"], "front") if front else None
        photo_back = _copy_photo(back, item["id"], "back") if back else None
    except OSError as e:
        db.remove(conn, item["id"])
        raise db.KardexError(f"cannot copy photo: {e}") from None
    if photo or photo_back:
        item = db.edit(conn, item["id"], photo=photo,
                       attrs={"photo_back": photo_back} if photo_back else None)
    return item


# -- sync ---------------------------------------------------------------------------------------

def sync(conn, client: Client) -> dict:
    """Compare the Discogs collection with kardex's records, by release id, copy for copy.
    Read-only."""
    remote: dict[int, list[dict]] = {}
    for r in client.collection():
        remote.setdefault(int(r["id"]), []).append(r)
    local: dict[int, list[dict]] = {}
    unlinked = []
    for item in db.find(conn, collection="records", sort="id"):
        rid = item["attrs"].get("discogs_release_id")
        if rid is None:
            unlinked.append(item)
        else:
            local.setdefault(int(rid), []).append(item)

    discogs_only = []
    for rid, instances in remote.items():
        for r in instances[len(local.get(rid, [])):]:
            b = r.get("basic_information") or {}
            discogs_only.append({
                "release_id": rid, "instance_id": r.get("instance_id"),
                "artist": ", ".join(clean_name(a.get("name", "")) for a in b.get("artists") or []),
                "title": b.get("title"), "year": b.get("year") or None,
                "label": clean_name((b.get("labels") or [{}])[0].get("name", "")) or None,
                "catno": (b.get("labels") or [{}])[0].get("catno"),
            })
    kardex_only = []
    for rid, items in local.items():
        kardex_only += items[len(remote.get(rid, [])):]
    return {"discogs_only": discogs_only, "kardex_only": kardex_only, "unlinked": unlinked}

# Residual review findings - feat/prims-web-interface

From the multi-agent code review of `main..feat/prims-web-interface`
(run `20260920-134046-41b0cbc1`, 9 reviewers, 15 merged findings).

The six P1s and the two crash/hang P2s were fixed in commits `56d3bbe`,
`5120ba2`, `debc4d5` and `ce64f33`. The items below were deliberately not
fixed. They are recorded here so they outlive the session that found them.

## Accepted, not fixed

**JSON list endpoints changed shape (P2, advisory).**
`GET /inventory`, `/categories` and `/locations` returned arrays of positional
tuples before this branch and now return arrays of named objects. No external
consumer is known, and the repo's own `tests/test_crud_operations.sh` does not
parse output, so nothing is actively broken. Not documented in the README, and
no test pins the response shape - so it could change again silently.

**`PUT /inventory/<id>` now 404s on an unknown id (P3, advisory).**
It previously returned 200 regardless. The new behavior is more correct; it is
recorded only because it is an undocumented status-code change.

**`reset_db()`'s guard lives in the CLI, not the function (P3).**
`src/seed.py`'s `main()` performs the destructive-run check. Anything importing
`db.reset_db()` directly bypasses it. Only the seed script and the tests call
it today.

**An upgraded database is stricter than a fresh one.**
The pre-branch schema declared `categories.name TEXT NOT NULL UNIQUE`, whose
implicit `sqlite_autoindex` cannot be dropped by `ALTER TABLE`. An upgraded
database therefore still rejects a sub-category whose name matches an existing
top-level name; a fresh one allows it. No data loss, and the user sees a normal
validation message. Removing it needs a full table rebuild (create/copy/drop/
rename with foreign keys disabled), which would put the preserved rows at risk.

## Pre-existing at the time of this review

The three entries below were accepted partly because the app only answered on
`127.0.0.1`. `feat/phone-capture-logging` put it on the home network, which
withdrew that reason, so each was revisited there.

**`app.run(debug=True)` - fixed for the network.** The network path now serves
through Waitress with debug off (`main()` in `src/app.py`), and every error
lands on a PRIMS page rather than a console. The loopback-only development path
still runs the debugger, which is reachable only from the machine itself;
Werkzeug 3.0.3+ also refuses debugger requests carrying a non-local Host.

**`DELETE /files/<filename>` - fixed.** It now runs `secure_filename` and
refuses any name that changes under it. The margin had belonged entirely to
the router, and the WSGI front-end in front of that router changed. Called with
the router bypassed, the old version returned 200 and deleted a file outside
`uploads/`; `tests/test_auth.py` pins the fix both through the router and past
it.

**`POST /upload` has no CSRF token - narrowed, still open.** It now answers
only a caller on the machine itself, so nothing on the network can reach it.
It is still writable by any web page the owner's own browser loads on this
machine: a multipart POST is CORS-simple, needs no preflight, and carries no
token. The JSON API does not share this - `request.get_json()` rejects the
CORS-simple content types, so a cross-site page cannot write to it.

## Known test gaps

- The file-management endpoints are covered: upload by
  `tests/test_csrf.py::test_file_upload_without_a_token_still_succeeds`
  (which predates this entry, so the entry was already wrong when written),
  and download and delete by `tests/test_auth.py`. `GET /files` is covered
  only as a refusal to a signed-out device.
- `tests/test_crud_operations.sh` asserts nothing - it prints responses. It
  cannot catch a JSON API regression, which is how three routes began
  returning 500 while the suite stayed green.
- No test bounds the number of DB calls a page makes, so the ancestor-lookup
  N+1 could regress unnoticed.
- `tests/conftest.py` sets `db.DB_PATH` directly rather than via the `PRIMS_DB`
  environment variable the README and seed docstring tell users to use, so that
  path is never exercised. There is also no autouse fixture pinning `DB_PATH`
  to a temp file, so a future fixture-less test could reach the real
  `prims.db`.

## Not fixed because the reviewer was wrong

**"The apostrophe regression test asserts nothing."**
The testing reviewer claimed `tests/test_pages.py::test_delete_confirm_survives_
an_apostrophe_in_the_name` verifies nothing, because the `onsubmit` handler is
a static string. Checked directly: reintroducing `{{ item['name'] }}` into the
`confirm()` call makes that exact assertion fail with
`'&#39;' is contained here: return confirm('Delete Kid&#39;s plates?');`.
The test does guard the regression. Kept.

**"No route can create a `parent_id` cycle."**
The performance reviewer concluded the recursive CTEs were safe because
`create_category` sets `parent_id` once and `update_category` only changes the
name. The adversarial reviewer disproved it and it was confirmed directly: a
`POST /categories` naming the id the row is about to receive produced
`{'id': 1, 'parent_id': 1}`, after which `descendant_ids(1)` never returned.
That became finding #11 and is fixed.

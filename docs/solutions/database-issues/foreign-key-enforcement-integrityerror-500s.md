---
title: Enabling PRAGMA foreign_keys turned three working JSON routes into 500s
date: 2026-09-20
category: database-issues
module: src/db.py (data access layer)
problem_type: database_issue
component: database
symptoms:
  - "DELETE /locations/<id> returns HTTP 500 instead of 400 when an inventory item still references that location"
  - "POST /inventory returns HTTP 500 instead of 400 when category_id does not exist"
  - "PUT /categories/<id> returns HTTP 500 instead of 400 when renaming onto a sibling name (UNIQUE violation)"
  - "sqlite3.IntegrityError escapes the Flask route handlers unhandled, since they only caught ValueError"
  - "The curl smoke script prints 200 responses for these paths but asserts nothing, so the regression was invisible"
root_cause: missing_validation
resolution_type: code_fix
severity: high
related_components:
  - flask_routes
  - inventory_api
  - test_crud_operations.sh
tags:
  - sqlite
  - foreign-keys
  - integrityerror
  - exception-translation
  - data-access-layer
  - api-error-handling
---

# Enabling PRAGMA foreign_keys turned three working JSON routes into 500s

## Problem

A feature branch added a web interface to PRIMS and, with it, a shared data-access layer (`src/db.py`) whose `get_conn()` turns on `PRAGMA foreign_keys = ON` (`src/db.py:29-33`). SQLite does not enforce foreign keys unless you ask, so this was the first time the constraints the schema had always *declared* were actually enforced — and three pre-existing JSON API routes that had returned 200 for months began returning HTTP 500, because `sqlite3.IntegrityError` was a new exception their handlers had never had to catch.

## Symptoms

All three failures had the same shape: an unhandled exception escaping a route that catches only `ValueError`, rendered as a 500 (and, because `app.run(debug=True)` ships at the bottom of `src/app.py`, as the Werkzeug interactive debugger rather than a plain error page).

**1. `DELETE /locations/<id>` while an item still references that location.**

```
sqlite3.IntegrityError: FOREIGN KEY constraint failed
```

Before the branch this route ran an unconditional `DELETE FROM locations WHERE id = ?` and returned `{"message": "Location deleted successfully"}` with a 200 (`git show d90fc26:src/app.py`). With enforcement off, the delete succeeded and left the referencing item pointing at a location id that no longer existed. With enforcement on, the same request 500s.

**2. `POST /inventory` with a nonexistent `category_id` (or `location_id`).**

```
sqlite3.IntegrityError: FOREIGN KEY constraint failed
```

`PUT /inventory/<id>` failed identically on a bad id.

**3. `PUT /categories/<id>` renaming a category onto a sibling's name.**

```
sqlite3.IntegrityError: UNIQUE constraint failed: categories.parent_id, categories.name
```

Not a foreign key, but the same unhandled-exception shape, surfaced by the same review — the route's `except ValueError` (`src/app.py:434-437`) does not catch it either.

A fourth entrance to the same failure mode turned up later: an oversized quantity reaching SQLite raises

```
OverflowError: Python int too large to convert to SQLite INTEGER
```

which is not an `IntegrityError` at all, but escapes the same routes the same way.

The common ingredient appears seven times in `src/app.py` -- at lines 380-381, 405-406, 421-422, 436-437, 445-446, 469-470 and 478-479:

```python
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400
```

`ValueError` is the only vocabulary these routes speak. Anything else is a 500.

## What Didn't Work

Two things were treated as evidence the API was fine. Neither could have seen this.

**The pytest suite was green — and structurally could not fail.** At `26a3cf7`, the commit immediately before the fix, the suite contained about ninety test functions (90 `def test_` across `tests/test_db.py`, `test_item_crud.py`, `test_pages.py`, `test_search.py`, `test_seed.py`). Every one of them exercised the data layer or the *new* HTML pages. There was no `tests/test_json_api.py` at all — that file was added by the fix commit `56d3bbe`. A green suite meant "the new code works," not "the old API still works," but it was read as the latter.

**`tests/test_crud_operations.sh` passed — and asserts nothing.** Read it: it is 25 lines of eight `curl` calls against a running dev server (`tests/test_crud_operations.sh:4-25`). It creates a category, creates a location, creates an item with `category_id: 1`, reads, updates, deletes the item, then lists categories and locations. It never deletes a location, never sends an id that does not exist, never renames a category. And crucially it has **no assertions and no exit status** — every response is printed to stdout. It is a shell script whose exit code is the exit code of the last `curl`, which succeeds as long as the server answers *something*, 500 included. Running it "successfully" is compatible with every endpoint returning 500. The project's own residual-findings doc records this plainly: "`tests/test_crud_operations.sh` asserts nothing - it prints responses. It cannot catch a JSON API regression, which is how three routes began returning 500 while the suite stayed green" (`docs/residual-review-findings/feat-prims-web-interface.md`).

**What did work:** a multi-reviewer code review, where four reviewers independently flagged the location-delete case, followed by direct reproduction against a running app. The finding came from reading the diff and asking "what changed for callers," not from any automated check.

**The near-miss worth sitting with.** The hazard had already been recognised *once*. Before the fix, `_resolve_item_form` in `src/app.py` already guarded the HTML form path against a stale category id, with a docstring that names the exact mechanism (`git show 26a3cf7:src/app.py`):

```python
    A category id that no longer exists is caught here rather than surfacing as the
    sqlite3.IntegrityError that PRAGMA foreign_keys would otherwise raise.
    ...
        if db.get_category(parent_id) is None:
            return None, 'That category no longer exists — choose another.'
```

So the author understood, while writing the form, that enabling the pragma made unknown ids into `IntegrityError`s. The guard was written into the one caller being worked on and never generalized to the shared data layer underneath it — which the JSON routes also call. The knowledge existed; it was placed at the wrong level. (That guard survives today at `src/app.py:242-243`, now correctly redundant with the data layer's own check.)

## Solution

Translate integrity failures at the data-layer boundary into `ValueError`, the vocabulary the routes already map to HTTP 400. No route handler changed; the routes were already correct for a data layer that reported failures in one error type.

**`create_item` — before** (`git show 56d3bbe -- src/db.py`):

```python
        cur = conn.execute(
            "INSERT INTO inventory (name, category_id, location_id, quantity, notes) "
            "VALUES (?, ?, ?, ?, ?)",
            (name, category_id, location_id, quantity, notes or ""),
        )
```

**after** (`src/db.py:389-398`):

```python
        try:
            cur = conn.execute(
                "INSERT INTO inventory (name, category_id, location_id, quantity, notes) "
                "VALUES (?, ?, ?, ?, ?)",
                (name, category_id, location_id, quantity, notes or ""),
            )
        except sqlite3.IntegrityError as exc:
            raise ValueError("Unknown category or location id") from exc
        except OverflowError as exc:
            raise ValueError("Quantity is too large to store") from exc
```

`update_item` got the identical treatment around its UPDATE (`src/db.py:467-476`).

**`delete_location` — before** it was an unconditional DELETE. **After** (`src/db.py:571-597`), it gained an in-use guard that mirrors the `category_is_empty` / `delete_category` pair (`src/db.py:365-378`) the module already had:

```python
def location_is_empty(location_id):
    """True when no item points at this location."""
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM inventory WHERE location_id = ?",
            (location_id,),
        ).fetchone()
        return row["n"] == 0
    finally:
        conn.close()


def delete_location(location_id):
    """Delete a location only when no item still references it.

    Mirrors delete_category: the foreign key would refuse the DELETE anyway,
    so the check turns that into a message the caller can act on.
    """
    if not location_is_empty(location_id):
        raise ValueError("Cannot delete a location that items still reference")
```

**`update_category`** wrapped its UPDATE the same way, and picked up the NULL-parent duplicate check that `create_category` already performed and the rename had walked straight past (`src/db.py:339-359`):

```python
        try:
            conn.execute(
                "UPDATE categories SET name = ? WHERE id = ?", (name, category_id)
            )
        except sqlite3.IntegrityError as exc:
            raise ValueError(
                f"A category named '{name}' already exists under this parent"
            ) from exc
```

**`create_category`** additionally looks the parent up *before* inserting (`src/db.py:151-156`), so an unknown `parent_id` reports itself rather than arriving as a foreign-key `IntegrityError` that the surrounding `except` would have misreported as a duplicate name:

```python
        else:
            parent = conn.execute(
                "SELECT 1 FROM categories WHERE id = ?", (parent_id,)
            ).fetchone()
            if parent is None:
                raise ValueError(f"No category with id {parent_id}")
```

**The follow-up (`ce64f33`).** An unbounded quantity crashed through the same routes with `OverflowError` instead of `IntegrityError`. The HTML form was fixed first — `MAX_QUANTITY = 2 ** 63 - 1` with a rejection message and a `max` on the input (`src/app.py:204-208`, `src/app.py:233-234`) — and the JSON path was initially missed. The commit message names the lesson: "the form path was fixed first, but the crash had two entrances." The durable fix was the same boundary translation, added as a second `except` clause in `create_item` and `update_item` (`src/db.py:397-398`, `src/db.py:475-476`).

`raise ... from exc` throughout preserves the original exception in the traceback, so the 400 message stays user-facing while the underlying SQLite error survives for debugging.

## Why This Works

The root cause is not any one route. It is that **enabling a constraint retroactively changes the set of exceptions every existing write path can raise** — silently, with no compile error, no test failure and no diff on any of the affected routes. `PRAGMA foreign_keys = ON` in `get_conn()` (`src/db.py:32`) is one line in a new file, and its blast radius is every caller of every write function in the module, including callers written months earlier by someone who had never heard of the pragma. The schema had declared `FOREIGN KEY (category_id) REFERENCES categories(id)` since the very first commit; the declarations were decorative until that one line made them load-bearing.

The fix works because it puts the translation where the knowledge lives. `src/db.py` is the module that owns the connection, sets the pragma, and knows which constraints exist; it is therefore the only module positioned to explain what a violation *means* ("Unknown category or location id", not "FOREIGN KEY constraint failed"). Callers above it already had a working error contract — `ValueError` means "the caller sent something correctable" and becomes a 400 — and that contract was fine. What was missing was anything holding the boundary to it. Every `except ValueError` in `src/app.py` is now backed by a data layer that guarantees `ValueError` is the only correctable-failure type it raises.

The `delete_location` guard is worth distinguishing from the `except`-wraps. It does not translate an error; it prevents one, by asking the question the database was going to ask anyway. That is the same move `delete_category` already made (`src/db.py:369-372`), and choosing it kept the two delete functions symmetric — a reader of one can predict the other. Where the check is cheap and the answer is useful to the caller, prefer asking first; where the constraint is only knowable at write time (unique names, integer range), wrap and translate.

## Prevention

**1. When you enable a constraint on an existing schema, enumerate every write path before you commit.** Turning on enforcement is a behavior change for code you are not editing, which is exactly the change a diff review is worst at catching. A mechanical sweep works: `grep -n "INSERT\|UPDATE\|DELETE" src/db.py`, then for each one ask which constraint it can now violate and what the caller sees when it does. In this repo that sweep is eight statements and would have found all three regressions plus the `create_category` misreport before any reviewer did.

**2. A smoke script that prints without asserting provides zero regression protection.** `tests/test_crud_operations.sh` ran clean through the entire regression. It has no assertions, checks no status codes, and exits 0 as long as the server answers at all — a 500 body prints just as happily as a 201. Worse than useless: its green output was cited as evidence. Either give such a script assertions (`curl -f`, or `-o /dev/null -w '%{http_code}'` compared against an expected code, with `set -e`) or stop treating a clean run as a signal. Read what a check actually asserts before you let it reassure you.

**3. Test the failure modes at the HTTP boundary, not only in the data layer.** The regression was a *status code* change; only a test that asserts on status codes could see it. `tests/test_json_api.py`, added by the fix, is exactly that file, and its docstring states the contract it defends (`tests/test_json_api.py:1-7`):

```python
"""JSON API validation tests.

The HTML forms guard their own inputs, but the JSON endpoints hand values
straight to the data layer. With `PRAGMA foreign_keys = ON`, a bad id or a
still-referenced row raises sqlite3.IntegrityError, which escapes as a 500
unless the data layer turns it into a ValueError the route maps to 400.
"""
```

Each regression gets a test that names the input and pins the code (`tests/test_json_api.py:17-20, 42-49, 67-74`):

```python
def test_create_item_with_unknown_category_is_a_400(client):
    response = client.post('/inventory', json={'name': 'Ghost', 'category_id': 999})
    assert response.status_code == 400
    assert 'error' in response.get_json()


def test_rename_category_onto_a_sibling_is_a_400(client):
    db = _db()
    kitchen = db.create_category('Kitchen')
    db.create_category('Glassware', parent_id=kitchen)
    mugs = db.create_category('Mugs', parent_id=kitchen)
    response = client.put(f'/categories/{mugs}', json={'name': 'Glassware'})
    assert response.status_code == 400
    assert 'error' in response.get_json()


def test_delete_location_still_in_use_is_a_400(client):
    db = _db()
    kitchen = db.create_category('Kitchen')
    cupboard = db.get_or_create_location('Cupboard')
    db.create_item('Pint glass', category_id=kitchen, location_id=cupboard)
    response = client.delete(f'/locations/{cupboard}')
    assert response.status_code == 400
    assert 'error' in response.get_json()
```

**4. Pin the success case next to the failure case.** A guard that rejects too much is its own regression, and `test_delete_unused_location_still_succeeds` (`tests/test_json_api.py:77-82`) is what stops the in-use guard from quietly becoming "no location can ever be deleted":

```python
def test_delete_unused_location_still_succeeds(client):
    db = _db()
    cupboard = db.get_or_create_location('Cupboard')
    response = client.delete(f'/locations/{cupboard}')
    assert response.status_code == 200
    assert db.get_location(cupboard) is None
```

**5. Assert that a rejected write left nothing behind.** The `OverflowError` tests check not just the status code but the resulting state (`tests/test_json_api.py:85-106`), so a "400" that half-wrote the row still fails:

```python
def test_create_item_with_an_unstorable_quantity_is_a_400(client):
    # The web form bounds quantity before writing, but the JSON API passes it
    # straight through: SQLite raises OverflowError past 2**63 - 1, which
    # escaped as a 500 until create_item translated it.
    ...
    assert db.get_all_items() == []


def test_update_item_with_an_unstorable_quantity_is_a_400(client):
    ...
    assert db.get_item(item)['quantity'] == 6
```

**6. When you fix a crash in one caller, ask where else that input can enter.** The quantity overflow was fixed in the form and missed in the JSON API, in a session that had *just finished* fixing three bugs with precisely this shape. And the original bug was itself a generalization failure: `_resolve_item_form` guarded against a stale category id, with a docstring naming `PRAGMA foreign_keys` as the reason, while the shared data layer one level down stayed unguarded for every other caller. The rule that falls out: **a guard written in a caller is a bug report about the layer below it.** If you find yourself defending against an error the data layer could raise, put the defense in the data layer.

**7. Reviewers reading for contract changes beat any suite you have.** Four reviewers independently flagged the location-delete case from the diff alone. Nothing automated could have. When a change touches a shared layer, the question to ask a reviewer is not "is this code correct" but "what does this change for code that isn't in the diff."

## Related

- [Web interface plan](../../plans/2026-08-03-001-feat-prims-web-interface-plan.md) -- KTD5 chose one shared data-access module for both the JSON API and the new HTML pages, which is the surface that later hosted `PRAGMA foreign_keys = ON`. KTD6 kept the curl smoke script alongside the new pytest suite. Neither the plan's requirements nor U2's test scenarios anticipated that enabling enforcement would change error behavior on pre-existing routes.
- [Residual review findings](../../residual-review-findings/feat-prims-web-interface.md) -- from the same review run. Its "Known test gaps" section already records that the smoke script cannot catch a JSON API regression; this document supplies the fix that gap let through. The gap itself is still open: the script remains assertion-free today.

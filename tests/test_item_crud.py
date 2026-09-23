"""Item add / edit / delete tests (U5).

Covers R10 (add an item, choosing an existing category or creating one
inline, with name/quantity/notes/location), R11/AE3 (edit an item including
its quantity), R12 (delete an item), R3/AE4 (attach an item directly to a
top-level collection) and flow F3 (add after shopping).

The form routes are CSRF-protected, so every POST here goes through
``post_form``, which fetches a form and submits the hidden token with it --
what a browser does. Rejection of a *missing* token is covered in
tests/test_csrf.py.
"""
import pytest

from conftest import post_form


def _db():
    """The data layer, already pointed at the throwaway DB by the client fixture."""
    import db

    return db


def _html(response):
    return response.get_data(as_text=True)


def _kitchen_tree():
    """Kitchen > Glassware, the fixture most add/edit tests hang off."""
    db = _db()
    kitchen = db.create_category("Kitchen")
    glassware = db.create_category("Glassware", parent_id=kitchen)
    return kitchen, glassware


def _pint_glasses_with_quantity_six():
    """The literal AE3 fixture: a pint-glass item with quantity 6."""
    db = _db()
    _, glassware = _kitchen_tree()
    item = db.create_item("Pint glass", category_id=glassware, quantity=6)
    return item


# --- Reaching the add form (R10 / F3) --------------------------------------

def test_home_offers_an_add_item_entry_point(client):
    # Covers R10: the add form is only real if the owner can reach it.
    body = _html(client.get("/"))
    assert "/item/new" in body


def test_add_form_lists_every_category_including_top_level(client):
    # Covers R10 with KD2/R3: items attach at any level, so the picker must
    # offer top-level collections as well as nested categories.
    kitchen, glassware = _kitchen_tree()

    resp = client.get("/item/new")
    body = _html(resp)
    assert resp.status_code == 200
    assert f'value="{kitchen}"' in body
    assert f'value="{glassware}"' in body
    assert "Glassware" in body


def test_category_pickers_empty_option_says_no_category(client):
    # Leaving the picker alone stores no category at all; it does not put the
    # item "at top level", which would mean attaching it to a top-level
    # collection. The label has to say the thing the code does.
    body = _html(client.get("/item/new"))
    assert "top level" not in body.lower()
    assert "No category" in body


def test_add_form_prefills_the_category_it_was_opened_from(client):
    kitchen, glassware = _kitchen_tree()

    body = _html(client.get(f"/item/new?category_id={glassware}"))
    assert f'value="{glassware}" selected' in body


def test_category_page_offers_add_item_prefilled(client):
    _, glassware = _kitchen_tree()

    body = _html(client.get(f"/category/{glassware}"))
    assert f"/item/new?category_id={glassware}" in body


# --- Adding (R10) ----------------------------------------------------------

def test_add_item_with_an_existing_category_persists_and_appears_under_it(client):
    # Covers R10.
    db = _db()
    _, glassware = _kitchen_tree()

    resp = post_form(
        client,
        "/item/new",
        data={
            "name": "Wine glass",
            "category_id": str(glassware),
            "quantity": "4",
            "notes": "Stemless",
            "location": "Top shelf",
        },
        follow_redirects=True,
    )
    assert resp.status_code == 200

    items = db.get_items_in_category(glassware)
    assert [row["name"] for row in items] == ["Wine glass"]
    saved = items[0]
    assert saved["quantity"] == 4
    assert saved["notes"] == "Stemless"
    assert db.get_location(saved["location_id"])["name"] == "Top shelf"
    assert "Wine glass" in _html(client.get(f"/category/{glassware}"))


def test_add_item_creating_a_category_inline_creates_both(client):
    # Covers R10: the inline-create path is the only category authoring in v1.
    db = _db()
    kitchen, _ = _kitchen_tree()

    post_form(
        client,
        "/item/new",
        data={
            "name": "Stock pot",
            "category_id": str(kitchen),
            "new_category": "Cookware",
            "quantity": "1",
        },
        follow_redirects=True,
    )

    cookware = [
        row for row in db.get_subcategories(kitchen) if row["name"] == "Cookware"
    ]
    assert len(cookware) == 1
    items = db.get_items_in_category(cookware[0]["id"])
    assert [row["name"] for row in items] == ["Stock pot"]


def test_add_item_creating_a_top_level_category_inline(client):
    # No parent chosen plus an inline name means a new top-level collection.
    db = _db()

    post_form(
        client,
        "/item/new",
        data={"name": "Tent", "category_id": "", "new_category": "Camping"},
        follow_redirects=True,
    )

    camping = [row for row in db.get_top_level_categories() if row["name"] == "Camping"]
    assert len(camping) == 1
    assert [row["name"] for row in db.get_items_in_category(camping[0]["id"])] == ["Tent"]


def test_add_book_attached_directly_to_library_saves_at_top_level(client):
    # Covers R3 / AE4: no sub-type chosen, so the book lives on Library itself.
    db = _db()
    library = db.create_category("Library")
    db.create_category("Records", parent_id=library)

    post_form(
        client,
        "/item/new",
        data={"name": "Dune", "category_id": str(library), "quantity": "1"},
        follow_redirects=True,
    )

    assert [row["name"] for row in db.get_items_in_category(library)] == ["Dune"]


def test_add_item_defaults_quantity_and_notes_when_left_blank(client):
    db = _db()
    kitchen, _ = _kitchen_tree()

    post_form(
        client,
        "/item/new",
        data={"name": "Whisk", "category_id": str(kitchen), "quantity": "", "notes": ""},
        follow_redirects=True,
    )

    saved = db.get_items_in_category(kitchen)[0]
    assert saved["quantity"] == 0
    assert saved["notes"] == ""
    assert saved["location_id"] is None


# --- Add validation --------------------------------------------------------

def test_add_without_a_name_returns_a_validation_error_not_a_crash(client):
    db = _db()
    kitchen, _ = _kitchen_tree()

    resp = post_form(
        client,
        "/item/new", data={"name": "  ", "category_id": str(kitchen), "quantity": "2"}
    )
    body = _html(resp)
    assert resp.status_code == 400
    assert "name" in body.lower()
    assert db.get_all_items() == []
    # The form comes back so the owner can fix it rather than retype everything.
    assert "<form" in body


def test_add_with_a_non_numeric_quantity_returns_a_validation_error(client):
    db = _db()
    kitchen, _ = _kitchen_tree()

    resp = post_form(
        client,
        "/item/new",
        data={"name": "Mug", "category_id": str(kitchen), "quantity": "lots"},
    )
    assert resp.status_code == 400
    assert "quantity" in _html(resp).lower()
    assert db.get_all_items() == []


def test_add_with_a_negative_quantity_returns_a_validation_error(client):
    db = _db()
    kitchen, _ = _kitchen_tree()

    resp = post_form(
        client,
        "/item/new",
        data={"name": "Mug", "category_id": str(kitchen), "quantity": "-3"},
    )
    assert resp.status_code == 400
    assert db.get_all_items() == []


def test_add_with_a_quantity_too_large_to_store_is_rejected_not_a_500(client):
    # SQLite's INTEGER stops at 2**63 - 1; a bigger number raised OverflowError
    # inside the INSERT, which surfaced as a 500 rather than a form error.
    db = _db()
    kitchen, _ = _kitchen_tree()

    resp = post_form(
        client,
        "/item/new",
        data={"name": "Mug", "category_id": str(kitchen), "quantity": "9" * 20},
    )
    assert resp.status_code == 400
    assert "quantity" in _html(resp).lower()
    assert db.get_all_items() == []


def test_add_with_the_largest_storable_quantity_still_saves(client):
    # The bound rejects what SQLite cannot store, and nothing short of it.
    db = _db()
    kitchen, _ = _kitchen_tree()

    post_form(
        client,
        "/item/new",
        data={"name": "Rice", "category_id": str(kitchen), "quantity": str(2**63 - 1)},
        follow_redirects=True,
    )
    assert db.get_items_in_category(kitchen)[0]["quantity"] == 2**63 - 1


def test_an_unstorable_quantity_is_refused_visibly_on_the_form(client):
    """Replaces a check that the input declared max="2**63-1".

    That attribute only exists on type="number", and quantity is now a select
    beside a text field (KTD10), so the browser can no longer refuse the value
    before submit. The intent -- the form says so rather than failing on save --
    now holds through the server check, rendered where the owner will see it.
    """
    db = _db()
    response = post_form(
        client,
        "/item/new",
        data={"name": "Rice", "quantity": "more", "quantity_other": str(2**63)},
    )
    body = _html(response)

    assert response.status_code == 400
    assert "cannot be larger than" in body
    assert 'class="field-error"' in body
    assert db.get_all_items() == []


def test_a_failed_item_write_leaves_no_inline_category_behind(client, monkeypatch):
    # Validation failures never created the category, but an error thrown
    # *inside* the item write stranded the category the same request had just
    # made: the item was lost and the half-made category survived.
    import app as app_module

    db = _db()
    kitchen, _ = _kitchen_tree()

    def boom(**kwargs):
        raise RuntimeError("the write failed")

    monkeypatch.setattr(app_module.db, "create_item", boom)

    with pytest.raises(RuntimeError):
        post_form(
            client,
            "/item/new",
            data={
                "name": "Stock pot",
                "category_id": str(kitchen),
                "new_category": "Cookware",
            },
        )

    assert [row["name"] for row in db.get_subcategories(kitchen)] == ["Glassware"]


def test_add_with_an_unknown_category_is_rejected_not_a_500(client):
    # PRAGMA foreign_keys = ON would otherwise raise IntegrityError -> 500.
    db = _db()

    resp = post_form(
        client,
        "/item/new", data={"name": "Mug", "category_id": "999", "quantity": "1"}
    )
    assert resp.status_code == 400
    assert db.get_all_items() == []


def test_add_with_an_existing_inline_category_name_reuses_that_category(client):
    # Typing a sub-category that already exists under the chosen parent is the
    # owner saying "put it in that one". The field is get-or-create, like the
    # Location field beside it: no duplicate, no second category, and above all
    # no rejected save that loses everything typed.
    db = _db()
    kitchen, glassware = _kitchen_tree()

    resp = post_form(
        client,
        "/item/new",
        data={
            "name": "Tumbler",
            "category_id": str(kitchen),
            "new_category": "Glassware",
        },
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert len(db.get_subcategories(kitchen)) == 1
    assert [row["name"] for row in db.get_items_in_category(glassware)] == ["Tumbler"]


def test_reusing_an_existing_inline_category_does_not_delete_it_on_a_failed_write(
    client, monkeypatch
):
    # The undo that removes a *newly created* inline category must not touch a
    # pre-existing one the submission merely reused.
    import app as app_module

    db = _db()
    kitchen, glassware = _kitchen_tree()

    def boom(**kwargs):
        raise RuntimeError("the write failed")

    monkeypatch.setattr(app_module.db, "create_item", boom)

    with pytest.raises(RuntimeError):
        post_form(
            client,
            "/item/new",
            data={
                "name": "Tumbler",
                "category_id": str(kitchen),
                "new_category": "Glassware",
            },
        )

    assert db.get_category(glassware) is not None


# --- Editing (R11 / AE3) ---------------------------------------------------

def test_edit_form_prefills_every_field(client):
    db = _db()
    _, glassware = _kitchen_tree()
    shelf = db.get_or_create_location("Top shelf")
    item = db.create_item(
        "Pint glass",
        category_id=glassware,
        location_id=shelf,
        quantity=6,
        notes="Two are chipped",
    )

    resp = client.get(f"/item/{item}/edit")
    body = _html(resp)
    assert resp.status_code == 200
    assert 'value="Pint glass"' in body
    # The select renders an <option value="6"> for every item, so only the
    # selected marker shows this item's quantity was prefilled.
    assert '<option value="6" selected>' in body
    assert 'value="Top shelf"' in body
    assert "Two are chipped" in body
    assert f'value="{glassware}" selected' in body


def test_editing_the_quantity_from_six_to_five_persists_five(client):
    # Covers R11 / AE3, built on the literal pint-glass fixture.
    db = _db()
    item = _pint_glasses_with_quantity_six()
    before = db.get_item(item)

    resp = post_form(
        client,
        f"/item/{item}/edit",
        data={
            "name": before["name"],
            "category_id": str(before["category_id"]),
            "quantity": "5",
            "notes": before["notes"],
            "location": "",
        },
        follow_redirects=True,
    )

    assert resp.status_code == 200
    assert db.get_item(item)["quantity"] == 5
    assert "5" in _html(client.get(f"/item/{item}"))


def test_editing_can_change_name_notes_location_and_category(client):
    # Covers R11 beyond quantity.
    db = _db()
    kitchen, glassware = _kitchen_tree()
    item = db.create_item("Pint glass", category_id=glassware, quantity=6)

    post_form(
        client,
        f"/item/{item}/edit",
        data={
            "name": "Pint glasses",
            "category_id": str(kitchen),
            "quantity": "6",
            "notes": "Moved out of Glassware",
            "location": "Sideboard",
        },
        follow_redirects=True,
    )

    row = db.get_item(item)
    assert row["name"] == "Pint glasses"
    assert row["category_id"] == kitchen
    assert row["notes"] == "Moved out of Glassware"
    assert db.get_location(row["location_id"])["name"] == "Sideboard"


def test_editing_to_a_blank_name_returns_a_validation_error(client):
    db = _db()
    item = _pint_glasses_with_quantity_six()

    resp = post_form(
        client,
        f"/item/{item}/edit",
        data={"name": "", "category_id": "", "quantity": "5"},
    )
    assert resp.status_code == 400
    assert db.get_item(item)["name"] == "Pint glass"
    assert db.get_item(item)["quantity"] == 6


def test_editing_with_a_bad_quantity_leaves_the_item_untouched(client):
    db = _db()
    item = _pint_glasses_with_quantity_six()
    before = db.get_item(item)

    resp = post_form(
        client,
        f"/item/{item}/edit",
        data={
            "name": before["name"],
            "category_id": str(before["category_id"]),
            "quantity": "five",
        },
    )
    assert resp.status_code == 400
    assert db.get_item(item)["quantity"] == 6


def test_edit_form_for_an_unknown_item_returns_404(client):
    assert client.get("/item/999/edit").status_code == 404
    assert post_form(client, "/item/999/edit", data={"name": "x"}).status_code == 404


# --- Deleting (R12) --------------------------------------------------------

def test_deleting_removes_the_item_from_browse_and_search(client):
    # Covers R12.
    db = _db()
    _, glassware = _kitchen_tree()
    item = db.create_item("Pint glass", category_id=glassware, quantity=6, notes="chipped")

    resp = post_form(client, f"/item/{item}/delete", follow_redirects=True)
    assert resp.status_code == 200

    assert db.get_item(item) is None
    assert client.get(f"/item/{item}").status_code == 404
    assert "Pint glass" not in _html(client.get(f"/category/{glassware}"))
    assert "Pint glass" not in _html(client.get("/search?q=Pint"))
    assert "Pint glass" not in _html(client.get("/search?q=chipped"))


def test_deleting_returns_to_the_items_category(client):
    db = _db()
    _, glassware = _kitchen_tree()
    item = db.create_item("Pint glass", category_id=glassware)

    resp = post_form(client, f"/item/{item}/delete")
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith(f"/category/{glassware}")


def test_deleting_an_unknown_item_returns_404(client):
    assert post_form(client, "/item/999/delete").status_code == 404


def test_get_on_the_delete_route_is_not_allowed(client):
    # Deletion is a POST (KTD1: a plain form post, guarded by confirm()).
    db = _db()
    _, glassware = _kitchen_tree()
    item = db.create_item("Pint glass", category_id=glassware)

    assert client.get(f"/item/{item}/delete").status_code == 405
    assert db.get_item(item) is not None


# --- Flow F3 end to end ----------------------------------------------------

def test_add_then_edit_quantity_then_delete_round_trip(client):
    # Covers F3 plus R11/R12: the whole loop through the web pages.
    db = _db()
    kitchen, _ = _kitchen_tree()

    assert "/item/new" in _html(client.get("/"))
    post_form(
        client,
        "/item/new",
        data={
            "name": "Pint glass",
            "category_id": str(kitchen),
            "new_category": "Barware",
            "quantity": "6",
            "location": "Top shelf",
        },
        follow_redirects=True,
    )
    barware = [r for r in db.get_subcategories(kitchen) if r["name"] == "Barware"][0]["id"]
    item = db.get_items_in_category(barware)[0]["id"]

    assert f"/item/{item}/edit" in _html(client.get(f"/item/{item}"))
    post_form(
        client,
        f"/item/{item}/edit",
        data={
            "name": "Pint glass",
            "category_id": str(barware),
            "quantity": "5",
            "location": "Top shelf",
        },
        follow_redirects=True,
    )
    assert db.get_item(item)["quantity"] == 5

    post_form(client, f"/item/{item}/delete", follow_redirects=True)
    assert db.get_item(item) is None


def test_item_form_is_mobile_ready(client):
    # Covers R9.
    body = _html(client.get("/item/new"))
    assert 'name="viewport"' in body
    assert "item-form" in body


def test_json_api_is_untouched_by_the_form_routes(client):
    # Guards KTD5.
    resp = client.post("/inventory", json={"name": "Mug", "category_id": None})
    assert resp.status_code == 201
    assert client.get("/inventory").get_json()[0]["name"] == "Mug"


# --- The capture screen (U7) ------------------------------------------------

def _field_order(body):
    """The order the owner meets the item form's fields, top to bottom.

    Scoped to the item form: the page header carries its own search box and
    sign-out form, which are not part of a capture.
    """
    import re

    form = re.search(r'<form class="item-form".*?</form>', body, re.S).group(0)
    return re.findall(r'<(?:input|select|textarea)[^>]*\bname="(\w+)"', form)


def test_quantity_is_set_from_a_list_without_the_keyboard(client):
    """Covers R8. The iOS number pad has no microphone key, so the keyboard
    cannot be the only way in; a select is one tap and needs no JavaScript."""
    body = _html(client.get("/item/new"))

    assert '<select id="quantity" name="quantity"' in body
    assert '<option value="24"' in body
    assert 'type="number"' not in body


def test_a_quantity_chosen_from_the_list_is_saved(client):
    db = _db()
    post_form(client, "/item/new", data={"name": "Pint glass", "quantity": "6"})

    assert db.get_all_items()[0]["quantity"] == 6


def test_a_larger_quantity_comes_from_the_text_field_and_wins(client):
    """The list covers the ordinary case; the field beside it the rest."""
    db = _db()
    post_form(
        client,
        "/item/new",
        data={"name": "Screws", "quantity": "more", "quantity_other": "250"},
    )

    assert db.get_all_items()[0]["quantity"] == 250


def test_a_blank_quantity_still_saves_as_zero(client):
    """Covers R11: parity with the form as it was."""
    db = _db()
    post_form(client, "/item/new", data={"name": "Twine", "quantity": ""})

    assert db.get_all_items()[0]["quantity"] == 0


def test_editing_a_large_quantity_puts_it_in_the_text_field(client):
    """Otherwise the select silently resets it to 0 and the next save loses it."""
    db = _db()
    item = db.create_item("Screws", category_id=None, quantity=250)
    body = _html(client.get(f"/item/{item}/edit"))

    assert 'name="quantity_other"' in body
    assert 'value="250"' in body


def _quantity_other_value(body):
    """What the rendered "larger number" field is pre-filled with."""
    import re

    return re.search(
        r'<input[^>]*\bname="quantity_other"[^>]*\bvalue="([^"]*)"', body, re.S
    ).group(1)


def test_picking_a_smaller_quantity_beats_a_stale_larger_one(client):
    """Editing a large count pre-fills the text field. When the owner then
    picks a smaller number from the list, the pick is what gets saved -- not
    the leftover number, silently, under a "Saved" message."""
    db = _db()
    item = db.create_item("Screws", category_id=None, quantity=250)

    resp = post_form(
        client,
        f"/item/{item}/edit",
        data={"name": "Screws", "quantity": "5", "quantity_other": "250"},
    )

    assert resp.status_code == 302
    assert db.get_item(item)["quantity"] == 5


def test_choosing_more_saves_the_number_in_the_text_field(client):
    db = _db()
    item = db.create_item("Screws", category_id=None, quantity=7)

    post_form(
        client,
        f"/item/{item}/edit",
        data={"name": "Screws", "quantity": "more", "quantity_other": "250"},
    )

    assert db.get_item(item)["quantity"] == 250


def test_choosing_more_with_no_number_is_refused_visibly(client):
    """Not a silent 0: the owner picked "more…" meaning to type a count."""
    db = _db()
    response = post_form(
        client,
        "/item/new",
        data={"name": "Screws", "quantity": "more", "quantity_other": ""},
    )
    body = _html(response)

    assert response.status_code == 400
    assert 'class="field-error"' in body
    assert "Enter the larger quantity, or pick one from the list." in body
    assert db.get_all_items() == []
    # Shown back as it was chosen: "more…" still picked, the field still empty.
    assert '<option value="more" selected>' in body
    assert _quantity_other_value(body) == ""


def test_the_edit_form_shows_a_large_quantity_under_more(client):
    db = _db()
    large = db.create_item("Screws", category_id=None, quantity=250)
    small = db.create_item("Hooks", category_id=None, quantity=7)

    body = _html(client.get(f"/item/{large}/edit"))
    assert '<option value="more" selected>' in body
    assert _quantity_other_value(body) == "250"

    body = _html(client.get(f"/item/{small}/edit"))
    assert '<option value="7" selected>' in body
    assert '<option value="more" selected>' not in body
    assert _quantity_other_value(body) == ""


def test_a_post_with_only_the_old_quantity_field_still_saves_it(client):
    """Covers R11: a client that sends one `quantity`, as the old single field
    did, is not required to know about "more"."""
    db = _db()
    post_form(client, "/item/new", data={"name": "Screws", "quantity": "30"})

    assert db.get_all_items()[0]["quantity"] == 30


def test_the_name_field_does_not_autofocus(client):
    """On a phone autofocus raises the keyboard before the owner has chosen
    what to do, and the owner's own tap is what puts the mic key in reach."""
    assert "autofocus" not in _html(client.get("/item/new"))


def test_fields_run_in_capture_order(client):
    """Name, quantity and notes first, so a typical capture needs no scrolling.
    The new sub-category sits straight after the category its hint refers to."""
    order = _field_order(_html(client.get("/item/new")))

    assert order == [
        "csrf_token",
        "name",
        "quantity",
        "quantity_other",
        "notes",
        "category_id",
        "new_category",
        "location",
    ]


def test_the_name_field_is_shaped_for_dictation(client):
    body = _html(client.get("/item/new"))

    assert 'autocapitalize="words"' in body
    assert 'enterkeyhint="next"' in body


def test_a_rejected_capture_is_shown_as_an_error_not_a_success(client):
    """The flash bar is green for everything. A rejection shown there reads as
    'saved', and the owner walks away believing the item was written."""
    response = post_form(
        client,
        "/item/new",
        data={"name": "Rice", "quantity": "more", "quantity_other": "lots"},
    )
    body = _html(response)

    assert response.status_code == 400
    assert 'class="field-error"' in body
    assert "whole number" in body
    assert 'class="flashes"' not in body


def test_one_capture_writes_exactly_one_item_and_nothing_before_it(client):
    """Covers AE1, AE2 and R7. Rendering the form writes nothing; submitting
    it once writes one row, even when the dictation names two things."""
    db = _db()
    client.get("/item/new")
    assert db.get_all_items() == []

    post_form(
        client,
        "/item/new",
        data={"name": "three pint glasses and a mason jar", "quantity": "3"},
    )

    items = db.get_all_items()
    assert len(items) == 1
    assert items[0]["name"] == "three pint glasses and a mason jar"


def test_a_capture_with_no_category_lands_in_the_uncategorised_view(client):
    """Covers AE6 and R10."""
    db = _db()
    post_form(client, "/item/new", data={"name": "Mystery cable", "quantity": "1"})

    assert "Mystery cable" in _html(client.get("/uncategorised"))
    item = db.get_all_items()[0]
    assert item["category_id"] is None

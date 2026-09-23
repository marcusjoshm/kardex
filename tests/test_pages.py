"""Browse-page tests (U3).

Covers R6 (home tiles + reachable search), R7/AE1 (a category shows its
sub-categories and its directly-attached items together), R9 (mobile-ready
markup), and flow F1 (browse home -> category -> item to read a quantity).
"""


def _db():
    """The data layer, already pointed at the throwaway DB by the client fixture."""
    import db

    return db


def _html(response):
    return response.get_data(as_text=True)


# --- Home (R6) -------------------------------------------------------------

def test_home_lists_top_level_collections_as_tiles(client):
    # Covers R6.
    db = _db()
    kitchen = db.create_category("Kitchen")
    db.create_category("Library")
    glass = db.create_category("Glassware", parent_id=kitchen)
    db.create_item("Pint glass", category_id=glass, quantity=6)

    resp = client.get("/")
    body = _html(resp)
    assert resp.status_code == 200
    assert "Kitchen" in body
    assert "Library" in body
    # Tiles link into the collection, and carry a subtree-wide item count.
    assert f'href="/category/{kitchen}"' in body
    assert 'class="tiles"' in body
    assert "1 item" in body


def test_home_exposes_search(client):
    # Covers R6: search is reachable from the home screen.
    body = _html(client.get("/"))
    assert 'action="/search"' in body


def test_home_with_no_collections_shows_empty_state(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "empty-state" in _html(resp)


def test_home_links_to_uncategorised_items_when_some_exist(client):
    # An item with no category appears on no category page, so the home page
    # is where it has to be reachable from.
    db = _db()
    db.create_item("Spare key", category_id=None)

    assert "/uncategorised" in _html(client.get("/"))


def test_home_hides_the_uncategorised_link_when_every_item_has_a_category(client):
    db = _db()
    kitchen = db.create_category("Kitchen")
    db.create_item("Whisk", category_id=kitchen)

    assert "/uncategorised" not in _html(client.get("/"))


# --- Uncategorised items ---------------------------------------------------

def test_uncategorised_page_lists_only_items_with_no_category(client):
    db = _db()
    kitchen = db.create_category("Kitchen")
    db.create_item("Whisk", category_id=kitchen)
    loose = db.create_item("Spare key", category_id=None, quantity=2)

    resp = client.get("/uncategorised")
    body = _html(resp)
    assert resp.status_code == 200
    assert "Spare key" in body
    assert f'href="/item/{loose}"' in body
    assert 'class="entry-list"' in body
    assert "Whisk" not in body


def test_uncategorised_page_with_nothing_to_show_renders_empty_state(client):
    resp = client.get("/uncategorised")
    assert resp.status_code == 200
    assert "empty-state" in _html(resp)


# --- Category (R7 / AE1) ---------------------------------------------------

def test_category_shows_subcategories_and_loose_items_together(client):
    # Covers R7 / AE1: Kitchen holds Glassware + Appliances and a loose item.
    db = _db()
    kitchen = db.create_category("Kitchen")
    db.create_category("Glassware", parent_id=kitchen)
    db.create_category("Appliances", parent_id=kitchen)
    sponges = db.create_item("spare sponges", category_id=kitchen, quantity=4)

    resp = client.get(f"/category/{kitchen}")
    body = _html(resp)
    assert resp.status_code == 200
    assert "Glassware" in body
    assert "Appliances" in body
    assert "spare sponges" in body
    assert f'href="/item/{sponges}"' in body


def test_category_with_only_subcategories_has_no_items_section(client):
    db = _db()
    kitchen = db.create_category("Kitchen")
    db.create_category("Glassware", parent_id=kitchen)

    body = _html(client.get(f"/category/{kitchen}"))
    assert "Glassware" in body
    assert 'id="subcategories"' in body
    assert 'id="items"' not in body


def test_category_with_only_items_has_no_subcategory_section(client):
    db = _db()
    kitchen = db.create_category("Kitchen")
    db.create_item("spare sponges", category_id=kitchen, quantity=4)

    body = _html(client.get(f"/category/{kitchen}"))
    assert "spare sponges" in body
    assert 'id="items"' in body
    assert 'id="subcategories"' not in body


def test_empty_category_renders_empty_state(client):
    db = _db()
    kitchen = db.create_category("Kitchen")

    resp = client.get(f"/category/{kitchen}")
    assert resp.status_code == 200
    assert "empty-state" in _html(resp)


def test_unknown_category_returns_404(client):
    assert client.get("/category/999").status_code == 404


# --- Item detail -----------------------------------------------------------

def test_item_detail_shows_name_quantity_notes_and_location(client):
    db = _db()
    kitchen = db.create_category("Kitchen")
    glass = db.create_category("Glassware", parent_id=kitchen)
    shelf = db.get_or_create_location("Top shelf")
    item = db.create_item(
        "Pint glass",
        category_id=glass,
        location_id=shelf,
        quantity=6,
        notes="Two are chipped",
    )

    resp = client.get(f"/item/{item}")
    body = _html(resp)
    assert resp.status_code == 200
    assert "Pint glass" in body
    assert "6" in body
    assert "Two are chipped" in body
    assert "Top shelf" in body


def test_item_detail_without_location_or_notes_still_renders(client):
    db = _db()
    kitchen = db.create_category("Kitchen")
    item = db.create_item("spare sponges", category_id=kitchen, quantity=4)

    resp = client.get(f"/item/{item}")
    body = _html(resp)
    assert resp.status_code == 200
    assert "spare sponges" in body
    assert "Kitchen" in body


def test_item_detail_renders_edit_and_delete_affordances(client):
    # U5 wires these routes; the page must render their links without 500ing.
    db = _db()
    kitchen = db.create_category("Kitchen")
    item = db.create_item("spare sponges", category_id=kitchen)

    body = _html(client.get(f"/item/{item}"))
    assert f'/item/{item}/edit' in body
    assert f'/item/{item}/delete' in body


def test_delete_confirm_survives_an_apostrophe_in_the_name(client):
    # Jinja autoescapes for HTML, not JS: interpolating a name into the inline
    # confirm() string turns "Kid's plates" into &#39;, which the HTML parser
    # decodes back to a bare quote and breaks the JS literal.
    db = _db()
    kitchen = db.create_category("Kitchen")
    item = db.create_item("Kid's plates", category_id=kitchen)

    body = _html(client.get(f"/item/{item}"))
    assert "&#39;" not in body.split('onsubmit="')[1].split('"')[0]


def test_unknown_item_returns_404(client):
    assert client.get("/item/999").status_code == 404


# --- Flow F1 ---------------------------------------------------------------

def test_browse_from_home_to_item_reads_quantity(client):
    # Covers F1: home -> Kitchen -> Glassware -> item, reading the quantity.
    db = _db()
    kitchen = db.create_category("Kitchen")
    glass = db.create_category("Glassware", parent_id=kitchen)
    item = db.create_item("Pint glass", category_id=glass, quantity=6)

    assert f'href="/category/{kitchen}"' in _html(client.get("/"))
    assert f'href="/category/{glass}"' in _html(client.get(f"/category/{kitchen}"))
    assert f'href="/item/{item}"' in _html(client.get(f"/category/{glass}"))
    assert "6" in _html(client.get(f"/item/{item}"))


def test_pages_carry_mobile_viewport(client):
    # Covers R9.
    assert 'name="viewport"' in _html(client.get("/"))


def test_json_api_still_works(client):
    # Guards KTD5: the page routes must not disturb the existing JSON API.
    resp = client.get("/inventory")
    assert resp.status_code == 200
    assert resp.get_json() == []


# --- Error pages (U4, R13) -------------------------------------------------

def test_an_unknown_item_renders_a_prims_page_with_a_way_back(client):
    """Covers AE10. A bare Werkzeug page is a dead end on a phone."""
    response = client.get("/item/9999")
    body = _html(response)

    assert response.status_code == 404
    assert 'href="/"' in body
    assert 'name="viewport"' in body


def test_a_missing_csrf_token_still_explains_itself(client):
    """The handler must not swallow the description the abort() carried."""
    response = client.post("/item/new", data={"name": "Pint glass"})
    body = _html(response)

    assert response.status_code == 400
    assert "CSRF token" in body
    assert 'href="/"' in body


def test_a_method_not_allowed_lands_on_a_prims_page(client):
    """405 is reachable today: a bookmark or back-button onto a delete URL."""
    response = client.get("/item/1/delete")
    body = _html(response)

    assert response.status_code == 405
    assert 'href="/"' in body


def test_an_unsupported_media_type_lands_on_a_prims_page(loopback_client):
    """415 is reachable today: a form-encoded POST to a JSON route."""
    response = loopback_client.post(
        "/inventory", data="name=x", content_type="application/x-www-form-urlencoded"
    )

    assert response.status_code == 415
    assert 'href="/"' in _html(response)


def test_an_unhandled_error_shows_no_console_and_no_traceback(client, monkeypatch):
    """Covers AE4. With debug off the interactive debugger must be gone."""
    import app as app_module
    import db

    monkeypatch.setattr(app_module.app, "debug", False)
    monkeypatch.setitem(app_module.app.config, "PROPAGATE_EXCEPTIONS", False)
    monkeypatch.setattr(db, "get_top_level_categories", _explode)

    response = client.get("/")
    body = _html(response)

    assert response.status_code == 500
    assert "Traceback" not in body
    assert "console" not in body.lower()
    assert 'href="/"' in body


def _explode(*args, **kwargs):
    raise RuntimeError("deliberate failure for the 500 path")

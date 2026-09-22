"""CSRF protection for the browser-facing form routes.

The web pages added plain form POSTs to an app with no auth and no session to
steal -- which is exactly why a forged POST would work: nothing else stands in
the way. Any page the owner has open can POST to /item/<id>/delete on
127.0.0.1 unless the request has to carry a secret the attacker cannot read.

These tests pin both halves of that: the three page endpoints reject a POST
without the session's token, and the pre-existing JSON API (driven by
tests/test_crud_operations.sh, a curl script that sends no cookies and no
tokens) stays exempt.
"""
import pytest

from conftest import csrf_token, post_form


def _db():
    """The data layer, already pointed at the throwaway DB by the client fixture."""
    import db

    return db


def _html(response):
    return response.get_data(as_text=True)


def _glassware():
    db = _db()
    kitchen = db.create_category("Kitchen")
    return db.create_category("Glassware", parent_id=kitchen)


def _pint_glass():
    db = _db()
    return db.create_item("Pint glass", category_id=_glassware(), quantity=6)


# --- The token reaches the forms -------------------------------------------

def test_add_form_renders_a_hidden_csrf_token(client):
    body = _html(client.get("/item/new"))
    assert 'name="csrf_token"' in body
    assert csrf_token(client)


def test_edit_form_renders_a_hidden_csrf_token(client):
    item = _pint_glass()
    body = _html(client.get(f"/item/{item}/edit"))
    assert 'name="csrf_token"' in body


def test_delete_form_on_the_item_page_renders_a_hidden_csrf_token(client):
    item = _pint_glass()
    body = _html(client.get(f"/item/{item}"))
    assert 'name="csrf_token"' in body


# --- A POST with no token is rejected and changes nothing ------------------

def test_add_without_a_token_is_rejected_and_creates_nothing(client):
    db = _db()
    glassware = _glassware()

    resp = client.post(
        "/item/new",
        data={"name": "Forged mug", "category_id": str(glassware), "quantity": "1"},
    )

    assert resp.status_code == 400
    assert db.get_all_items() == []


def test_edit_without_a_token_is_rejected_and_leaves_the_item_untouched(client):
    db = _db()
    item = _pint_glass()

    resp = client.post(
        f"/item/{item}/edit",
        data={"name": "Forged name", "category_id": "", "quantity": "99"},
    )

    assert resp.status_code == 400
    row = db.get_item(item)
    assert row["name"] == "Pint glass"
    assert row["quantity"] == 6


def test_delete_without_a_token_is_rejected_and_keeps_the_item(client):
    db = _db()
    item = _pint_glass()

    resp = client.post(f"/item/{item}/delete")

    assert resp.status_code == 400
    assert db.get_item(item) is not None


def test_delete_without_a_token_is_rejected_even_following_redirects(client):
    # The forged-request shape: a cross-site form auto-submitted by a page the
    # owner has open. It must not reach the view at all.
    db = _db()
    item = _pint_glass()

    resp = client.post(f"/item/{item}/delete", follow_redirects=True)

    assert resp.status_code == 400
    assert db.get_item(item) is not None


# --- A POST with the wrong token is rejected -------------------------------

@pytest.mark.parametrize("bogus", ["", "not-the-token", "x" * 43])
def test_add_with_a_wrong_token_is_rejected(client, bogus):
    db = _db()
    client.get("/item/new")  # establish a session with a real token

    resp = client.post(
        "/item/new",
        data={"name": "Forged mug", "quantity": "1", "csrf_token": bogus},
    )

    assert resp.status_code == 400
    assert db.get_all_items() == []


def test_edit_with_a_wrong_token_is_rejected(client):
    db = _db()
    item = _pint_glass()
    client.get(f"/item/{item}/edit")

    resp = client.post(
        f"/item/{item}/edit",
        data={"name": "Forged name", "quantity": "99", "csrf_token": "not-the-token"},
    )

    assert resp.status_code == 400
    assert db.get_item(item)["quantity"] == 6


def test_delete_with_a_wrong_token_is_rejected(client):
    db = _db()
    item = _pint_glass()
    client.get(f"/item/{item}")

    resp = client.post(f"/item/{item}/delete", data={"csrf_token": "not-the-token"})

    assert resp.status_code == 400
    assert db.get_item(item) is not None


def test_a_token_from_another_session_is_rejected(client, app_client):
    # The attacker can mint a token in their own browser; it must not validate
    # against the victim's session.
    db = _db()
    item = _pint_glass()
    other_sessions_token = csrf_token(app_client)
    client.get(f"/item/{item}")

    resp = client.post(
        f"/item/{item}/delete", data={"csrf_token": other_sessions_token}
    )

    assert resp.status_code == 400
    assert db.get_item(item) is not None


# --- The normal browser flow still works -----------------------------------

def test_add_with_the_token_the_form_rendered_succeeds(client):
    db = _db()
    glassware = _glassware()

    resp = post_form(
        client,
        "/item/new",
        {"name": "Wine glass", "category_id": str(glassware), "quantity": "4"},
        follow_redirects=True,
    )

    assert resp.status_code == 200
    assert [row["name"] for row in db.get_items_in_category(glassware)] == ["Wine glass"]


def test_edit_with_the_token_the_form_rendered_succeeds(client):
    db = _db()
    item = _pint_glass()

    resp = post_form(
        client,
        f"/item/{item}/edit",
        {"name": "Pint glass", "category_id": "", "quantity": "5"},
        form_path=f"/item/{item}/edit",
        follow_redirects=True,
    )

    assert resp.status_code == 200
    assert db.get_item(item)["quantity"] == 5


def test_delete_with_the_token_the_item_page_rendered_succeeds(client):
    db = _db()
    item = _pint_glass()

    resp = post_form(
        client,
        f"/item/{item}/delete",
        form_path=f"/item/{item}",
        follow_redirects=True,
    )

    assert resp.status_code == 200
    assert db.get_item(item) is None


def test_the_token_stays_valid_across_several_posts(client):
    # One browser session, several forms: the token is per session, not per form.
    db = _db()
    glassware = _glassware()
    token = csrf_token(client)

    for name in ("Tumbler", "Highball"):
        client.post(
            "/item/new",
            data={"name": name, "category_id": str(glassware), "csrf_token": token},
            follow_redirects=True,
        )

    assert sorted(row["name"] for row in db.get_items_in_category(glassware)) == [
        "Highball",
        "Tumbler",
    ]


# --- The JSON API stays exempt (KTD5 / tests/test_crud_operations.sh) -------

def test_json_api_post_without_a_token_still_succeeds(loopback_client):
    # test_crud_operations.sh is curl: no cookies, no session, no token.
    resp = loopback_client.post("/inventory", json={"name": "Laptop", "quantity": 1})
    assert resp.status_code == 201
    assert loopback_client.get("/inventory").get_json()[0]["name"] == "Laptop"


def test_json_category_and_location_posts_without_a_token_still_succeed(loopback_client):
    assert loopback_client.post("/categories", json={"name": "Electronics"}).status_code == 201
    assert loopback_client.post("/locations", json={"name": "Office"}).status_code == 201


def test_json_api_put_and_delete_without_a_token_still_succeed(loopback_client):
    created = loopback_client.post("/inventory", json={"name": "Laptop", "quantity": 1})
    item_id = created.get_json()["id"]

    assert loopback_client.put(
        f"/inventory/{item_id}", json={"name": "Laptop", "quantity": 2}
    ).status_code == 200
    assert loopback_client.delete(f"/inventory/{item_id}").status_code == 200


def test_file_upload_without_a_token_still_succeeds(loopback_client, tmp_path):
    import io

    import app as app_module

    original = app_module.app.config["UPLOAD_FOLDER"]
    (tmp_path / "uploads").mkdir()
    app_module.app.config["UPLOAD_FOLDER"] = str(tmp_path / "uploads")
    try:
        resp = loopback_client.post(
            "/upload",
            data={"file": (io.BytesIO(b"hello"), "note.txt")},
            content_type="multipart/form-data",
        )
    finally:
        app_module.app.config["UPLOAD_FOLDER"] = original

    assert resp.status_code == 201

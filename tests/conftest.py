"""Shared pytest fixtures for PRIMS tests.

Each fixture points the data layer at a throwaway SQLite file and rebuilds the
schema, so tests never touch the real prims.db.

**Every HTTP client here presents a non-loopback address by default.** Werkzeug's
test client uses 127.0.0.1, which the sign-in gate exempts (U2/KTD1), so without
this a `require_login` that did nothing would leave the whole suite green and
prove nothing. Loopback is opt-in through `loopback_client`, which exists
precisely to exercise the exemption.
"""
import os
import re
import sys

import pytest

SRC = os.path.join(os.path.dirname(__file__), "..", "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

_CSRF_INPUT = re.compile(
    r'name="csrf_token"[^>]*\svalue="([^"]+)"|value="([^"]+)"[^>]*\sname="csrf_token"'
)

#: What a device on the home network looks like -- anything but loopback.
LAN_ADDRESS = "192.168.1.50"

#: Long enough to clear the password floor in src/auth.py.
TEST_PASSWORD = "a-long-enough-passphrase"


def csrf_token(client, form_path="/item/new"):
    """The CSRF token a real browser would have, read off a rendered form.

    GETting the page both seeds the session token and hands back the hidden
    field the form emits, which is exactly the round trip a browser makes.
    """
    body = client.get(form_path).get_data(as_text=True)
    match = _CSRF_INPUT.search(body)
    assert match is not None, f"no csrf_token field rendered by {form_path}"
    return match.group(1) or match.group(2)


def post_form(client, path, data=None, form_path="/item/new", **kwargs):
    """POST a page form the way a browser does: with the token it rendered."""
    form = dict(data or {})
    form["csrf_token"] = csrf_token(client, form_path)
    return client.post(path, data=form, **kwargs)


def sign_in(client, password=TEST_PASSWORD):
    """Sign a client in the way a browser does, token and all."""
    return client.post(
        "/login",
        data={
            "username": "owner",
            "password": password,
            "csrf_token": csrf_token(client, "/login"),
        },
        follow_redirects=True,
    )


def _prepare(tmp_path):
    """Point the data layer and the secrets file at throwaway locations."""
    import app as app_module
    import auth
    import db

    db.DB_PATH = str(tmp_path / "test.db")
    db.reset_db()

    instance = str(tmp_path / "instance")
    app_module.app.instance_path = instance
    auth.init_secrets(instance, TEST_PASSWORD)
    # The key is read once at import; re-read it now that the file has moved.
    app_module.app.config["SECRET_KEY"] = auth.load_secrets(instance)["secret_key"]
    app_module.app.config["TESTING"] = True
    app_module.reset_sign_in_throttle()
    return app_module


def _lan_client(app_module):
    client = app_module.app.test_client()
    client.environ_base["REMOTE_ADDR"] = LAN_ADDRESS
    return client


@pytest.fixture
def db_mod(tmp_path):
    import db

    db.DB_PATH = str(tmp_path / "test.db")
    db.reset_db()
    return db


@pytest.fixture
def client(tmp_path):
    """The owner's browser: on the network, signed in."""
    app_module = _prepare(tmp_path)
    client = _lan_client(app_module)
    sign_in(client)
    return client


@pytest.fixture
def app_client(client):
    """A second browser of the owner's: separate cookie jar, also signed in.

    Its session is independent, so it stands in for a token minted in another
    session -- an attacker's browser, for the cross-session CSRF case.
    """
    import app as app_module

    other = _lan_client(app_module)
    sign_in(other)
    return other


@pytest.fixture
def stranger_client(client):
    """Someone else's device on the same network, not signed in (AE3)."""
    import app as app_module

    return _lan_client(app_module)


@pytest.fixture
def loopback_client(client):
    """curl on the machine itself: no cookies, no session, no token.

    This is the only client that takes the gate's loopback exemption, which is
    what keeps tests/test_crud_operations.sh working (KTD1/KTD5).
    """
    import app as app_module

    return app_module.app.test_client()

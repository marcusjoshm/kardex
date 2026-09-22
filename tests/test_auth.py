"""Persisted secrets and the setup command (U1).

The signing key must outlive the process or every restart signs the phone out,
which is the whole of R4. These tests pin that, the password floor, and the
session epoch that makes a lost phone revocable without rotating the key.
"""
import json
import os
import stat
import subprocess

import pytest

import auth


@pytest.fixture
def instance(tmp_path):
    """A throwaway instance directory, standing in for src/instance."""
    path = tmp_path / "instance"
    return str(path)


def test_secrets_survive_a_write_and_read(instance):
    written = auth.init_secrets(instance, "a-long-enough-passphrase")
    reloaded = auth.load_secrets(instance)

    assert reloaded["secret_key"] == written["secret_key"]
    assert reloaded["session_epoch"] == written["session_epoch"]


def test_a_correct_password_verifies_and_a_wrong_one_does_not(instance):
    auth.init_secrets(instance, "a-long-enough-passphrase")
    loaded = auth.load_secrets(instance)

    assert auth.check_password(loaded, "a-long-enough-passphrase") is True
    assert auth.check_password(loaded, "not-the-passphrase") is False


def test_the_password_is_stored_hashed_not_in_the_clear(instance):
    auth.init_secrets(instance, "a-long-enough-passphrase")
    raw = open(auth.secrets_path(instance)).read()

    assert "a-long-enough-passphrase" not in raw
    stored = json.loads(raw)["password_hash"]
    # Werkzeug prefixes the algorithm it used; scrypt is the 2.3+ default.
    assert stored.split(":", 1)[0] in {"scrypt", "pbkdf2"}


def test_a_password_under_the_floor_is_refused(instance):
    with pytest.raises(ValueError) as caught:
        auth.init_secrets(instance, "short")

    assert str(auth.MIN_PASSWORD_LENGTH) in str(caught.value)
    assert auth.load_secrets(instance) is None


def test_a_common_password_is_refused(instance):
    with pytest.raises(ValueError):
        auth.init_secrets(instance, "password1234")

    assert auth.load_secrets(instance) is None


def test_changing_the_password_leaves_other_devices_signed_in(instance):
    """Deliberate: a password change is NOT the answer to a lost phone.

    Keeping the key and epoch means existing sessions survive, which is what
    lets the owner change a password without signing their own phone out. The
    revocation path is sign_out_everywhere, and the README says so.
    """
    first = auth.init_secrets(instance, "the-first-passphrase")
    second = auth.init_secrets(instance, "the-second-passphrase")

    assert second["secret_key"] == first["secret_key"]
    assert second["session_epoch"] == first["session_epoch"]
    assert auth.check_password(second, "the-second-passphrase") is True
    assert auth.check_password(second, "the-first-passphrase") is False


def test_sign_out_everywhere_bumps_the_epoch_and_nothing_else(instance):
    before = auth.init_secrets(instance, "a-long-enough-passphrase")
    epoch = auth.sign_out_everywhere(instance)
    after = auth.load_secrets(instance)

    assert epoch == before["session_epoch"] + 1
    assert after["session_epoch"] == epoch
    assert after["secret_key"] == before["secret_key"]
    assert after["password_hash"] == before["password_hash"]


def test_the_secrets_file_is_readable_only_by_its_owner(instance):
    auth.init_secrets(instance, "a-long-enough-passphrase")
    mode = os.stat(auth.secrets_path(instance)).st_mode

    assert stat.S_IMODE(mode) == 0o600


def test_a_missing_secrets_file_reports_absence_rather_than_raising(instance):
    assert auth.load_secrets(instance) is None


def test_an_unreadable_secrets_file_is_a_named_error_not_a_traceback(instance):
    auth.init_secrets(instance, "a-long-enough-passphrase")
    with open(auth.secrets_path(instance), "w") as handle:
        handle.write("{ this is not json")

    with pytest.raises(auth.SecretsError) as caught:
        auth.load_secrets(instance)

    assert auth.SETUP_COMMAND in str(caught.value)


def test_the_real_instance_directory_is_git_ignored():
    """The secrets file lives inside the working tree, so this must hold."""
    repo_root = os.path.join(os.path.dirname(__file__), "..")
    result = subprocess.run(
        ["git", "check-ignore", "-q", "src/instance/secrets.json"],
        cwd=repo_root,
    )

    assert result.returncode == 0, "src/instance/ is not git-ignored"


# --- The sign-in gate (U2) --------------------------------------------------

from conftest import LAN_ADDRESS, TEST_PASSWORD, csrf_token, sign_in  # noqa: E402


def _db():
    import db

    return db


def test_a_device_on_the_network_cannot_read_or_change_the_inventory(stranger_client):
    """Covers AE3. Not signed in, not loopback: no read, no write."""
    db = _db()
    kitchen = db.create_category("Kitchen")
    db.create_item("Pint glass", category_id=kitchen, quantity=6)

    page = stranger_client.get("/")
    listing = stranger_client.get("/inventory")
    files = stranger_client.get("/files")
    write = stranger_client.post("/inventory", json={"name": "Ghost", "quantity": 1})

    for response in (page, listing, files, write):
        assert response.status_code != 200
        assert b"Pint glass" not in response.data
    assert [i["name"] for i in db.get_all_items()] == ["Pint glass"]


def test_the_stylesheet_is_reachable_signed_out(stranger_client):
    """Otherwise the sign-in page renders unstyled."""
    assert stranger_client.get("/static/style.css").status_code == 200


def test_loopback_reaches_the_json_api_with_no_cookie_or_token(loopback_client):
    """The curl contract: KTD5's exemption survives, but only from the machine."""
    created = loopback_client.post("/inventory", json={"name": "Laptop", "quantity": 1})

    assert created.status_code == 201
    assert loopback_client.get("/inventory").get_json()[0]["name"] == "Laptop"


def test_an_untrusted_host_from_loopback_is_refused_with_400(loopback_client):
    """The gate and host validation must compose; neither proves it alone.

    A rebinding page reaches the app over loopback, so the exemption would let
    it through. Host validation is what stops it -- and it only fires because
    the gate returns None for an unmatched endpoint instead of answering itself.
    """
    response = loopback_client.get("/", headers={"Host": "evil.com"})

    assert response.status_code == 400


def test_a_forwarded_for_header_does_not_buy_the_loopback_exemption(stranger_client):
    """request.access_route would believe this; request.remote_addr must not."""
    response = stranger_client.get(
        "/inventory", headers={"X-Forwarded-For": "127.0.0.1"}
    )

    assert response.status_code != 200


def test_an_unmatched_path_404s_rather_than_redirecting_to_sign_in(stranger_client):
    """Redirecting would confirm to a scanner that a login is listening."""
    assert stranger_client.get("/no-such-page").status_code == 404


def test_the_right_password_signs_in_and_the_wrong_one_does_not(stranger_client):
    good = sign_in(stranger_client)
    assert good.status_code == 200

    other = stranger_client.application.test_client()
    other.environ_base["REMOTE_ADDR"] = LAN_ADDRESS
    bad = other.post(
        "/login",
        data={
            "username": "owner",
            "password": "not-the-passphrase",
            "csrf_token": csrf_token(other, "/login"),
        },
    )

    assert bad.status_code == 401
    assert b"Wrong password" in bad.data
    assert other.get("/").status_code != 200


def test_a_session_from_a_previous_epoch_is_signed_out(client):
    import app as app_module
    import auth

    assert client.get("/").status_code == 200
    auth.sign_out_everywhere(app_module.app.instance_path)

    assert client.get("/").status_code != 200


def test_signing_out_one_device_leaves_the_other_signed_in(client, app_client):
    """Covers AE9."""
    client.post("/logout", data={"csrf_token": csrf_token(client, "/item/new")})

    assert client.get("/").status_code != 200
    assert app_client.get("/").status_code == 200


def test_sign_out_without_a_token_does_not_sign_the_owner_out(client):
    """The CSRF allowlist is the only thing enforcing this."""
    response = client.post("/logout", data={})

    assert response.status_code == 400
    assert client.get("/").status_code == 200


def test_sign_out_over_get_does_not_sign_the_owner_out(client):
    """An <img src="/logout"> must not end the session."""
    assert client.get("/logout").status_code == 405
    assert client.get("/").status_code == 200


def test_the_session_identifier_changes_across_sign_in(stranger_client):
    """The clear discards attacker-injectable pre-authentication state."""
    before = csrf_token(stranger_client, "/login")
    sign_in(stranger_client)
    after = csrf_token(stranger_client, "/item/new")

    assert before != after


def test_a_second_attempt_in_quick_succession_is_refused_before_hashing(
    stranger_client, monkeypatch
):
    """Bounds guessing, and stops /login being a 32MB pre-auth amplifier."""
    import app as app_module

    stranger_client.post(
        "/login",
        data={
            "username": "owner",
            "password": "wrong-but-long-enough",
            "csrf_token": csrf_token(stranger_client, "/login"),
        },
    )

    monkeypatch.setattr(app_module.auth, "check_password", _should_not_be_called)
    throttled = stranger_client.post(
        "/login",
        data={
            "username": "owner",
            "password": "wrong-but-long-enough",
            "csrf_token": csrf_token(stranger_client, "/login"),
        },
    )

    assert throttled.status_code == 429
    assert b"Too many" in throttled.data


def test_a_failed_attempt_is_logged_without_the_password(stranger_client, caplog):
    with caplog.at_level("WARNING"):
        stranger_client.post(
            "/login",
            data={
                "username": "owner",
                "password": "hunter2-hunter2",
                "csrf_token": csrf_token(stranger_client, "/login"),
            },
        )

    logged = caplog.text
    assert LAN_ADDRESS in logged
    assert "hunter2" not in logged


def test_the_session_survives_an_application_restart(client, tmp_path):
    """Covers AE5. The persisted key is the whole reason this holds."""
    import app as app_module
    import auth

    assert client.get("/").status_code == 200

    # Re-reading the key is what a restart does; the cookie must still verify.
    stored = auth.load_secrets(app_module.app.instance_path)
    app_module.app.config["SECRET_KEY"] = stored["secret_key"]

    assert client.get("/").status_code == 200


def _should_not_be_called(*args, **kwargs):
    raise AssertionError("password was hashed despite the throttle")

"""Persisted secrets and the setup command (U1).

The signing key must outlive the process or every restart signs the phone out,
which is the whole of R4. These tests pin that, the password floor, and the
session epoch that makes a lost phone revocable without rotating the key.
"""
import json
import os
import runpy
import stat
import subprocess
import sys

import pytest

import auth

SRC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))


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


def _read(path):
    with open(path) as handle:
        return handle.read()


def test_a_write_leaves_nothing_beside_the_secrets_file(instance):
    """The write goes through a temporary sibling; no copy of the hash may be
    left lying next to the real file, and the result is still owner-only."""
    auth.init_secrets(instance, "a-long-enough-passphrase")
    auth.sign_out_everywhere(instance)

    assert os.listdir(instance) == [auth.SECRETS_FILENAME]
    mode = os.stat(auth.secrets_path(instance)).st_mode
    assert stat.S_IMODE(mode) == 0o600


def test_a_reader_during_a_write_sees_the_old_file_whole(instance, monkeypatch):
    """The server reads this file on every request from the network, and
    --sign-out-everywhere runs while it serves. Truncating in place let a
    request catch an empty file and fail mid-revocation."""
    before = auth.init_secrets(instance, "a-long-enough-passphrase")
    seen = []
    real_fsync = os.fsync

    def fsync_then_read(descriptor):
        real_fsync(descriptor)
        # The new contents are written and flushed but not yet in place.
        seen.append(auth.load_secrets(instance))

    monkeypatch.setattr(auth.os, "fsync", fsync_then_read)
    auth.sign_out_everywhere(instance)

    assert seen == [before]
    assert auth.load_secrets(instance)["session_epoch"] == before["session_epoch"] + 1


def test_a_write_that_fails_midway_leaves_the_previous_file_intact(
    instance, monkeypatch
):
    """A full disk mid-write must not destroy the key and hash: losing them
    signs every device out and stops PRIMS starting on the network."""
    auth.init_secrets(instance, "a-long-enough-passphrase")
    path = auth.secrets_path(instance)
    original = _read(path)

    def torn_dump(data, handle, **kwargs):
        handle.write('{"secret_key": "half')
        raise OSError("No space left on device")

    monkeypatch.setattr(auth.json, "dump", torn_dump)
    with pytest.raises(OSError):
        auth.sign_out_everywhere(instance)

    assert _read(path) == original
    assert os.listdir(instance) == [auth.SECRETS_FILENAME]


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


# --- The setup command, `python src/auth.py` ---------------------------------

def test_the_setup_command_sets_the_password(instance, monkeypatch):
    monkeypatch.setattr(auth, "_prompt_for_password", lambda: "a-long-enough-passphrase")

    code = auth.main([], instance_path=instance)

    assert code == 0
    assert auth.check_password(auth.load_secrets(instance), "a-long-enough-passphrase")


def test_the_setup_command_refuses_a_short_password_and_writes_nothing(
    instance, monkeypatch, capsys
):
    monkeypatch.setattr(auth, "_prompt_for_password", lambda: "short")

    code = auth.main([], instance_path=instance)

    assert code == 1
    assert auth.load_secrets(instance) is None
    assert str(auth.MIN_PASSWORD_LENGTH) in capsys.readouterr().err


def test_the_setup_command_refuses_mismatched_entries_and_writes_nothing(
    instance, monkeypatch
):
    """Asking twice is what stops a typo becoming a password nobody knows."""
    entries = iter(["a-long-enough-passphrase", "a-long-enough-passphrasf"])
    monkeypatch.setattr(auth.getpass, "getpass", lambda prompt="": next(entries))

    code = auth.main([], instance_path=instance)

    assert code == 1
    assert auth.load_secrets(instance) is None


def test_signing_out_everywhere_before_any_password_is_set_exits_2(instance, capsys):
    """The likely first-run slip. It must name the setup command and exit 2,
    at the moment the owner is trying to recover from a lost phone."""
    code = auth.main(["--sign-out-everywhere"], instance_path=instance)

    assert code == 2
    assert auth.SETUP_COMMAND in capsys.readouterr().err
    assert auth.load_secrets(instance) is None


def test_signing_out_everywhere_from_the_command_line_bumps_the_epoch(
    instance, monkeypatch
):
    """No instance_path given: main() finds the folder itself, as the real
    command does -- from auth.DEFAULT_INSTANCE_PATH, not by importing app,
    which reads this same file and so could fail on the state being fixed."""
    before = auth.init_secrets(instance, "a-long-enough-passphrase")
    monkeypatch.setattr(auth, "DEFAULT_INSTANCE_PATH", instance)
    monkeypatch.setitem(sys.modules, "app", None)  # any `import app` now raises

    code = auth.main(["--sign-out-everywhere"])

    assert code == 0
    assert auth.load_secrets(instance)["session_epoch"] == before["session_epoch"] + 1


# --- Both entry points, one secrets file ------------------------------------

def _run_isolated(tmp_path, args):
    """Run Python in a fresh interpreter that can only see throwaway files.

    A fresh process is the honest test of anything that happens at import: in
    this one, app was imported long before any test corrupted a file. The
    working directory is a separate folder under tmp_path, so the uploads
    folder app.py creates at import lands there, and Flask's fallback to the
    working directory cannot coincide with the instance folder by accident.
    """
    cwd = tmp_path / "cwd"
    cwd.mkdir(exist_ok=True)
    env = dict(os.environ)
    env["PRIMS_INSTANCE_PATH"] = str(tmp_path / "instance")
    env["PRIMS_DB"] = str(tmp_path / "test.db")
    env["PYTHONPATH"] = SRC_DIR
    return subprocess.run(
        [sys.executable, *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _corrupt_secrets(tmp_path):
    instance = str(tmp_path / "instance")
    auth.init_secrets(instance, "a-long-enough-passphrase")
    path = auth.secrets_path(instance)
    with open(path, "w") as handle:
        handle.write("{ not json")
    return path


def test_running_app_as_a_script_reads_the_folder_the_setup_command_writes(
    tmp_path,
):
    """Run as `python src/app.py`, app's import name is __main__; Flask then put
    the instance folder under the working directory while auth.py wrote to
    src/instance, and the server refused to start with "no password is set"
    right after one was set. run_path with any name but __main__ puts Flask in
    the same position without running main(), which would bind a socket."""
    probe = (
        "import json, runpy, app, auth\n"
        f"script = runpy.run_path({os.path.join(SRC_DIR, 'app.py')!r},"
        " run_name='prims_as_a_script')\n"
        "print(json.dumps([app.app.instance_path, script['app'].instance_path,"
        " auth.DEFAULT_INSTANCE_PATH]))\n"
    )

    result = _run_isolated(tmp_path, ["-c", probe])

    assert result.returncode == 0, result.stderr
    imported, as_script, setup = json.loads(result.stdout.strip().splitlines()[-1])
    assert imported == as_script == setup == str(tmp_path / "instance")


def test_the_default_instance_folder_is_src_instance(tmp_path, monkeypatch):
    """Where the README and the git-ignore test say the secrets live, whatever
    the working directory -- depending on it is what split the two commands.
    Only the path is computed here; nothing in the real folder is read."""
    monkeypatch.delenv("PRIMS_INSTANCE_PATH", raising=False)
    monkeypatch.chdir(tmp_path)

    fresh = runpy.run_path(auth.__file__, run_name="prims_auth_probe")

    assert fresh["DEFAULT_INSTANCE_PATH"] == os.path.join(SRC_DIR, "instance")


def test_a_cold_start_on_a_corrupt_secrets_file_refuses_rather_than_tracing(
    tmp_path,
):
    """Corrupt first, then import: the order a real start after a crash has.

    test_startup's version corrupts the file after this process imported app,
    so it never sees the import. The signing key is read at import, and a bare
    SecretsError there pre-empted main()'s named refusal. The injected serve
    means a regression that got past the refusal still binds nothing.
    """
    path = _corrupt_secrets(tmp_path)
    probe = (
        "import sys, app\n"
        "sys.exit(app.main(['--host', '0.0.0.0'], serve=lambda **kwargs: None))\n"
    )

    result = _run_isolated(tmp_path, ["-c", probe])

    assert result.returncode == 2
    assert "Traceback" not in result.stderr
    # Refused over this file, not for some other reason such as a missing one.
    assert path in result.stderr
    assert auth.SETUP_COMMAND in result.stderr
    assert _read(path) == "{ not json"  # refused, not silently replaced


def test_signing_out_everywhere_on_a_corrupt_file_is_a_named_error(tmp_path):
    """The recovery command must survive the state it exists to recover from.
    It used to import app to find the folder, and that import crashed first."""
    path = _corrupt_secrets(tmp_path)

    result = _run_isolated(
        tmp_path, [os.path.join(SRC_DIR, "auth.py"), "--sign-out-everywhere"]
    )

    assert result.returncode == 2
    assert "Traceback" not in result.stderr
    assert path in result.stderr
    assert auth.SETUP_COMMAND in result.stderr
    assert _read(path) == "{ not json"


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


def test_sign_in_without_a_token_does_not_sign_in(stranger_client):
    """The CSRF allowlist is the only thing enforcing this, and every other
    POST to /login in the suite carries a valid token -- so dropping 'sign_in'
    from the list would leave a form that looks protected and is not."""
    csrf_token(stranger_client, "/login")  # the session has a token; the form omits it
    response = stranger_client.post(
        "/login", data={"username": "owner", "password": TEST_PASSWORD}
    )

    assert response.status_code == 400
    assert stranger_client.get("/").status_code != 200


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


# --- Hardening (U6) ---------------------------------------------------------

def _session_cookie_header(response):
    for header in response.headers.getlist("Set-Cookie"):
        if header.startswith("session="):
            return header
    raise AssertionError("no session cookie was set")


def test_the_session_cookie_is_httponly_and_samesite_lax(stranger_client):
    """SameSite=Lax is load-bearing: the JSON API is CSRF-exempt."""
    response = stranger_client.post(
        "/login",
        data={
            "username": "owner",
            "password": TEST_PASSWORD,
            "csrf_token": csrf_token(stranger_client, "/login"),
        },
    )
    cookie = _session_cookie_header(response)

    assert "HttpOnly" in cookie
    assert "SameSite=Lax" in cookie


def test_the_session_cookie_is_not_marked_secure(stranger_client):
    """Secure over plain HTTP: the browser stores the cookie and never sends it,
    so sign-in silently never completes. There is no TLS here by decision."""
    response = stranger_client.post(
        "/login",
        data={
            "username": "owner",
            "password": TEST_PASSWORD,
            "csrf_token": csrf_token(stranger_client, "/login"),
        },
    )

    assert "Secure" not in _session_cookie_header(response)


def test_responses_carry_the_basic_security_headers(client):
    response = client.get("/")

    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "SAMEORIGIN"


def _uploads(tmp_path, monkeypatch):
    import app as app_module

    folder = tmp_path / "uploads"
    folder.mkdir()
    monkeypatch.setitem(app_module.app.config, "UPLOAD_FOLDER", str(folder))
    return folder


def test_a_download_is_served_as_an_attachment(loopback_client, tmp_path, monkeypatch):
    """An uploaded .html must not run as script on this origin."""
    folder = _uploads(tmp_path, monkeypatch)
    (folder / "page.txt").write_text("<script>alert(1)</script>")

    response = loopback_client.get("/files/page.txt")

    assert response.status_code == 200
    assert response.headers["Content-Disposition"].startswith("attachment")


def test_a_traversal_shaped_delete_neither_deletes_nor_fails(
    loopback_client, tmp_path, monkeypatch
):
    """The safety margin used to be the router's alone, and the router changed."""
    folder = _uploads(tmp_path, monkeypatch)
    outside = tmp_path / "outside.txt"
    outside.write_text("must survive")

    response = loopback_client.delete("/files/..%2Foutside.txt")

    assert response.status_code in (400, 404)
    assert outside.exists()


def test_the_delete_route_is_safe_even_if_the_router_lets_traversal_through(
    client, tmp_path, monkeypatch
):
    """Defence in depth. The router blocks this today, so the test above cannot
    go red; calling the view directly simulates a front-end that does not."""
    import app as app_module

    folder = _uploads(tmp_path, monkeypatch)
    outside = tmp_path / "outside.txt"
    outside.write_text("must survive")

    with app_module.app.test_request_context(method="DELETE"):
        response = app_module.delete_file("../outside.txt")

    status = response[1] if isinstance(response, tuple) else response.status_code
    assert status in (400, 404)
    assert outside.exists()

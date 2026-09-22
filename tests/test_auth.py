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

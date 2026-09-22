"""Persisted secrets for PRIMS: signing key, password hash, session epoch (U1).

The signing key must outlive the process. Flask's default is to mint a fresh one
per start, which is fine while the app only answers on loopback but fatal once a
phone is meant to stay signed in: every restart would invalidate the cookie and
ask for the password again, which is exactly the failure R4 exists to prevent.

Everything lives in one JSON file inside Flask's instance folder -- out of the
repository (``.gitignore`` already matches ``instance/`` at any depth), out of the
process environment where another process of the same user could read it, and
out of the database, which would otherwise need a schema migration and a users
table for a single-owner app.

The ``session_epoch`` is the revocation path. A signed cookie cannot be revoked
server-side, so a lost phone would otherwise mean rotating the signing key and
signing every device out, including the laptop. Bumping the epoch invalidates
every session without touching the key. Note that changing the password
deliberately does *not* bump it -- see ``init_secrets``.

Run it from the repository root::

    python src/auth.py                        # set or change the password
    python src/auth.py --sign-out-everywhere  # revoke every signed-in device
"""
import argparse
import getpass
import json
import os
import secrets
import sys

from werkzeug.security import check_password_hash, generate_password_hash

SECRETS_FILENAME = "secrets.json"
SETUP_COMMAND = "python src/auth.py"

# The password is typed by hand on a phone with no autofill, which pushes hard
# toward something short. With no lockout, its length is most of what stands
# between the inventory and a patient guesser on the network, so the floor is
# enforced here -- at a one-time prompt -- rather than left to good intentions.
MIN_PASSWORD_LENGTH = 12

# Not a dictionary, just the handful that a floor alone would happily accept.
COMMON_PASSWORDS = frozenset(
    {
        "password1234",
        "passwordpassword",
        "123456789012",
        "qwertyuiopas",
        "letmeinletmein",
        "iloveyouiloveyou",
        "administrator",
        "prims-inventory",
    }
)


class SecretsError(Exception):
    """The secrets file exists but cannot be used."""


def secrets_path(instance_path):
    """Where the secrets file lives for a given instance directory."""
    return os.path.join(instance_path, SECRETS_FILENAME)


def validate_password(password):
    """Return an error message, or None when the password is acceptable."""
    if len(password) < MIN_PASSWORD_LENGTH:
        return (
            f"Password must be at least {MIN_PASSWORD_LENGTH} characters. "
            "You will type it about once per device, so a passphrase costs "
            "you nothing and buys a lot."
        )
    if password.lower() in COMMON_PASSWORDS:
        return "That password is too common to be worth the length it has."
    return None


def load_secrets(instance_path):
    """The stored secrets, or None when none have been written yet.

    Absence is an ordinary state -- a fresh checkout has no secrets and still
    runs on loopback. A file that exists but cannot be parsed is not absence;
    treating it as such would silently mint a new key and sign every device
    out, so it raises instead.
    """
    path = secrets_path(instance_path)
    if not os.path.exists(path):
        return None
    try:
        with open(path) as handle:
            return json.load(handle)
    except (json.JSONDecodeError, OSError) as exc:
        raise SecretsError(
            f"Cannot read {path}: {exc}. Fix or remove the file, then run "
            f"`{SETUP_COMMAND}` to write a fresh one. Removing it signs every "
            "device out."
        ) from exc


def _write_secrets(instance_path, data):
    """Write the secrets file with owner-only permissions."""
    os.makedirs(instance_path, mode=0o700, exist_ok=True)
    path = secrets_path(instance_path)
    # Create with the right mode rather than widening then narrowing, so the
    # hash is never briefly world-readable.
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        json.dump(data, handle, indent=2)
        handle.write("\n")
    os.chmod(path, 0o600)
    return data


def init_secrets(instance_path, password):
    """Set or change the password, creating the signing key on first use.

    Changing the password keeps the signing key and the session epoch, so other
    devices stay signed in. That is deliberate and it is the reason a password
    change is not the answer to a lost phone -- ``sign_out_everywhere`` is.
    """
    error = validate_password(password)
    if error:
        raise ValueError(error)

    existing = load_secrets(instance_path) or {}
    return _write_secrets(
        instance_path,
        {
            "secret_key": existing.get("secret_key") or secrets.token_hex(32),
            "password_hash": generate_password_hash(password),
            "session_epoch": existing.get("session_epoch", 1),
        },
    )


def check_password(stored, password):
    """True when the password matches the stored hash.

    Call this only on the sign-in POST. The hash is deliberately expensive --
    tens of milliseconds and tens of megabytes -- which is correct once and
    wrong on every request.
    """
    if not stored or not stored.get("password_hash"):
        return False
    return check_password_hash(stored["password_hash"], password)


def sign_out_everywhere(instance_path):
    """Invalidate every signed-in device. Returns the new epoch."""
    stored = load_secrets(instance_path)
    if stored is None:
        raise SecretsError(
            f"No secrets file yet; nothing to sign out. Run `{SETUP_COMMAND}` first."
        )
    stored["session_epoch"] = stored.get("session_epoch", 1) + 1
    _write_secrets(instance_path, stored)
    return stored["session_epoch"]


def _prompt_for_password():
    """Ask twice, so a typo does not become the password."""
    first = getpass.getpass("New PRIMS password: ")
    second = getpass.getpass("Again: ")
    if first != second:
        print("Those did not match; nothing was changed.", file=sys.stderr)
        return None
    return first


def main(argv=None, instance_path=None):
    parser = argparse.ArgumentParser(
        description="Set the PRIMS password, or sign every device out.",
    )
    parser.add_argument(
        "--sign-out-everywhere",
        action="store_true",
        help="invalidate every signed-in device without changing the password",
    )
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    if instance_path is None:
        import app as app_module

        instance_path = app_module.app.instance_path

    if args.sign_out_everywhere:
        try:
            epoch = sign_out_everywhere(instance_path)
        except SecretsError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        print(f"Signed every device out (epoch {epoch}). Sign in again on each.")
        return 0

    try:
        password = _prompt_for_password()
    except (EOFError, KeyboardInterrupt):
        print()
        return 1
    if password is None:
        return 1

    try:
        init_secrets(instance_path, password)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except SecretsError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    print(f"Password set. Secrets live in {secrets_path(instance_path)}.")
    print("Devices already signed in stay signed in; use")
    print(f"  {SETUP_COMMAND} --sign-out-everywhere")
    print("to revoke them.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Starting the app safely (U5).

The refusal is the point of this unit: binding beyond loopback with no password
would put the whole inventory, readable and writable, on the home network.
`main()` takes an injectable `serve` so these tests check the decision without
opening a socket.
"""
import io

import pytest

import app as app_module
import auth


class _Recorder:
    """Stands in for the server; records what it would have been asked to do."""

    def __init__(self):
        self.calls = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)


@pytest.fixture
def fresh_instance(tmp_path, monkeypatch):
    """An instance directory with no secrets file in it.

    Also points the data layer at a throwaway file: main() calls db.init_db(),
    and without this it would run against whatever DB_PATH was last set --
    possibly the real prims.db.
    """
    import db

    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "test.db"))
    instance = str(tmp_path / "instance")
    monkeypatch.setattr(app_module.app, "instance_path", instance)
    monkeypatch.setattr(app_module.app, "debug", False)
    return instance


@pytest.fixture
def configured_instance(fresh_instance):
    auth.init_secrets(fresh_instance, "a-long-enough-passphrase")
    return fresh_instance


def test_binding_to_the_network_with_no_password_refuses_to_start(
    fresh_instance, capsys
):
    """Covers AE8."""
    serve = _Recorder()

    code = app_module.main(["--host", "0.0.0.0"], serve=serve)

    assert code != 0
    assert serve.calls == []
    assert auth.SETUP_COMMAND in capsys.readouterr().err


def test_binding_to_the_network_with_a_password_starts(configured_instance):
    serve = _Recorder()

    code = app_module.main(["--host", "0.0.0.0", "--port", "8080"], serve=serve)

    assert code == 0
    assert serve.calls == [{"host": "0.0.0.0", "port": 8080}]


def test_loopback_with_no_password_still_starts(fresh_instance):
    """A fresh checkout has to run somewhere before it has been configured."""
    serve = _Recorder()

    code = app_module.main(["--host", "127.0.0.1"], serve=serve)

    assert code == 0
    assert len(serve.calls) == 1


def test_the_default_bind_is_loopback(fresh_instance):
    """Reaching the network is something the owner asks for, never a default."""
    serve = _Recorder()

    app_module.main([], serve=serve)

    assert serve.calls[0]["host"] == "127.0.0.1"


def test_a_corrupt_secrets_file_refuses_rather_than_tracing(fresh_instance, capsys):
    auth.init_secrets(fresh_instance, "a-long-enough-passphrase")
    with open(auth.secrets_path(fresh_instance), "w") as handle:
        handle.write("{ not json")
    serve = _Recorder()

    code = app_module.main(["--host", "0.0.0.0"], serve=serve)

    assert code != 0
    assert serve.calls == []
    assert auth.SETUP_COMMAND in capsys.readouterr().err


def test_the_network_path_never_serves_the_debugger(configured_instance, monkeypatch):
    """Covers R2. The dev server's debugger is remote code execution."""
    serve = _Recorder()
    monkeypatch.setattr(app_module.app, "debug", True)

    app_module.main(["--host", "0.0.0.0"], serve=serve)

    assert app_module.app.debug is False


def test_an_upload_over_the_size_limit_is_refused(loopback_client):
    """Refused before the body is read into memory, not after."""
    limit = app_module.app.config["MAX_CONTENT_LENGTH"]
    oversized = io.BytesIO(b"x" * (limit + 1))

    response = loopback_client.post(
        "/upload",
        data={"file": (oversized, "big.txt")},
        content_type="multipart/form-data",
    )

    assert response.status_code == 413
    assert 'href="/"' in response.get_data(as_text=True)

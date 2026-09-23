"""Starting the app safely (U5).

The refusal is the point of this unit: binding beyond loopback with no password
would put the whole inventory, readable and writable, on the home network.
`main()` takes an injectable `serve` so these tests check the decision without
opening a socket. The ones that drive the real Waitress path replace
`waitress.create_server` instead, for the same reason: no test binds a port.
"""
import errno
import io
import socket

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


def test_the_network_defaults_to_a_port_macos_leaves_free(configured_instance):
    """macOS's AirPlay Receiver holds *:5000, so a 0.0.0.0 bind there fails."""
    serve = _Recorder()

    code = app_module.main(["--host", "0.0.0.0"], serve=serve)

    assert code == 0
    assert serve.calls == [{"host": "0.0.0.0", "port": 5050}]


def test_the_loopback_default_port_stays_5000(fresh_instance):
    """tests/test_crud_operations.sh curls 127.0.0.1:5000."""
    serve = _Recorder()

    app_module.main([], serve=serve)

    assert serve.calls == [{"host": "127.0.0.1", "port": 5000}]


class _FakeWaitressServer:
    """What waitress.create_server returns, minus the socket."""

    def __init__(self, on_run):
        self._on_run = on_run

    def print_listen(self, format_str):
        pass

    def run(self):
        self._on_run()


def test_a_port_in_use_is_named_rather_than_announced(
    configured_instance, monkeypatch, capsys
):
    """No address is printed for a server that never started."""
    import waitress

    def refuse(application, **kwargs):
        raise OSError(errno.EADDRINUSE, "Address already in use")

    monkeypatch.setattr(waitress, "create_server", refuse)

    code = app_module.main(["--host", "0.0.0.0"])

    out, err = capsys.readouterr()
    assert code == 2
    assert "5050" in err
    assert "--port" in err
    assert "http://" not in out + err


def test_the_address_is_announced_only_once_the_port_is_bound(
    configured_instance, monkeypatch, capsys
):
    import waitress

    printed_at = {}

    def bind(application, **kwargs):
        printed_at["bind"] = capsys.readouterr().out
        printed_at["bind_args"] = kwargs
        return _FakeWaitressServer(
            on_run=lambda: printed_at.setdefault("run", capsys.readouterr().out)
        )

    monkeypatch.setattr(waitress, "create_server", bind)

    code = app_module.main(["--host", "0.0.0.0"])

    assert code == 0
    assert printed_at["bind_args"] == {"host": "0.0.0.0", "port": 5050}
    assert "http://" not in printed_at["bind"]
    assert f"http://{socket.gethostname().lower()}:5050" in printed_at["run"]


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

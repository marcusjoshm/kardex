---
title: Running the Flask app as a script resolves instance_path to a different folder than the setup command used
date: 2026-09-23
category: runtime-errors
module: src/auth.py, src/app.py (Flask instance path resolution)
problem_type: runtime_error
component: authentication
symptoms:
  - "Following the README's first run (`python src/auth.py`, then `python src/app.py --host 0.0.0.0`) ends in a refusal saying no password is set, right after a password was set"
  - "`python src/auth.py` imported `app` by name and wrote `src/instance/secrets.json`; `python src/app.py`, running as `__main__`, looked for `<cwd>/instance/secrets.json` instead, and nothing reported the mismatch"
  - "`--sign-out-everywhere` reports success but bumps the session epoch in a file the running server never reads, so no session is revoked"
  - "The full pytest suite (221 tests) passed despite the defect, because every test imports `app` by name and conftest overwrites `app.instance_path` before the divergent path can matter"
root_cause: config_error
resolution_type: code_fix
severity: high
related_components:
  - src/auth.py
  - src/app.py
  - tests/conftest.py
  - flask_instance_path_resolution
tags: [flask, instance-path, dunder-main, working-directory, entry-point, secrets-file, authentication, test-coverage-gap]
---

# Running the Flask app as a script resolves instance_path to a different folder than the setup command used

## Problem

The README's two first-run commands used two different secrets files. `python src/auth.py` sets the password and `python src/app.py --host 0.0.0.0` serves the home network, but each resolved Flask's instance folder to a different directory, so the server never saw the password the setup command had just written. The cause is in Flask. When `src/app.py` runs as a script, its import name is `__main__`, and Flask anchors a `__main__` app's `instance_path` to the **current working directory**. When the same module is imported by name, Flask anchors it next to the file.

## Symptoms

Following the README's first-run steps exactly (`README.md:54-56`), from the repository root:

```
$ python src/auth.py
...
Password set. Secrets live in <repo>/src/instance/secrets.json.

$ python src/app.py --host 0.0.0.0
Refusing to listen on 0.0.0.0: no password is set, so the whole inventory would be readable and writable by anyone on the network.
Run `python src/auth.py` first.
```

The second command exits with status 2. Both messages come from the code before the fix (`git show 87de46a:src/auth.py`, line 214, and `git show 87de46a:src/app.py`, lines 884-889). The refusal tells the owner to run the command they just ran. The code-review correctness reviewer reproduced it, and so did an independent validator with serving stubbed out: exit 2 and "no password is set", with a valid `src/instance/secrets.json` on disk.

What made it confusing:

- **Nothing reports an error.** Flask accepts an instance folder that doesn't exist, and `load_secrets` treats a missing file as normal. It returns `None` (`src/auth.py:102-104`), because a fresh checkout also has no secrets. So the server's view was "no password has been set," which is a valid state.
- **The loopback default looks healthy.** `python src/app.py` with no `--host` starts fine. With no stored key, `_load_signing_key` falls back to an env var or a generated key (`src/app.py:41-43`). The refusal only applies off loopback, so only the network path showed the bug.
- **The result depends on where you launch from.** The server looked in `<cwd>/instance`. From the repo root that is `<repo>/instance`, which is empty. From inside `src/` (`cd src && python app.py`) it is `src/instance`, which happens to match the setup command. So one person can hit the bug and another can't reproduce it.
- **Revocation fails silently.** `python src/auth.py --sign-out-everywhere` is the lost-phone command. It would bump `session_epoch` in `src/instance/secrets.json`. The running server checks each request's epoch against the file under its own `app.instance_path` (`src/app.py:180-183`), so it would never see the change. The command would print "Signed every device out" and revoke nothing.

## What Didn't Work

The bug didn't come from any recent logic change. It depended on *how the module was started*, and no automated check ever started it the way the owner does.

**The full pytest suite was green, and structurally it could not fail on this.** The suite had 221 tests at the time of the review, per the review run. Every test gets the app by importing it by name. See `import app as app_module` in `tests/conftest.py:78` and `tests/test_startup.py:15`. So `__name__` was always `'app'`, and Flask always computed the module-relative `src/instance`. The fixtures then overwrote even that value:

- conftest's `_prepare` assigns `app_module.app.instance_path = instance` (`tests/conftest.py:86`; line 75 before the fix).
- test_startup's `fresh_instance` monkeypatches the same attribute (`tests/test_startup.py:41`).

No test ever asserted on the path Flask computed, and Flask's `__main__` branch never ran under pytest.

**`test_startup.py` tested `main()`, not the program.** It calls `app_module.main(["--host", "0.0.0.0"], serve=serve)` in-process with an injected server (`tests/test_startup.py:5-7`, `:58`). That checks main's decision logic well, and it's the right way to avoid binding sockets in tests. But calling `main()` in-process and running `python src/app.py` differ in one thing: the module's `__name__`. That was the broken part. And `main()`'s refusal reads `app.instance_path` (`src/app.py:940`), which the fixture had already replaced.

**The setup command had no tests.** The fix commit's own message says "the CLI had no tests". Before the fix, `auth.main` got its path by importing the web app (`git show 87de46a:src/auth.py`, lines 183-186). No test covered that line.

**Manual runs looked right because the rest of Flask anchors differently.** Templates and static files use `app.root_path`, which Flask computes with a different function. `get_root_path` checks `sys.modules[import_name].__file__` first (`flask/helpers.py:587-590`, Flask 3.1.3), and a script's `__main__` module has a `__file__`. So under `python src/app.py` every page renders from the right folder, and only the instance folder moves. Here are both values for a `__main__` app, with `__main__.__file__` set to `src/app.py` and the working directory set to `/tmp`:

```
root_path     <repo>/src
instance_path /private/tmp/instance
```

**A latent effect of the same blind spot.** `src/app.py` reads the secrets file at import: `app.config['SECRET_KEY'] = _load_signing_key()` (`src/app.py:46`; line 36 before the fix). Conftest only redirected `instance_path` after import, so every test run had been reading the owner's real `src/instance/secrets.json` before the redirect.

**What did work** was a multi-agent code review. The correctness reviewer put the two entry points side by side and asked which file each would read, and an independent validator confirmed the finding. The same review found two more defects of the same shape, where tests never ran the real entry point: a corrupt secrets file crashed at import, and the network default port collided with macOS AirPlay Receiver. Both were fixed on the same branch. Neither is covered here, and as of this writing neither has its own doc.

## Solution

The fix is commit `890e069` on branch `feat/phone-capture-logging`. It exists only locally: it has not been pushed, it has no PR, and it has not been merged to `main`. The SHAs in this doc are local-branch SHAs, as of 2026-09-23, and a rebase or squash merge will change them. If they no longer resolve, find the fix by its subject, "fix(review): apply findings #1 #2 #11 #8 #3 -- secrets file location and robustness". The pre-fix state, `87de46a`, is that commit's parent.

`src/instance/secrets.json` is created at first run and is gitignored, so it never appears in the tree. Flask paths such as `flask/sansio/app.py` are relative to the installed package, Flask 3.1.3 under `venv/lib/python3.14/site-packages/`.

It defines the instance folder once, anchors it to a file instead of the import name, and passes it to Flask so Flask never has to infer it.

**`src/app.py`, before** (`git show 87de46a:src/app.py`, line 17):

```python
app = Flask(__name__)
```

**after** (`src/app.py:18-21`):

```python
# Pinned, not derived: run as `python src/app.py` the import name is __main__
# and Flask would put the instance folder under the working directory, apart
# from the one `python src/auth.py` writes. See auth.DEFAULT_INSTANCE_PATH.
app = Flask(__name__, instance_path=auth.DEFAULT_INSTANCE_PATH)
```

**`src/auth.py`: the one definition** (`src/auth.py:46-49`):

```python
DEFAULT_INSTANCE_PATH = os.path.abspath(
    os.environ.get("PRIMS_INSTANCE_PATH")
    or os.path.join(os.path.dirname(os.path.abspath(__file__)), "instance")
)
```

`os.path.abspath` is required. Flask raises `ValueError("If an instance path is provided it must be absolute...")` for a relative `instance_path` (`flask/sansio/app.py:305-308`). `PRIMS_INSTANCE_PATH` follows the existing `PRIMS_DB` override in `src/db.py:20`.

**`auth.main`, before** (`git show 87de46a:src/auth.py`, lines 183-186). It imported the whole web app to learn a directory name:

```python
    if instance_path is None:
        import app as app_module

        instance_path = app_module.app.instance_path
```

**after** (`src/auth.py:219-222`):

```python
    if instance_path is None:
        # Not `import app`: importing it reads this same file, so a corrupt one
        # would crash the very command that exists to recover from it.
        instance_path = DEFAULT_INSTANCE_PATH
```

**`tests/conftest.py` now sets the override before anything imports `app` or `auth`**, and sets it unconditionally so a value exported in the owner's shell can't point the suite back at real data (`tests/conftest.py:21-27`):

```python
_SESSION_INSTANCE_ROOT = tempfile.mkdtemp(prefix="prims-tests-")
atexit.register(shutil.rmtree, _SESSION_INSTANCE_ROOT, ignore_errors=True)
os.environ["PRIMS_INSTANCE_PATH"] = os.path.join(_SESSION_INSTANCE_ROOT, "instance")
```

Two regression tests were added to `tests/test_auth.py` (see Prevention). The fix commit's message says each new test was checked to fail against the code it guards. The commit reports 234 tests passing, and the suite is at 243 after the remaining review fixes on the branch.

## Why This Works

Here is how Flask 3.1.3 picks the folder when the constructor gets no `instance_path` (`flask/sansio/app.py:303-304`):

1. It calls `auto_find_instance_path` (`flask/sansio/app.py:510-521`), which calls `find_package(self.import_name)`.
2. `find_package` calls `_find_package_path` (`flask/sansio/scaffold.py:709-724`), which runs `importlib.util.find_spec(root_mod_name)` (line 714).
3. For a script run directly, `__main__.__spec__` is `None`, so `find_spec("__main__")` raises `ValueError`. Flask's comment at line 722 names this case: "the module name is `__main__`". The `except` branch returns **`os.getcwd()`** (line 724).
4. That path isn't under `sys.prefix` or a `site-packages`, so `auto_find_instance_path` returns `os.path.join(package_path, "instance")` (line 520), which is `<cwd>/instance`.

For import name `app`, `find_spec` finds `src/app.py` and the module branch returns its directory (`flask/sansio/scaffold.py:749-751`), so the result is `src/instance`. Same source file, two answers. The `__main__` answer depends on the shell's working directory, not on where the code lives. You can reproduce it in any empty directory: `Flask('__main__').instance_path` evaluates to `<that directory>/instance`. Flask's docstring for `auto_find_instance_path` says the folder is found "next to your main file" (`flask/sansio/app.py:511-514`). For a `__main__` app that is wrong.

Before the fix, PRIMS had one process in each branch. `auth.py` imported `app` by name and got `src/instance`. `app.py` ran as `__main__` and got `<cwd>/instance`.

The fix takes away Flask's inference instead of trying to make both inferences agree:

- `__file__` is the same whether a module runs as a script or is imported, so `os.path.dirname(os.path.abspath(__file__))` gives the same answer under both names and from any working directory.
- Passing the result as `instance_path=` skips the inference entirely (the `if instance_path is None` branch in `flask/sansio/app.py:303` is never taken).
- Both entry points now read one constant. `auth.main` no longer asks the web app where the folder is, so the setup command's answer doesn't depend on how some other module was imported.

The environment override matters for the tests. The path is computed at import (`src/auth.py:46` is module-level, and `src/app.py:46` reads the file at import). Before the fix, the only way to redirect the path was to patch the attribute after import, and that is what let the suite skip the path question entirely. With an override that is read at import, a subprocess can be started with a throwaway folder and exercise the real startup, with no patching.

## Prevention

**1. In an app that is started as `python path/to/app.py`, never let Flask infer `instance_path`.** Pass it explicitly and derive it from `__file__`. More generally, any path derived from the import name or the working directory can have one value when the module is run and another when it is imported. When a docstring says "Run it from the repository root" (`src/auth.py:20`, `src/app.py:906`), treat that as a sign that something still depends on the working directory, and check what it is.

**2. Apply the same check to every path that more than one piece of code resolves.** A path is safe only if every reader and writer resolves it the same way. While this doc was being written, the same check found a second pair that disagrees. It hasn't been fixed, and it already exists on `main`:

- `UPLOAD_FOLDER = 'uploads'` (`src/app.py:49`) is saved to and listed relative to the working directory (`src/app.py:814`, `:822`).
- `send_from_directory` resolves a relative directory against `app.root_path` (`flask/helpers.py:402`), which is `src/`.
- Probed from the repo root with the test client, `GET /files` listed the empty `<repo>/uploads`, while `GET /files/evil.txt` returned 200 from `<repo>/src/uploads`.

The `PRIMS_DB` default `"prims.db"` (`src/db.py:20`) is also relative to the working directory. It is at least consistent: both commands resolve it the same way, as long as they run from the same directory.

**3. Test the real entry point in a fresh process, from a different working directory.** In-process tests import the module by name, so they can't reach the `__main__` branch. The regression test (`tests/test_auth.py:284-304`) runs a fresh interpreter. It loads `src/app.py` the way a script would be loaded and compares all three answers:

```python
def test_running_app_as_a_script_reads_the_folder_the_setup_command_writes(
    tmp_path,
):
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
```

Two details make it work:

- **Why `run_name='prims_as_a_script'` and not `'__main__'`.** Running as `__main__` would trigger `if __name__ == '__main__': raise SystemExit(main())` (`src/app.py:974-975`) and bind a socket. Any name that `find_spec` can't resolve takes the same Flask branch. `find_spec` returns `None`, Flask raises `ValueError("not found")` itself (`flask/sansio/scaffold.py:716-717`), and it ends at the same `return os.getcwd()`. `Flask('prims_as_a_script').instance_path` equals `<cwd>/instance`, the same as `Flask('__main__')`.
- **The working directory must not be the folder that holds the instance directory.** `_run_isolated` (`tests/test_auth.py:250-272`) points `PRIMS_INSTANCE_PATH` at `tmp_path/instance` but runs the subprocess in `tmp_path/cwd`. If the subprocess ran in `tmp_path`, Flask's fallback `<cwd>/instance` would equal the override by coincidence. A regression back to `Flask(__name__)` would then still pass. The docstring says so: "Flask's fallback to the working directory cannot coincide with the instance folder by accident" (lines 256-257).

```python
    cwd = tmp_path / "cwd"
    cwd.mkdir(exist_ok=True)
    env = dict(os.environ)
    env["PRIMS_INSTANCE_PATH"] = str(tmp_path / "instance")
    env["PRIMS_DB"] = str(tmp_path / "test.db")
    env["PYTHONPATH"] = SRC_DIR
    return subprocess.run([sys.executable, *args], cwd=cwd, env=env,
                          capture_output=True, text=True, timeout=60)
```

**4. Test the default with the override removed.** An environment override is another place a path bug can hide: a test that sets it no longer exercises the default. The companion test deletes the override, moves to a different directory, and checks the default where the README says the secrets live (`tests/test_auth.py:307-316`):

```python
def test_the_default_instance_folder_is_src_instance(tmp_path, monkeypatch):
    monkeypatch.delenv("PRIMS_INSTANCE_PATH", raising=False)
    monkeypatch.chdir(tmp_path)

    fresh = runpy.run_path(auth.__file__, run_name="prims_auth_probe")

    assert fresh["DEFAULT_INSTANCE_PATH"] == os.path.join(SRC_DIR, "instance")
```

It uses `run_path` to evaluate the module again, because the `auth` already imported in the test process computed its constant with conftest's override in place.

**5. Set test-isolation overrides before the first import.** If a module reads a path or a file at import, patching the attribute afterwards comes too late. The real file has already been read by then, as happened with `src/instance/secrets.json` here. Set the environment variable at the top of `conftest.py` (`tests/conftest.py:21-27`), unconditionally, and remove the temp folder on exit.

**6. Follow the README literally once, from a directory it doesn't mention.** A path mismatch between two commands shows up only when both commands actually run as programs. Run the documented steps as written. Then run them again from a different working directory, and try the revocation command too, not just the setup and the start.

## Related Issues

- [Phone capture plan](../../plans/2026-09-20-001-feat-phone-capture-logging-plan.md) -- the plan that introduced `src/auth.py`, the secrets file and the two-command first run (F4, KTD4, U1, U9). KTD4 says the instance path "resolves to src/instance/". That is true on this branch only because `auth.DEFAULT_INSTANCE_PATH` pins it there; Flask's own default does not, for a script. The plan's assumption that `prims.db` and the upload folder resolve against the working directory is still accurate, and Prevention #2 above shows the upload folder is where that assumption now bites.
- [Enabling PRAGMA foreign_keys turned three working JSON routes into 500s](../database-issues/foreign-key-enforcement-integrityerror-500s.md) -- a different mechanism with the same lesson: a green suite that structurally never ran the path that broke.
- [Residual review findings](../../residual-review-findings/feat-prims-web-interface.md) -- its "Known test gaps" section records that `tests/conftest.py` sets `db.DB_PATH` directly (`tests/conftest.py:82`, `:105`) instead of through `PRIMS_DB`. That is the same shape of gap this fix closed for `PRIMS_INSTANCE_PATH`, and it is still open.
- [README, "Using it from your phone"](../../../README.md#using-it-from-your-phone) -- the two-command first run that exposed the bug. It is accurate after the fix.
- No GitHub issues exist for this.

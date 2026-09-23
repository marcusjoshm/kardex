from flask import (
    Flask, request, jsonify, send_from_directory, render_template,
    redirect, url_for, flash, abort, session,
)
import hmac
import os
import secrets
import socket
import time
from datetime import timedelta
from werkzeug.exceptions import HTTPException, SecurityError
from werkzeug.utils import secure_filename

import auth
import db

app = Flask(__name__)


def _load_signing_key():
    """The key that signs the session cookie, preferring the stored one.

    A generated key does not outlive the process, so every restart would
    invalidate the phone's session and ask for the password again -- which is
    the failure R4 exists to prevent. The stored key (U1) is therefore the
    first choice. Falling back to a generated one keeps a fresh checkout
    runnable on loopback; refusing to start unconfigured is the entry point's
    job (U5), because the bind host is not known here at import time.
    """
    stored = auth.load_secrets(app.instance_path)
    if stored and stored.get('secret_key'):
        return stored['secret_key']
    return os.environ.get('PRIMS_SECRET_KEY') or secrets.token_hex(32)


app.config['SECRET_KEY'] = _load_signing_key()

# Configure upload folder
UPLOAD_FOLDER = 'uploads'
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

# Unset by default, and /upload is a live route, so an arbitrarily large body
# would be read into memory. Set here, alongside the network bind, rather than
# later: the gap between the two is exactly when an unbounded body can reach a
# reachable endpoint. Werkzeug refuses with 413 before reading the body.
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024

# Allowed extensions
ALLOWED_EXTENSIONS = {'txt', 'pdf', 'png', 'jpg', 'jpeg', 'gif'}

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


# ---------------------------------------------------------------------------
# CSRF protection for the browser-facing forms
# ---------------------------------------------------------------------------

# Only the page routes that change state. The JSON API (/inventory,
# /categories, /locations, /upload, /files/...) is deliberately absent: it is a
# scriptable local API driven by curl with no cookies and no session, so a
# token requirement would break it (KTD5, tests/test_crud_operations.sh). That
# exemption is now enforced rather than assumed -- the sign-in gate below only
# lets those routes answer a caller on the machine itself.
CSRF_PROTECTED_ENDPOINTS = frozenset(
    {'new_item', 'edit_item', 'remove_item', 'sign_in', 'sign_out'}
)


def csrf_token():
    """The session's CSRF token, minted lazily on first use.

    The session now carries a sign-in, so it is worth stealing in a way it was
    not before: a forged cross-site POST rides the owner's cookie. A secret
    that only a page served by this app can read is what stands in the way.
    `SESSION_COOKIE_SAMESITE` narrows the same hole from the other side.
    """
    token = session.get('csrf_token')
    if not token:
        token = secrets.token_urlsafe(32)
        session['csrf_token'] = token
    return token


# Every form template emits the hidden field with {{ csrf_token() }}.
app.jinja_env.globals['csrf_token'] = csrf_token


# ---------------------------------------------------------------------------
# Sign-in gate (U2)
# ---------------------------------------------------------------------------

# Host validation closes DNS rebinding. Without it any web page can point a
# hostname at this machine, reach the app same-origin through the browser
# running here, and sail straight through the loopback exemption below -- so
# the exemption is only safe in this list's presence.
#
# Two sharp edges, both verified against the pinned Werkzeug: the comparison is
# case-sensitive and is not folded, while browsers lowercase the Host header,
# so gethostname() must be lowered or every request from the phone 400s with no
# visible cause. And never write a leading dot -- '.local' becomes a suffix
# match over every mDNS name on the network, which voids the protection.
app.config['TRUSTED_HOSTS'] = [
    socket.gethostname().lower(),
    '127.0.0.1',
    'localhost',
    '[::1]',
]

# The password is typed about once per device, and a long session that slides
# forward on each visit is what keeps that promise (R4). Flask validates this
# against the cookie's signature timestamp, so it is a hard cap, not a hint.
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=365)

# Flask leaves SameSite unset. It is load-bearing here: the JSON API is exempt
# from the CSRF token, so once a session authenticates it, a cross-site POST
# riding the owner's cookie would write. Lax withholds the cookie on cross-site
# writes. Not Strict -- that also withholds it when the owner follows a bookmark
# from another app, which reads as a spurious sign-out.
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_HTTPONLY'] = True

# Deliberately False. Over plain HTTP a Secure cookie is stored and never sent,
# so every request looks signed-out and sign-in silently never completes. There
# is no TLS on the LAN by decision; flip this only if that changes.
app.config['SESSION_COOKIE_SECURE'] = False

# Reachable without signing in. 'static' must be here or the sign-in page
# renders unstyled. Everything not named is private -- the opposite polarity to
# the CSRF allowlist above, and deliberately so: a route added later is closed
# until someone opens it, rather than open until someone remembers to close it.
PUBLIC_ENDPOINTS = frozenset({'sign_in', 'static'})

# A dual-stack socket reports the IPv4-mapped form, so all three mean loopback.
LOOPBACK_ADDRESSES = frozenset({'127.0.0.1', '::1', '::ffff:127.0.0.1'})

# Bounds guessing without locking the owner out, and keeps /login from being a
# pre-authentication amplifier: the hash costs tens of milliseconds and tens of
# megabytes, so a few concurrent posts would starve the capture screen.
SIGN_IN_MIN_INTERVAL_SECONDS = 2
_sign_in_last_attempt = {}


def reset_sign_in_throttle():
    """Forget every recorded attempt. Used by tests and after a sign-in."""
    _sign_in_last_attempt.clear()


def _request_is_from_this_machine():
    """True when the caller is on the machine itself.

    Read `request.remote_addr` and nothing else. `request.access_route` hands
    back the caller's own X-Forwarded-For header verbatim, so trusting it would
    give the exemption to anyone who sends one; Waitress is started without
    `trusted_proxy` for the same reason.
    """
    return request.remote_addr in LOOPBACK_ADDRESSES


def _session_is_signed_in():
    """True when this session is signed in and its epoch is still current.

    The epoch is how a lost phone is revoked: a signed cookie cannot be taken
    back server-side, so bumping the stored epoch retires every session at once
    without rotating the signing key and logging the laptop out too.
    """
    if not session.get('signed_in'):
        return False
    stored = auth.load_secrets(app.instance_path)
    if not stored:
        return False
    return session.get('session_epoch') == stored.get('session_epoch')


@app.before_request
def require_login():
    """Deny by default; only the exemptions above get through.

    Registered before the CSRF hook (KTD2) so a lapsed session meets the
    sign-in page rather than a bare 400 it cannot act on.

    Returning None when `request.endpoint` is None is load-bearing, not
    laziness. Flask catches the untrusted-host SecurityError while building the
    URL adapter and defers it to dispatch, which runs *after* every
    before_request handler. Answering here with our own 404 would short-circuit
    dispatch, silently delete the rebinding protection, and still pass every
    obvious test -- because a 404 reads as a refusal too.
    """
    if request.endpoint is None:
        return None
    if request.endpoint in PUBLIC_ENDPOINTS:
        return None
    if _request_is_from_this_machine():
        return None
    if _session_is_signed_in():
        return None
    return redirect(url_for('sign_in'))


def _throttle_wait_remaining():
    """Seconds the caller must wait, or 0 when they may try now."""
    last = _sign_in_last_attempt.get(request.remote_addr)
    if last is None:
        return 0
    elapsed = time.monotonic() - last
    return max(0, int(round(SIGN_IN_MIN_INTERVAL_SECONDS - elapsed)))


def _render_sign_in(error=None, status=200):
    return render_template('login.html', error=error), status


@app.route('/login', methods=['GET', 'POST'])
def sign_in():
    """The only way in from the network."""
    stored = auth.load_secrets(app.instance_path)
    if stored is None:
        # Nothing to check against. Bound beyond loopback the entry point
        # refuses to start in this state (U5); on loopback it just means a
        # fresh checkout nobody has configured yet.
        return _render_sign_in(
            f'No password is set yet. Run `{auth.SETUP_COMMAND}` at the machine.',
            503,
        )
    if request.method == 'GET':
        return _render_sign_in()

    wait = _throttle_wait_remaining()
    if wait:
        # Before the hash, not after -- the cost is the thing being bounded.
        return _render_sign_in(
            f'Too many attempts. Wait {wait} seconds, then try again.', 429
        )
    _sign_in_last_attempt[request.remote_addr] = time.monotonic()

    if not auth.check_password(stored, request.form.get('password', '')):
        # Address and outcome, never the submitted password.
        app.logger.warning('Failed PRIMS sign-in from %s', request.remote_addr)
        return _render_sign_in('Wrong password.', 401)

    # Clear before setting the flag, to discard anything an unauthenticated
    # caller planted in the session. (Not session fixation -- Flask's signed
    # cookie has no server-side identifier to fix, and nobody can mint one
    # without the key. Recording the wrong reason invites deleting the line.)
    session.clear()
    session['signed_in'] = True
    session['session_epoch'] = stored.get('session_epoch', 1)
    session.permanent = True
    # Don't make the owner wait out the throttle they just cleared honestly.
    reset_sign_in_throttle()
    return redirect(url_for('home'))


@app.route('/logout', methods=['POST'])
def sign_out():
    """Sign this device out. Other devices keep their sessions (R12).

    POST only, and in CSRF_PROTECTED_ENDPOINTS, so an <img src="/logout"> on a
    page the owner visits cannot end their session.
    """
    session.clear()
    return redirect(url_for('sign_in'))


@app.after_request
def add_security_headers(response):
    """Two headers with no downside for a single-origin app.

    nosniff stops a browser guessing an uploaded file's type and running it;
    SAMEORIGIN stops another site framing these pages to trick a tap.
    """
    response.headers.setdefault('X-Content-Type-Options', 'nosniff')
    response.headers.setdefault('X-Frame-Options', 'SAMEORIGIN')
    return response


def _render_error_page(code, heading, description):
    return render_template(
        'error.html', code=code, heading=heading, description=description
    ), code


@app.errorhandler(HTTPException)
def render_http_error(exc):
    """Every HTTP error gets a PRIMS page with a way back (R13).

    One handler rather than a list of statuses, because the list is always
    wrong: 405 is reachable from a bookmarked delete URL, 415 from a
    form-encoded POST to a JSON route, and 413 once a body limit exists. Each
    was a bare Werkzeug page with no header and no link home before this.

    An untrusted Host is the one case the shell cannot serve: the failure
    happened while building the URL adapter, so `url_for` -- which every page
    here uses -- has nothing to build against. Answer it plainly instead of
    raising a second error out of the error handler.
    """
    if isinstance(exc, SecurityError):
        return exc

    return _render_error_page(exc.code, exc.name, exc.description)


@app.errorhandler(Exception)
def render_unexpected_error(exc):
    """An unhandled exception, with the traceback kept out of the response.

    Debug mode would serve the Werkzeug console here, which is remote code
    execution for anyone who can reach the port -- the reason R2 exists. The
    traceback goes to the log, where the owner can find it, and the visitor
    gets a page rather than a wall of frames.

    Registering this handler overrides Flask's own re-raise in testing, which
    would quietly blind every test that asserts an exception escapes a route.
    Honour `PROPAGATE_EXCEPTIONS` first so those keep working.
    """
    # Flask ships this key set to None, so a .get() default never applies --
    # reproduce Flask's own resolution rather than inventing one.
    propagate = app.config['PROPAGATE_EXCEPTIONS']
    if propagate is None:
        propagate = app.testing or app.debug
    if propagate:
        raise exc

    app.logger.exception('Unhandled error serving %s', request.path)
    return _render_error_page(
        500,
        'Something went wrong',
        'The error has been logged. Nothing was changed.',
    )


@app.before_request
def require_csrf_token():
    """Reject state-changing page POSTs that do not carry the session token."""
    if request.method != 'POST' or request.endpoint not in CSRF_PROTECTED_ENDPOINTS:
        return None
    expected = session.get('csrf_token')
    submitted = request.form.get('csrf_token', '')
    if not expected or not hmac.compare_digest(str(expected), submitted):
        abort(400, description='Invalid or missing CSRF token. Reload the page and try again.')
    return None


# ---------------------------------------------------------------------------
# Web pages
# ---------------------------------------------------------------------------

def _with_item_counts(rows):
    """Pair each category row with its subtree-wide item count, for tile lists."""
    return [{'row': row, 'count': db.category_item_count(row['id'])} for row in rows]


@app.route('/')
def home():
    """Top-level collections as tiles, each with a subtree-wide item count (R6).

    Items saved with no category belong to no collection, so they get a link of
    their own -- shown only when there are some, so the page stays just the
    tiles for someone who never saves one.
    """
    return render_template(
        'home.html',
        collections=_with_item_counts(db.get_top_level_categories()),
        uncategorised_count=db.uncategorised_item_count(),
    )


@app.route('/uncategorised')
def uncategorised_page():
    """Items with no category, which appear on no category page (KD2).

    The category picker allows "no category", so these items are real; without
    this list they would be reachable only by search or a direct URL.
    """
    return render_template('uncategorised.html', items=db.get_uncategorised_items())


@app.route('/category/<int:category_id>')
def category_page(category_id):
    """A category's sub-categories and its directly-attached items (R7/AE1).

    get_ancestors() ends with this category, so it doubles as the existence
    check -- an unknown id yields an empty chain.
    """
    ancestors = db.get_ancestors(category_id)
    if not ancestors:
        abort(404)
    return render_template(
        'category.html',
        category=ancestors[-1],
        ancestors=ancestors,
        subcategories=_with_item_counts(db.get_subcategories(category_id)),
        items=db.get_items_in_category(category_id),
    )


@app.route('/search')
def search():
    """One box over every collection, matching item name and notes (R8/F2).

    A plain GET form submission (KTD1): no q, or a blank one, renders a prompt
    rather than an error. Each hit carries its category path so the answer to
    "do I already own this?" names the collection it lives in.
    """
    query = request.args.get('q', '').strip()
    results = []
    for row in db.search_items(query):
        ancestors = db.get_ancestors(row['category_id']) if row['category_id'] else []
        results.append({
            'row': row,
            'path': ' / '.join(crumb['name'] for crumb in ancestors),
        })
    return render_template('search.html', query=query, results=results)


@app.route('/item/<int:item_id>')
def item_page(item_id):
    """One item: name, quantity, notes and location."""
    item = db.get_item(item_id)
    if item is None:
        abort(404)
    ancestors = db.get_ancestors(item['category_id']) if item['category_id'] else []
    return render_template(
        'item.html',
        item=item,
        category=ancestors[-1] if ancestors else None,
        ancestors=ancestors,
        location=db.get_location(item['location_id']),
    )


# ---------------------------------------------------------------------------
# Item add / edit / delete (U5)
# ---------------------------------------------------------------------------

BLANK_ITEM_FORM = {
    'name': '',
    'category_id': '',
    'new_category': '',
    'quantity': '',
    'notes': '',
    'location': '',
}


def _category_choices():
    """Every category, depth-first, each label indented by its depth.

    Per R3/KD2 an item may hang off any level of the tree, so the picker
    offers every category rather than only the leaves.
    """
    children = {}
    for row in db.get_all_categories():
        children.setdefault(row['parent_id'], []).append(row)

    choices = []

    def walk(parent_id, depth):
        for row in children.get(parent_id, []):
            choices.append({
                'id': row['id'],
                'label': '   ' * depth + row['name'],
            })
            walk(row['id'], depth + 1)

    walk(None, 0)
    return choices


def _submitted_item_form():
    """The raw, stripped form values, kept so a rejected form can be re-rendered.

    Quantity arrives in two fields (KTD10): a select for the ordinary case and a
    text field for anything larger, which wins when filled. They are merged
    back into one `quantity` here, so everything downstream -- validation, the
    save, the re-render -- sees exactly what the old single field sent (R11).
    """
    values = {key: request.form.get(key, '').strip() for key in BLANK_ITEM_FORM}
    larger = request.form.get('quantity_other', '').strip()
    if larger:
        values['quantity'] = larger
    return values


# The quantity select runs 0 to this; anything larger goes in the text field.
# Household quantities are small, so the list covers the ordinary capture and
# nothing needs the keyboard -- the iOS number pad has no microphone key.
QUANTITY_LIST_MAX = 24
app.jinja_env.globals['QUANTITY_LIST_MAX'] = QUANTITY_LIST_MAX


# SQLite stores an INTEGER in 64 bits, so a larger number cannot be written at
# all: the driver raises OverflowError from inside the INSERT. Rejecting it up
# front turns that 500 into an ordinary form error.
MAX_QUANTITY = 2 ** 63 - 1


def _resolve_item_form(values):
    """Turn submitted strings into db arguments, or return a message to show.

    Returns ``(fields, created_category_id, error)``: either ``fields`` or
    ``error`` is set, never both. Every rejectable value is checked before the
    inline category is created, so a form that fails validation leaves no
    half-made category behind. Validation is not the only way to lose the item,
    though, so when a category *is* created here ``created_category_id`` names
    it -- and only it, never a category that was merely reused -- for
    _save_item_form to remove if the item write itself then fails. A category
    id that no longer exists is caught here rather than surfacing as the
    sqlite3.IntegrityError that PRAGMA foreign_keys would otherwise raise.
    """
    if not values['name']:
        return None, None, 'Item name is required.'

    try:
        quantity = int(values['quantity'] or 0)
    except ValueError:
        return None, None, 'Quantity must be a whole number.'
    if quantity < 0:
        return None, None, 'Quantity cannot be negative.'
    if quantity > MAX_QUANTITY:
        return None, None, f'Quantity cannot be larger than {MAX_QUANTITY}.'

    parent_id = None
    if values['category_id']:
        try:
            parent_id = int(values['category_id'])
        except ValueError:
            return None, None, 'Choose a category from the list.'
        if db.get_category(parent_id) is None:
            return None, None, 'That category no longer exists — choose another.'

    category_id = parent_id
    created_category_id = None
    if values['new_category']:
        # Get-or-create, like the Location field beside it: a name already in
        # use under this parent means that category, not a rejected save. Only
        # a category this submission actually made is ours to undo, so the
        # lookup happens first -- an existing one must survive a failed write.
        existing = db.get_category_by_name(values['new_category'], parent_id)
        try:
            category_id = db.get_or_create_category(values['new_category'], parent_id)
        except ValueError as exc:
            return None, None, str(exc)
        if existing is None:
            created_category_id = category_id

    return {
        'name': values['name'],
        'category_id': category_id,
        'location_id': db.get_or_create_location(values['location']),
        'quantity': quantity,
        'notes': values['notes'],
    }, created_category_id, None


def _save_item_form(values, save):
    """Resolve the form and write the item, as one all-or-nothing step.

    ``save`` receives the resolved fields and does the actual db write. If it
    raises, a category this same submission created inline is removed again:
    the item is not saved, so the category it was invented for must not
    outlive it. Returns ``(result_of_save, error)``.
    """
    fields, created_category_id, error = _resolve_item_form(values)
    if error:
        return None, error
    try:
        return save(fields), None
    except Exception:
        if created_category_id is not None:
            db.delete_category(created_category_id)
        raise


def _render_item_form(values, heading, action, submit_label, status=200, error=None):
    # A rejection is passed in and rendered as a field error rather than
    # flashed: the flash bar is styled green for everything, so a refusal shown
    # there reads as "saved" and the owner walks away with nothing written.
    return render_template(
        'item_form.html',
        values=values,
        categories=_category_choices(),
        heading=heading,
        action=action,
        submit_label=submit_label,
        error=error,
    ), status


@app.route('/item/new', methods=['GET', 'POST'])
def new_item():
    """Add an item, picking a category or creating one inline (R10/AE4, F3)."""
    if request.method == 'POST':
        values = _submitted_item_form()
        item_id, error = _save_item_form(values, lambda fields: db.create_item(**fields))
        if error:
            return _render_item_form(
                values, 'Add item', url_for('new_item'), 'Save item',
                status=400, error=error,
            )
        flash(f"Added {values['name']}.")
        return redirect(url_for('item_page', item_id=item_id))

    values = dict(BLANK_ITEM_FORM)
    # Adding from a category page starts with that category already chosen.
    values['category_id'] = request.args.get('category_id', '')
    return _render_item_form(values, 'Add item', url_for('new_item'), 'Save item')


@app.route('/item/<int:item_id>/edit', methods=['GET', 'POST'])
def edit_item(item_id):
    """Edit an item, including its quantity (R11/AE3)."""
    item = db.get_item(item_id)
    if item is None:
        abort(404)
    action = url_for('edit_item', item_id=item_id)

    if request.method == 'POST':
        values = _submitted_item_form()
        _, error = _save_item_form(
            values, lambda fields: db.update_item(item_id, **fields)
        )
        if error:
            return _render_item_form(
                values, 'Edit item', action, 'Save changes',
                status=400, error=error,
            )
        flash(f"Saved {values['name']}.")
        return redirect(url_for('item_page', item_id=item_id))

    location = db.get_location(item['location_id'])
    values = dict(
        BLANK_ITEM_FORM,
        name=item['name'],
        category_id='' if item['category_id'] is None else str(item['category_id']),
        quantity='' if item['quantity'] is None else str(item['quantity']),
        notes=item['notes'] or '',
        location=location['name'] if location else '',
    )
    return _render_item_form(values, 'Edit item', action, 'Save changes')


@app.route('/item/<int:item_id>/delete', methods=['POST'])
def remove_item(item_id):
    """Delete an item (R12). POST only; the page guards it with a confirm()."""
    item = db.get_item(item_id)
    if item is None:
        abort(404)
    db.delete_item(item_id)
    flash(f"Deleted {item['name']}.")
    if item['category_id'] and db.get_category(item['category_id']):
        return redirect(url_for('category_page', category_id=item['category_id']))
    return redirect(url_for('home'))


# ---------------------------------------------------------------------------
# JSON API (existing endpoints, now backed by the shared data layer in db.py)
# ---------------------------------------------------------------------------

@app.route('/inventory', methods=['POST'])
def create_inventory_item():
    data = request.get_json() or {}
    try:
        item_id = db.create_item(
            name=data.get('name'),
            category_id=data.get('category_id'),
            location_id=data.get('location_id'),
            quantity=data.get('quantity', 0),
            notes=data.get('notes', ''),
        )
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400
    return jsonify({'message': 'Inventory item created successfully', 'id': item_id}), 201


@app.route('/inventory', methods=['GET'])
def get_inventory_items():
    return jsonify([dict(row) for row in db.get_all_items()])


@app.route('/inventory/<int:item_id>', methods=['PUT'])
def update_inventory_item(item_id):
    data = request.get_json() or {}
    existing = db.get_item(item_id)
    if existing is None:
        return jsonify({'error': 'Inventory item not found'}), 404
    try:
        db.update_item(
            item_id,
            name=data.get('name', existing['name']),
            category_id=data.get('category_id', existing['category_id']),
            location_id=data.get('location_id', existing['location_id']),
            quantity=data.get('quantity', existing['quantity']),
            notes=data.get('notes', existing['notes']),
        )
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400
    return jsonify({'message': 'Inventory item updated successfully'})


@app.route('/inventory/<int:item_id>', methods=['DELETE'])
def delete_inventory_item(item_id):
    db.delete_item(item_id)
    return jsonify({'message': 'Inventory item deleted successfully'})


@app.route('/categories', methods=['POST'])
def create_category():
    data = request.get_json() or {}
    try:
        category_id = db.create_category(data.get('name'), data.get('parent_id'))
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400
    return jsonify({'message': 'Category created successfully', 'id': category_id}), 201


@app.route('/categories', methods=['GET'])
def get_categories():
    return jsonify([dict(row) for row in db.get_all_categories()])


@app.route('/categories/<int:category_id>', methods=['PUT'])
def update_category(category_id):
    data = request.get_json() or {}
    try:
        db.update_category(category_id, data.get('name'))
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400
    return jsonify({'message': 'Category updated successfully'})


@app.route('/categories/<int:category_id>', methods=['DELETE'])
def delete_category(category_id):
    try:
        db.delete_category(category_id)
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400
    return jsonify({'message': 'Category deleted successfully'})


@app.route('/locations', methods=['POST'])
def create_location():
    data = request.get_json() or {}
    location_id = db.get_or_create_location(data.get('name'))
    if location_id is None:
        return jsonify({'error': 'Location name is required'}), 400
    return jsonify({'message': 'Location created successfully', 'id': location_id}), 201


@app.route('/locations', methods=['GET'])
def get_locations():
    return jsonify([dict(row) for row in db.get_locations()])


@app.route('/locations/<int:location_id>', methods=['PUT'])
def update_location(location_id):
    data = request.get_json() or {}
    try:
        db.update_location(location_id, data.get('name'))
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400
    return jsonify({'message': 'Location updated successfully'})


@app.route('/locations/<int:location_id>', methods=['DELETE'])
def delete_location(location_id):
    try:
        db.delete_location(location_id)
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400
    return jsonify({'message': 'Location deleted successfully'})


# ---------------------------------------------------------------------------
# File management (unchanged)
# ---------------------------------------------------------------------------

@app.route('/upload', methods=['POST'])
def upload_file():
    if 'file' not in request.files:
        return jsonify({'error': 'No file part'}), 400
    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': 'No selected file'}), 400
    if file and allowed_file(file.filename):
        filename = secure_filename(file.filename)
        file.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))
        return jsonify({'message': 'File uploaded successfully', 'filename': filename}), 201
    else:
        return jsonify({'error': 'File type not allowed'}), 400


@app.route('/files', methods=['GET'])
def list_files():
    files = os.listdir(app.config['UPLOAD_FOLDER'])
    return jsonify(files)


@app.route('/files/<filename>', methods=['GET'])
def download_file(filename):
    # As an attachment, so an uploaded HTML file cannot run as script on this
    # origin -- which now carries a signed-in session worth stealing.
    return send_from_directory(
        app.config['UPLOAD_FOLDER'], filename, as_attachment=True
    )


@app.route('/files/<filename>', methods=['DELETE'])
def delete_file(filename):
    # secure_filename, like upload_file already uses. Before this the only thing
    # keeping '../' out was the router, and the WSGI front-end in front of that
    # router just changed (U5). Called with the router bypassed, the old version
    # deleted a file outside the uploads folder.
    safe_name = secure_filename(filename)
    if not safe_name or safe_name != filename:
        return jsonify({'error': 'Invalid filename'}), 400
    file_path = os.path.join(app.config['UPLOAD_FOLDER'], safe_name)
    try:
        os.remove(file_path)
    except FileNotFoundError:
        return jsonify({'error': 'File not found'}), 404
    return jsonify({'message': 'File deleted successfully'})


# ---------------------------------------------------------------------------
# Entry point (U5)
# ---------------------------------------------------------------------------

DEFAULT_HOST = '127.0.0.1'
DEFAULT_PORT = 5000
LOOPBACK_HOSTS = frozenset({'127.0.0.1', 'localhost', '::1'})


def _serve_with_waitress(host, port):
    """Production WSGI server: one process, no debugger, no dev reloader.

    Flask's own documentation is explicit that the development server is not
    for this -- "production" includes serving "privately to a single user".
    `trusted_proxy` is deliberately left unset: there is no proxy, and setting
    it would make X-Forwarded-For authoritative and hand the loopback exemption
    to anyone who sends the header.
    """
    import waitress

    waitress.serve(app, host=host, port=port)


def main(argv=None, serve=None):
    """Start PRIMS. Refuses to reach the network with nothing guarding it.

    Run from the repository root::

        python src/app.py                    # this machine only, dev server
        python src/app.py --host 0.0.0.0     # the home network, via Waitress

    The refusal is the reason this is a function rather than a bare block:
    binding beyond loopback with no password configured would put the whole
    inventory, readable and writable, on the network. It is decided here and
    not at import, because the bind host is only known once arguments exist.
    """
    import argparse
    import sys

    parser = argparse.ArgumentParser(description='Run PRIMS.')
    parser.add_argument(
        '--host',
        default=DEFAULT_HOST,
        help='address to listen on; 0.0.0.0 reaches the home network '
             f'(default: {DEFAULT_HOST}, this machine only)',
    )
    parser.add_argument('--port', type=int, default=DEFAULT_PORT)
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    on_network = args.host not in LOOPBACK_HOSTS

    try:
        stored = auth.load_secrets(app.instance_path)
    except auth.SecretsError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    if on_network and stored is None:
        print(
            f'Refusing to listen on {args.host}: no password is set, so the '
            'whole inventory would be readable and writable by anyone on the '
            f'network.\nRun `{auth.SETUP_COMMAND}` first.',
            file=sys.stderr,
        )
        return 2

    db.init_db()

    if on_network:
        # Never the debugger on a reachable socket: it is remote code
        # execution for anyone who can reach the port (R2).
        app.debug = False

    if serve is not None:
        # Tests inject this; everything above is the real decision path.
        serve(host=args.host, port=args.port)
        return 0

    if on_network:
        print(f'PRIMS on http://{socket.gethostname().lower()}:{args.port}')
        _serve_with_waitress(args.host, args.port)
    else:
        # Loopback-only keeps the owner's edit-reload loop.
        app.run(host=args.host, port=args.port, debug=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

from flask import (
    Flask, request, jsonify, send_from_directory, render_template,
    redirect, url_for, flash, abort, session,
)
import hmac
import os
import secrets
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
# token requirement would break it (KTD5, tests/test_crud_operations.sh).
CSRF_PROTECTED_ENDPOINTS = frozenset({'new_item', 'edit_item', 'remove_item'})


def csrf_token():
    """The session's CSRF token, minted lazily on first use.

    The app has no login, so there is no session to steal -- which is exactly
    why a forged cross-site POST would otherwise work. A secret that only a
    page served by this app can read is the thing standing in the way.
    """
    token = session.get('csrf_token')
    if not token:
        token = secrets.token_urlsafe(32)
        session['csrf_token'] = token
    return token


# Every form template emits the hidden field with {{ csrf_token() }}.
app.jinja_env.globals['csrf_token'] = csrf_token


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
    """The raw, stripped form values, kept so a rejected form can be re-rendered."""
    return {key: request.form.get(key, '').strip() for key in BLANK_ITEM_FORM}


# SQLite stores an INTEGER in 64 bits, so a larger number cannot be written at
# all: the driver raises OverflowError from inside the INSERT. Rejecting it up
# front turns that 500 into an ordinary form error.
MAX_QUANTITY = 2 ** 63 - 1
app.jinja_env.globals['MAX_QUANTITY'] = MAX_QUANTITY


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


def _render_item_form(values, heading, action, submit_label, status=200):
    return render_template(
        'item_form.html',
        values=values,
        categories=_category_choices(),
        heading=heading,
        action=action,
        submit_label=submit_label,
    ), status


@app.route('/item/new', methods=['GET', 'POST'])
def new_item():
    """Add an item, picking a category or creating one inline (R10/AE4, F3)."""
    if request.method == 'POST':
        values = _submitted_item_form()
        item_id, error = _save_item_form(values, lambda fields: db.create_item(**fields))
        if error:
            flash(error)
            return _render_item_form(
                values, 'Add item', url_for('new_item'), 'Save item', status=400
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
            flash(error)
            return _render_item_form(
                values, 'Edit item', action, 'Save changes', status=400
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
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)


@app.route('/files/<filename>', methods=['DELETE'])
def delete_file(filename):
    file_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    if os.path.exists(file_path):
        os.remove(file_path)
        return jsonify({'message': 'File deleted successfully'})
    else:
        return jsonify({'error': 'File not found'}), 404


if __name__ == '__main__':
    db.init_db()
    app.run(debug=True)

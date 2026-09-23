# PRIMS
A "Personal Resource and Information Management System" for organizing my life.

## Overview
PRIMS (Personal Resource and Information Management System) is designed to help organize personal inventory, such as kitchen supplies, clothes, and electronics, with specific locations for easy retrieval.

## Features Implemented
- Web interface for browsing, searching, and managing inventory
  - Home screen of top-level collections, each with a subtree-wide item count
  - Category pages showing sub-categories and directly-attached items together
  - One search box matching item names *and* notes across every collection
  - Add, edit, and delete items, creating a category inline when needed
  - Mobile-friendly: it works at phone width, for checking inventory while out
  - Reachable from your phone on the home network, behind a password you type
    once per device — then log items by dictating into the add form
- Categories nest to any depth, and an item can attach at any level
- SQLite database behind a single data-access layer (`src/db.py`)
- JSON API for inventory items, categories, and locations
- File upload/download/delete endpoints
- Seed script with realistic sample data
- 221 automated tests (`pytest`), plus the original curl smoke script

## Checklist
- [x] Set up project structure
- [x] Implement basic CRUD operations
- [x] Create test script for CRUD operations
- [x] Add search functionality
- [x] Develop a web interface
- [x] Implement user authentication
- [ ] Deploy the application

Every item records the same three fields — name, quantity, and freeform notes.
A book's author or a record's artist lives in the notes, where search finds it.

## Getting Started
To run the application locally, follow these steps:
1. Clone the repository: `git clone https://github.com/marcusjoshm/PRIMS.git`
2. Navigate to the project directory: `cd PRIMS`
3. Create a virtual environment: `python3 -m venv venv`
4. Activate the virtual environment: `source venv/bin/activate`
5. Install dependencies: `pip install -r requirements.txt`
6. *(Optional)* Load sample data: `python src/seed.py`
7. Run the application: `python src/app.py`, then open http://127.0.0.1:5000

> **`src/seed.py` is destructive.** It drops every table and rebuilds the
> database from the sample inventory. It asks for confirmation first if the
> database already holds items; pass `--force` to skip the prompt. Point it at
> a different file with `PRIMS_DB=/tmp/demo.db python src/seed.py`.

## Using it from your phone

Run these at the machine, from the repository root.

1. **Set a password:** `python src/auth.py`. At least 12 characters. You will
   type it about once per phone, so a long passphrase costs you nothing.
2. **Start it on the home network:** `python src/app.py --host 0.0.0.0`. It
   prints the address to use, like `http://eniac.local:5000`.
3. **Allow incoming connections** when macOS asks. It only asks the first time.
4. **On the phone,** open that address and sign in once. Bookmark it — the
   `.local` name keeps working when the network hands the machine a new IP,
   which a bare IP address would not.

To log an item: tap **Add item**, tap into the name field, and use the
microphone key on the keyboard. Pick the quantity from the list; nothing
needs the keyboard unless the count is above 24.

Safari will not offer to fill in a password on a plain-HTTP address, so the
sign-in is built to last instead: a year, extended every time you use it. If
you go a month without opening PRIMS at all, Safari clears its data and you
sign in again.

> **PRIMS should ask for your password once.** If it ever asks again
> unexpectedly, don't type it — check at the machine first. On a home network
> anything can answer to the `.local` name, and with no certificate there is no
> padlock to warn you. An unexpected prompt is the only signal you get.

With no `--host`, PRIMS listens on this machine only, as before. It refuses to
start on the network until a password is set.

### Signing out, and a lost phone

- **Sign out** in the page header signs out that device only.
- **A lost phone:** run `python src/auth.py --sign-out-everywhere`. That signs
  out every device, and you sign in again on the ones you still have.
- **Changing the password does not sign a lost phone out.** It deliberately
  keeps existing sign-ins, so you can change it without being thrown out.
  Use `--sign-out-everywhere` for that.

The password hash and the key that signs sign-ins live in
`src/instance/secrets.json`, which git ignores. Anything that deletes ignored
files — `git clean -xdf`, for instance — deletes it, which signs out every
device and stops PRIMS starting on the network until you run
`python src/auth.py` again. Failed sign-ins are logged, with the address they
came from, to the terminal PRIMS is running in.

## Running the tests
```bash
python -m pytest tests/          # full unit, page and regression suite
bash tests/test_crud_operations.sh   # curl smoke test, needs the app running
```

## Web pages
| Path | Purpose |
| --- | --- |
| `/` | Top-level collections, with an entry point to add an item |
| `/category/<id>` | A category's sub-categories and its own items |
| `/uncategorised` | Items saved with no category (linked from `/` when any exist) |
| `/item/<id>` | One item's name, quantity, location, category, and notes |
| `/search?q=` | Items matching a term in their name or notes |
| `/item/new`, `/item/<id>/edit`, `/item/<id>/delete` | Add, edit, remove |
| `/login`, `/logout` | Sign in from another device; sign out of this one |

Every page is private to anything but this machine until you sign in. The JSON
API and file endpoints answer requests from this machine only, which is what
keeps `tests/test_crud_operations.sh` working.

## License
This project is licensed under the MIT License.

## File Management
PRIMS includes file management capabilities, allowing users to upload, list, download, and delete files. This feature is useful for managing important documents and configurations.

### Endpoints
- **Upload File**: POST `/upload` - Upload a file to the server.
- **List Files**: GET `/files` - Retrieve a list of all uploaded files.
- **Download File**: GET `/files/<filename>` - Download a specific file.
- **Delete File**: DELETE `/files/<filename>` - Delete a specific file from the server.

These endpoints support various file types, including `txt`, `pdf`, `png`, `jpg`, `jpeg`, and `gif`. Ensure that the files you upload meet the allowed criteria.

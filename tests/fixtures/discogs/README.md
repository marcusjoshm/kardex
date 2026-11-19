# Discogs fixtures

Response bodies in the shape the Discogs API returns them (field names, nesting and types as
in the API documentation and real responses), so the tests never touch the network. The
release ids (91000xx), instance ids and the username are made up; the music is real.

| file | endpoint |
|---|---|
| `identity.json` | `GET /oauth/identity` |
| `search_*.json` | `GET /database/search` (barcode; catno+label; a unique catno; artist+title; none) |
| `release_*.json` | `GET /releases/{id}` |
| `collection_release_*.json` | `GET /users/{u}/collection/releases/{id}` (not there; one instance) |
| `add_to_collection.json` | `POST /users/{u}/collection/folders/1/releases/{id}` (201) |
| `collection_page{1,2}.json` | `GET /users/{u}/collection/folders/0/releases?page=N` |
| `error_*.json` | error bodies (404, 429) |

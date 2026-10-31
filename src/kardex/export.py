"""Plain-text export for backup in git: one JSONL file per collection, stable order."""

import json
import os
from pathlib import Path

from . import db
from .schema import COLLECTIONS

REPO = Path(__file__).resolve().parents[2]


def default_dir() -> Path:
    return Path(os.environ.get("KARDEX_EXPORT") or REPO / "export")


def export(conn, out_dir=None) -> dict[str, int]:
    out = Path(out_dir) if out_dir else default_dir()
    out.mkdir(parents=True, exist_ok=True)
    counts = {}
    for collection in COLLECTIONS:
        items = db.find(conn, collection=collection, sort="id")
        text = "".join(json.dumps(i, sort_keys=True, ensure_ascii=False) + "\n" for i in items)
        (out / f"{collection}.jsonl").write_text(text, encoding="utf-8", newline="\n")
        counts[collection] = len(items)
    return counts

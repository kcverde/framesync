"""TV-independent image preparation, additive sync and per-Frame receipts."""
from __future__ import annotations

import hashlib
import io
import ipaddress
import json
import os
from pathlib import Path
import sqlite3
import threading
from collections.abc import Callable
from contextlib import contextmanager

from PIL import Image, ImageOps

EXTENSIONS = {".jpg", ".jpeg", ".png"}


def validate_host(value: str) -> str:
    try:
        address = ipaddress.IPv4Address(value.strip())
    except ValueError as exc:
        raise ValueError("Enter the TV’s local IPv4 address, for example 192.168.1.50.") from exc
    if not any(address in ipaddress.IPv4Network(network) for network in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")):
        raise ValueError("Use the TV’s private home-network IP address.")
    return str(address)


def photos_in(folder: Path) -> list[Path]:
    if not folder.is_dir():
        raise ValueError("Choose an existing photo folder.")
    return sorted((p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in EXTENSIONS), key=lambda p: p.name.casefold())


def revision(stat):
    return f"{stat.st_mtime_ns}:{stat.st_size}"


def crop_box(size, zoom, pan_x, pan_y):
    width, height = size
    crop_width = min(width, height * 16 / 9) / zoom
    crop_height = crop_width * 9 / 16
    x = max(0, width - crop_width) * pan_x
    y = max(0, height - crop_height) * pan_y
    return x, y, min(width, x + crop_width), min(height, y + crop_height)


def prepare_photo(path: Path, crop=None) -> bytes:
    # Read once so the hash always describes the exact bytes sent to the TV.
    with path.open("rb") as source, Image.open(source) as original:
        image = ImageOps.exif_transpose(original).convert("RGBA")
        if crop and crop[0] == revision(os.fstat(source.fileno())):
            box = crop_box(image.size, *crop[1:])
            image = image.resize((3840, 2160), Image.Resampling.LANCZOS, box=box)
        else:
            image.thumbnail((3840, 2160), Image.Resampling.LANCZOS)
        canvas = Image.new("RGB", (3840, 2160), "black")
        canvas.paste(image, ((3840 - image.width) // 2, (2160 - image.height) // 2), image)
        output = io.BytesIO()
        canvas.save(output, "JPEG", quality=90)
        return output.getvalue()


class Store:
    def __init__(self, root: Path):
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root = root
        with self.connect() as db:
            try:
                db.execute("PRAGMA journal_mode=WAL")
            except sqlite3.OperationalError:
                pass
            db.execute("CREATE TABLE IF NOT EXISTS uploads (tv TEXT, digest TEXT, name TEXT, content_id TEXT, PRIMARY KEY(tv, digest))")

            db.execute("CREATE TABLE IF NOT EXISTS removed (tv TEXT, digest TEXT, PRIMARY KEY(tv, digest))")
            db.execute("CREATE TABLE IF NOT EXISTS upload_paths (tv TEXT, digest TEXT, path TEXT, PRIMARY KEY(tv, digest, path))")
            db.execute("CREATE TABLE IF NOT EXISTS museum_sends (tv TEXT, digest TEXT, work_key TEXT, PRIMARY KEY(tv, digest, work_key))")
            db.execute("CREATE TABLE IF NOT EXISTS crops (path TEXT PRIMARY KEY, revision TEXT, zoom REAL, x REAL, y REAL)")

    def crops(self):
        with self.connect() as db:
            return {path: values for path, *values in db.execute("SELECT path, revision, zoom, x, y FROM crops")}

    def set_crop(self, path, values):
        with self.connect() as db:
            if values is None:
                db.execute("DELETE FROM crops WHERE path=?", (str(path),))
            else:
                db.execute("INSERT OR REPLACE INTO crops VALUES (?, ?, ?, ?, ?)", (str(path), *values))

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.root / "sync.sqlite3", timeout=5.0)
        try:
            db.execute("PRAGMA busy_timeout=5000")
            with db:
                yield db
        finally:
            db.close()

    def records(self, tv: str) -> dict[str, tuple[str, str | None]]:
        with self.connect() as db:
            return {digest: (name, content_id) for digest, name, content_id in db.execute("SELECT digest, name, content_id FROM uploads WHERE tv=?", (tv,))}

    def suppressed(self, tv: str) -> set[str]:
        with self.connect() as db:
            return {digest for digest, in db.execute("SELECT digest FROM removed WHERE tv=?", (tv,))}

    def mark_removing(self, tv: str, content_id: str):
        # Persist intent before the request: a lost reply must never resurrect a removed photo.
        with self.connect() as db:
            rows = db.execute("""INSERT OR IGNORE INTO removed
                                 SELECT tv, digest FROM uploads WHERE tv=? AND content_id=?
                                 RETURNING digest""", (tv, content_id))
            return [digest for digest, in rows]

    def undo_removal_intent(self, tv: str, digests: list[str]):
        with self.connect() as db:
            db.executemany("DELETE FROM removed WHERE tv=? AND digest=?", ((tv, digest) for digest in digests))

    def confirm_removal(self, tv: str, content_id: str):
        with self.connect() as db:
            db.execute("DELETE FROM uploads WHERE tv=? AND content_id=?", (tv, content_id))

    def upload_paths(self, tv: str) -> list[tuple[str, str]]:
        with self.connect() as db:
            return db.execute("""SELECT path, content_id FROM upload_paths JOIN uploads USING(tv, digest)
                                 WHERE tv=? AND content_id IS NOT NULL""", (tv,)).fetchall()

    def record(self, tv: str, digest: str, name: str, content_id: str | None,
               *, path: Path | None = None, resend: bool = False, work_key=None):
        with self.connect() as db:
            if content_id is not None:
                # The Frame may reuse a content ID after removing an older image.
                db.execute("DELETE FROM uploads WHERE tv=? AND content_id=? AND digest<>?", (tv, content_id, digest))
            db.execute("INSERT OR REPLACE INTO uploads VALUES (?, ?, ?, ?)", (tv, digest, name, content_id))
            if content_id is not None:
                if work_key:
                    db.execute("INSERT OR IGNORE INTO museum_sends VALUES (?, ?, ?)", (tv, digest, work_key))
                elif path is not None:
                    db.execute("INSERT OR IGNORE INTO upload_paths VALUES (?, ?, ?)", (tv, digest, str(path.absolute())))
                if resend:
                    db.execute("DELETE FROM removed WHERE tv=? AND digest=?", (tv, digest))

    def clear_uncertain(self, tv: str):
        with self.connect() as db:
            db.execute("DELETE FROM uploads WHERE tv=? AND content_id IS NULL", (tv,))

    def save_settings(self, settings: dict):
        temporary = self.root / "settings.tmp"
        temporary.write_text(json.dumps(settings))
        temporary.replace(self.root / "settings.json")

    def settings(self) -> dict:
        try:
            return json.loads((self.root / "settings.json").read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def token_file(self, tv: str) -> str:
        return str(self.root / f"token-{tv}.txt")

    def load_token(self, tv: str) -> str | None:
        try:
            return Path(self.token_file(tv)).read_text().strip() or None
        except FileNotFoundError:
            return None

    def save_token(self, tv: str, token: str):
        path = Path(self.token_file(tv))
        temporary = path.with_suffix(".tmp")
        descriptor = os.open(temporary, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            stream.write(token)
        temporary.replace(path)


def artwork_list(art) -> list[dict]:
    items = art.available()
    if not isinstance(items, list) or any(not isinstance(item, dict) or not item.get("content_id") for item in items):
        raise RuntimeError("The TV returned an unreadable artwork list.")
    # The same content ID can appear in several categories. Keep the TV's order
    # but prefer My Photos: some Frames reject select_image on the secondary row.
    by_id = {}
    for item in items:
        cid = str(item["content_id"])
        if cid not in by_id or item.get("category_id") == "MY-C0002":
            by_id[cid] = item
    return list(by_id.values())


def content_ids(art) -> set[str]:
    return {str(item["content_id"]) for item in artwork_list(art)}


def sync_folder(art, store: Store, tv: str, folder: Path, cancelled: threading.Event,
                progress: Callable[[int, int, str], None], *, snapshot=None) -> str:
    return sync_files(art, store, tv, photos_in(folder), cancelled, progress, snapshot=snapshot)


def sync_files(art, store: Store, tv: str, paths: list[Path], cancelled: threading.Event,
               progress: Callable[[int, int, str], None], *, resend=False, snapshot=None, crop_overrides=None, work_key=None, confirmed=None) -> str:
    records = store.records(tv)
    if any(content_id is None for _, content_id in records.values()):
        raise RuntimeError("A previous upload was interrupted. Use ‘Resolve interrupted upload’ before syncing again.")
    crops = store.crops() if crop_overrides is None else crop_overrides
    existing = content_ids(art)
    if snapshot is not None:
        snapshot(existing.copy())
    removed = store.suppressed(tv)
    uploaded = skipped = excluded = 0

    def summary(prefix):
        counts = f"{uploaded} added · {skipped} already synced"
        if excluded:
            counts += f" · {excluded} previously removed"
        return f"{prefix} {counts}."

    for index, path in enumerate(paths):
        if cancelled.is_set():
            return summary("Stopped.")
        progress(index, len(paths), f"Preparing {path.name}")
        payload = prepare_photo(path, crops.get(str(path)))
        digest = hashlib.sha256(payload).hexdigest()
        previous = records.get(digest)
        if digest in removed and not resend:
            excluded += 1
        elif previous and previous[1] in existing:
            store.record(tv, digest, previous[0], previous[1], path=path, resend=resend, work_key=work_key)
            if confirmed:
                confirmed(digest, previous[1], True)
            skipped += 1
        else:
            if cancelled.is_set():
                return summary("Stopped.")
            # Journal before sending: a timeout must never cause an automatic duplicate.
            store.record(tv, digest, path.name, None)
            progress(index, len(paths), f"Sending {path.name}")
            try:
                content_id = art.upload(payload, matte="none", portrait_matte="none", file_type="JPEG")
                if not isinstance(content_id, str) or not content_id:
                    raise RuntimeError("The TV did not confirm the upload.")
                existing = content_ids(art)
                if content_id not in existing:
                    raise RuntimeError("The new photo is not visible in the TV’s artwork list yet.")
                store.record(tv, digest, path.name, content_id, path=path, resend=resend, work_key=work_key)
                if snapshot is not None:
                    snapshot(existing.copy())
            except Exception as exc:
                raise RuntimeError(f"Could not confirm {path.name}. Sync stopped to avoid duplicates. Check the TV, then use ‘Resolve interrupted upload’. ({exc})") from exc
            # Mirror the journal's invalidation if firmware recycled a content ID mid-run.
            records = {key: row for key, row in records.items() if row[1] != content_id}
            records[digest] = (path.name, content_id)
            if confirmed:
                confirmed(digest, content_id, False)
            uploaded += 1
        progress(index + 1, len(paths), path.name)
    return summary("Sync complete.")

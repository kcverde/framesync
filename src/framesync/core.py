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

from PIL import Image, ImageCms, ImageOps, __version__ as pillow_version, features

EXTENSIONS = {".jpg", ".jpeg", ".png"}
# Bump the recipe version whenever photo preparation changes its output.
JPEG_VERSION = (pillow_version, features.version_codec("jpg"),
                features.version_feature("libjpeg_turbo"))
LEGACY_ENCODER_VERSION = json.dumps((1, *JPEG_VERSION))
ENCODER_VERSION = json.dumps((2, *JPEG_VERSION, features.version_module("littlecms2")))
FRAME_SIZE = (3840, 2160)


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
    with path.open("rb") as source:
        return _prepare_photo(source, crop)


def _to_srgb(image):
    profile = image.info.get("icc_profile")
    if not profile:
        return image

    # Transform color channels only. Keep the original per-pixel transparency.
    alpha = None
    color = image
    if image.has_transparency_data:
        alpha = image.convert("RGBA").getchannel("A")
        color = image.convert("L" if image.mode in ("1", "L", "LA") else "RGB")
    elif image.mode in ("1", "P"):
        color = image.convert("L" if image.mode == "1" else "RGB")

    try:
        converted = ImageCms.profileToProfile(
            color, io.BytesIO(profile), ImageCms.createProfile("sRGB"), outputMode="RGB"
        )
    except ImageCms.PyCMSError:
        return image
    if alpha is not None:
        converted.putalpha(alpha)
    return converted


def _prepare_photo(source, crop, *, legacy=False) -> bytes:
    with Image.open(source) as original:
        image = ImageOps.exif_transpose(original)
        if legacy:
            image = image.convert("RGBA")
        else:
            image = _to_srgb(image)
            mode = "RGBA" if image.has_transparency_data else "RGB"
            if image.mode != mode:
                image = image.convert(mode)
        if crop and crop[0] == revision(os.fstat(source.fileno())):
            box = crop_box(image.size, *crop[1:])
            image = image.resize(FRAME_SIZE, Image.Resampling.LANCZOS, box=box)
        else:
            image.thumbnail(FRAME_SIZE, Image.Resampling.LANCZOS)

        if image.mode == "RGBA" or image.size != FRAME_SIZE:
            canvas = Image.new("RGB", FRAME_SIZE, "black")
            offset = ((FRAME_SIZE[0] - image.width) // 2, (FRAME_SIZE[1] - image.height) // 2)
            canvas.paste(image, offset, image if image.mode == "RGBA" else None)
            image = canvas

        output = io.BytesIO()
        # The pixels are sRGB. Keep the JPEG free of source metadata.
        image.save(output, "JPEG", quality=90, icc_profile=None)
        return output.getvalue()


def _source_revision(stat):
    return json.dumps((stat.st_dev, stat.st_ino, stat.st_ctime_ns, revision(stat)))


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
            db.execute("""CREATE TABLE IF NOT EXISTS prepared_digests (
                path TEXT PRIMARY KEY, revision TEXT, crop TEXT, encoder TEXT, digest TEXT)""")
            db.execute("""CREATE TABLE IF NOT EXISTS color_digests (
                path TEXT PRIMARY KEY, revision TEXT, crop TEXT, encoder TEXT, digest TEXT, legacy_digest TEXT)""")

    def prepared_digest(self, key):
        with self.connect() as db:
            row = db.execute("""SELECT digest, legacy_digest FROM color_digests
                                WHERE path=? AND revision=? AND crop=? AND encoder=?""",
                             (*key, ENCODER_VERSION)).fetchone()
            if row:
                return row
            legacy = db.execute("""SELECT digest FROM prepared_digests
                                   WHERE path=? AND revision=? AND crop=? AND encoder=?""",
                                (*key, LEGACY_ENCODER_VERSION)).fetchone()
            return None, legacy[0] if legacy else None

    def cache_prepared_digest(self, key, digest, legacy_digest):
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO color_digests VALUES (?, ?, ?, ?, ?, ?)",
                       (*key, ENCODER_VERSION, digest, legacy_digest))

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
               *, path: Path | None = None, resend: bool = False, work_key=None, legacy_digest=None):
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
                    db.execute("DELETE FROM removed WHERE tv=? AND digest IN (?, ?)", (tv, digest, legacy_digest))

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

    def lookup(digest, legacy_digest):
        if not resend and (digest in removed or legacy_digest in removed):
            return True, None
        for candidate in (digest, legacy_digest):
            previous = records.get(candidate)
            if previous and previous[1] in existing:
                return False, candidate
        return False, None

    for index, path in enumerate(paths):
        if cancelled.is_set():
            return summary("Stopped.")
        progress(index, len(paths), f"Preparing {path.name}")
        crop = crops.get(str(path))
        key = (str(path.absolute()), _source_revision(path.stat()), json.dumps(crop))
        digest, legacy_digest = store.prepared_digest(key)
        if _source_revision(path.stat()) != key[1]:
            digest = legacy_digest = None

        suppressed, matched = lookup(digest, legacy_digest)
        # A current receipt alone cannot rule out a removal under the old recipe.
        if not suppressed and (not matched or (removed and not resend and legacy_digest is None)):
            with path.open("rb") as source:
                stamp = _source_revision(os.fstat(source.fileno()))
                if stamp != key[1]:
                    key = (key[0], stamp, key[2])
                    legacy_digest = None
                if legacy_digest is None and (records or removed):
                    legacy_digest = hashlib.sha256(_prepare_photo(source, crop, legacy=True)).hexdigest()
                    source.seek(0)
                # Rehash the exact bytes that could be sent, even when the cache had a digest.
                payload = _prepare_photo(source, crop)
                digest = hashlib.sha256(payload).hexdigest()
                if _source_revision(path.stat()) != stamp:
                    raise RuntimeError(f"{path.name} changed during preparation. Sync again when the file is stable.")
                store.cache_prepared_digest(key, digest, legacy_digest)
            suppressed, matched = lookup(digest, legacy_digest)

        if suppressed:
            excluded += 1
        elif matched:
            name, content_id = records[matched]
            other_digest = legacy_digest if matched == digest else digest
            store.record(tv, matched, name, content_id, path=path, resend=resend,
                         work_key=work_key, legacy_digest=other_digest)
            if resend:
                removed.difference_update((digest, legacy_digest))
            if confirmed:
                confirmed(matched, content_id, True)
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
                store.record(tv, digest, path.name, content_id, path=path, resend=resend,
                             work_key=work_key, legacy_digest=legacy_digest)
                if resend:
                    removed.difference_update((digest, legacy_digest))
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

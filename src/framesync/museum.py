"""Museum discovery, local saved works and non-destructive artwork imports."""
from filecmp import cmp
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import secrets
import stat
from tempfile import NamedTemporaryFile, TemporaryDirectory
import threading
from time import monotonic
from urllib.error import URLError

from PIL import Image, ImageFile, ImageOps
from PySide6.QtCore import QThread

from .museum_providers import (
    Filters, MAX_IMPORT_BYTES, SOURCES, SearchPage, abort_thread_requests, chicago_search,
    clear_thread_cancellation, cleveland_search, fetch_json, met_search, museum_request,
    read_chunk, tracked_response, urlopen,
)

from .core import prepare_photo, revision

FEATURED = Path(__file__).with_name("featured")


def featured_works(source=""):
    records = json.loads((FEATURED / "works.json").read_text(encoding="utf-8"))
    return [
        dict(item, preview=(FEATURED / item["preview"]).as_uri())
        for item in records if not source or item["source"] == source
    ]


def image_size(url, cancelled):
    # PIL can parse JPEG dimensions and EXIF orientation from its initial segments.
    # Stop reading as soon as they are available, including on servers without Range support.
    parser = ImageFile.Parser()
    deadline = monotonic() + 12.0
    with urlopen(museum_request(url), timeout=8) as source, tracked_response(source, deadline=deadline) as expired:
        try:
            for _ in range(64):
                if cancelled():
                    raise InterruptedError("Museum search cancelled.")
                if expired.is_set() or monotonic() > deadline:
                    raise TimeoutError("Timed out reading image dimensions.")
                chunk = read_chunk(source, 16384)
                if not chunk:
                    break
                parser.feed(chunk)
                if parser.image:
                    width, height = parser.image.size
                    if parser.image.getexif().get(274) in (5, 6, 7, 8):
                        return height, width
                    return width, height
        except Exception as exc:
            if cancelled():
                raise InterruptedError("Museum search cancelled.") from exc
            if expired.is_set():
                raise TimeoutError("Timed out reading image dimensions.") from exc
            raise
    return 0, 0


def fit_label(width, height):
    if not width or not height:
        return "Dimensions unavailable"
    if (width, height) == (3840, 2160):
        return "Exact 4K"
    return "4K crop possible" if width >= 3840 and height >= 2160 else "Needs upscaling"


class MuseumLibrary:
    def __init__(self, store):
        self.store = store
        with store.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS saved_artwork (key TEXT PRIMARY KEY, record TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS museum_imports (path TEXT PRIMARY KEY, key TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS museum_image_sizes (url TEXT PRIMARY KEY, width INTEGER, height INTEGER)")

    def saved(self):
        with self.store.connect() as db:
            return {key: json.loads(record) for key, record in db.execute(
                "SELECT key, record FROM saved_artwork ORDER BY rowid DESC")}

    def toggle(self, work):
        with self.store.connect() as db:
            if db.execute("DELETE FROM saved_artwork WHERE key=?", (work["key"],)).rowcount == 0:
                db.execute("INSERT INTO saved_artwork VALUES (?, ?)", (work["key"], json.dumps(work)))

    def remember_import(self, path, key):
        with self.store.connect() as db:
            db.execute("INSERT OR REPLACE INTO museum_imports VALUES (?, ?)", (str(path.absolute()), key))

    def keys_on_tv(self, host, content_ids):
        if not content_ids:
            return set()
        paths = self.store.upload_paths(host)
        with self.store.connect() as db:
            imported = dict(db.execute("SELECT path,key FROM museum_imports"))
            direct = db.execute("""SELECT work_key,content_id FROM museum_sends
                                   JOIN uploads USING(tv,digest) WHERE tv=?""", (host,)).fetchall()
        found = {imported[path] for path, cid in paths if cid in content_ids and path in imported}
        found.update(key for key, cid in direct if cid in content_ids)
        # Imports predating source tracking used this app-generated Met suffix.
        legacy_ids = content_ids.difference(cid for path, cid in paths if path in imported)
        for name, cid in self.store.records(host).values():
            if cid in legacy_ids and (match := re.search(r" \(Met (\d+)\)\.jpg$", name)):
                found.add("met:" + match[1])
        return found

    def dimensions(self, url, cancelled):
        with self.store.connect() as db:
            row = db.execute("SELECT width,height FROM museum_image_sizes WHERE url=?", (url,)).fetchone()
        if row:
            return row
        try:
            size = image_size(url, cancelled)
        except InterruptedError:
            raise
        except (OSError, ValueError, URLError, Image.DecompressionBombError):
            return 0, 0
        if all(size):
            with self.store.connect() as db:
                db.execute("INSERT OR REPLACE INTO museum_image_sizes VALUES (?, ?, ?)", (url, *size))
        return size


def search(query, offset, cancelled, *, filters=Filters(), library=None):
    providers = {"met": met_search, "cleveland": cleveland_search, "chicago": chicago_search}
    page = providers[filters.source](query, offset, filters, cancelled, fetch_json)
    curated = {w["key"]: w for w in featured_works("chicago")} if filters.source == "chicago" else {}
    matches = []
    for work in page.works:
        if cancelled():
            raise InterruptedError("Museum search cancelled.")
        work = curated.get(work["key"], work)
        if library and work["source"] != "chicago" and not (work["width"] and work["height"]):
            work["width"], work["height"] = library.dimensions(work["original"], cancelled)
        width, height = work["width"], work["height"]
        if filters.fits and (width < 3840 or height < 2160):
            continue
        if filters.orientation != "Any":
            width = width or work.get("sourceWidth", 0)
            height = height or work.get("sourceHeight", 0)
            if not width or not height:
                continue
            orientation = "Square" if width == height else "Landscape" if width > height else "Portrait"
            if orientation != filters.orientation:
                continue
        matches.append(work)
    return SearchPage(matches, page.next_offset, page.more)


@dataclass
class LocalImageNeeded:
    key: str
    link: str
    title: str


@dataclass
class MuseumPreview:
    directory: TemporaryDirectory
    path: Path
    key: str
    title: str

    def crop(self, framing):
        return (revision(self.path.stat()), *framing)

    def save_copy(self, target, framing, cancelled):
        payload = prepare_photo(self.path, self.crop(framing))
        try:
            existing_mode = stat.S_IMODE(target.stat().st_mode)
        except FileNotFoundError:
            existing_mode = None
        if existing_mode is not None:
            # Preserve existing destination permissions without briefly exposing private files.
            with NamedTemporaryFile(dir=target.parent, prefix=".framesync-", delete=False) as stream:
                staging = Path(stream.name)
                try:
                    stream.write(payload)
                    stream.close()
                    if cancelled():
                        raise InterruptedError("Save cancelled.")
                    os.chmod(staging, existing_mode)
                    staging.replace(target)
                finally:
                    staging.unlink(missing_ok=True)
        else:
            # Let the OS apply the process umask atomically without mutating global umask.
            while True:
                staging = target.parent / f".framesync-{secrets.token_hex(6)}.tmp"
                try:
                    fd = os.open(staging, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o666)
                    break
                except FileExistsError:
                    continue
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(payload)
                if cancelled():
                    raise InterruptedError("Save cancelled.")
                staging.replace(target)
            finally:
                staging.unlink(missing_ok=True)
        return target


def preview_work(work, cancelled, local_file=None):
    directory = TemporaryDirectory(prefix="framesync-preview-")
    try:
        path = import_work(work, Path(directory.name), cancelled, local_file)
        return MuseumPreview(directory, path, work["key"], work["title"])
    except BaseException:
        directory.cleanup()
        raise


def import_work(work, folder, cancelled, local_file=None):
    title = re.sub(r'[^\w .–-]', '', work["title"])[:80].strip() or "Artwork"
    # Bound by UTF-8 bytes as some titles use multibyte characters.
    title = title.encode("utf-8")[:200].decode("utf-8", errors="ignore")
    label = {"met": "Met", "cleveland": "CMA", "chicago": "AIC"}[work.get("source", "met")]
    target = folder / f"{title} ({label} {work['id']}).jpg"
    with NamedTemporaryFile(dir=folder, prefix=".framesync-", suffix=".part", delete=False) as temp:
        staging = Path(temp.name)
    try:
        deadline = None if local_file else monotonic() + 60.0
        source_file = local_file.open("rb") if local_file else urlopen(museum_request(work["original"]), timeout=15)
        with source_file as source, tracked_response(source, deadline=deadline) as expired, staging.open("wb") as output:
            received = 0
            try:
                while chunk := read_chunk(source, 128 * 1024):
                    received += len(chunk)
                    if received > MAX_IMPORT_BYTES:
                        raise ValueError("This source image exceeds the 128 MB import limit.")
                    if cancelled():
                        raise InterruptedError("Artwork download cancelled.")
                    if expired.is_set() or (deadline is not None and monotonic() > deadline):
                        raise TimeoutError("Timed out downloading artwork from museum service.")
                    output.write(chunk)
            except Exception as exc:
                if cancelled():
                    raise InterruptedError("Artwork download cancelled.") from exc
                if expired.is_set():
                    raise TimeoutError("Timed out downloading artwork from museum service.") from exc
                raise
            if cancelled():
                raise InterruptedError("Artwork download cancelled.")
            if expired.is_set():
                raise TimeoutError("Timed out downloading artwork from museum service.")
        with Image.open(staging) as image:
            image.load()
            if image.format != "JPEG":
                if (work.get("source") != "cleveland" and not local_file) or image.format not in ("TIFF", "PNG"):
                    raise ValueError("The museum returned an unsupported image format.")
                # Cleveland distributes its largest Open Access images as TIFFs.
                converted = ImageOps.exif_transpose(image).convert("RGB")
            else:
                converted = None
        if converted is not None:
            converted.save(staging, format="JPEG", quality=95)
        if cancelled():
            raise InterruptedError("Artwork download cancelled.")
        try:
            os.link(staging, target)
        except FileExistsError:
            if target.is_symlink() or not target.is_file() or not cmp(staging, target, shallow=False):
                raise ValueError(f"A different file named {target.name} already exists in this folder.")
        return target
    finally:
        staging.unlink(missing_ok=True)


class MuseumTask(QThread):
    def __init__(self, operation):
        super().__init__()
        self.operation = operation
        self.result = None
        self.error = ""
        self._thread_lock = threading.Lock()
        self._thread_id = None

    def requestInterruption(self):
        super().requestInterruption()
        with self._thread_lock:
            thread_id = self._thread_id
        abort_thread_requests(thread_id)

    def run(self):
        ident = threading.get_ident()
        with self._thread_lock:
            self._thread_id = ident
        if self.isInterruptionRequested():
            abort_thread_requests(ident)
        try:
            self.result = self.operation(self.isInterruptionRequested)
        except Exception as exc:
            self.error = str(exc) or type(exc).__name__
        finally:
            clear_thread_cancellation(ident)
            with self._thread_lock:
                self._thread_id = None

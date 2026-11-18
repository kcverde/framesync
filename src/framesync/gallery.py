"""The connected Frame's artwork, including photos uploaded outside FrameSync."""
import base64
from dataclasses import dataclass
from functools import partial
import hashlib
import io
import json
from time import monotonic

from PIL import Image
from PySide6.QtCore import QObject, Property, Signal, Slot

from .core import artwork_list
from .tv import call_tv

PAGE_SIZE = 12
PAGE_CACHE_SECONDS = 120


def thumbnail_url(raw):
    """Limit image size passed through the QML model, irrespective of TV encoding."""
    if not isinstance(raw, (bytes, bytearray)) or not raw:
        return ""
    try:
        with Image.open(io.BytesIO(raw)) as image:
            image.thumbnail((384, 216))
            encoded = io.BytesIO()
            image.convert("RGB").save(encoded, "JPEG", quality=83)
    except (OSError, ValueError, Image.DecompressionBombError):
        return ""
    return "data:image/jpeg;base64," + base64.b64encode(encoded.getvalue()).decode("ascii")


def thumbnails(art, ids):
    if not ids:
        return {}
    try:
        images = call_tv(art, art.get_thumbnail_list, ids)
    except Exception:
        # Some models provide an individual thumbnail command only.
        try:
            images = call_tv(art, art.get_thumbnail, ids, as_dict=True)
        except Exception:
            return {}
    if not isinstance(images, dict):
        return {}
    previews = {}
    for key, raw in images.items():
        # Newer TVs include a filename extension in the D2D response.
        cid = key if key in ids else str(key).rpartition(".")[0]
        if cid in ids:
            previews[cid] = thumbnail_url(raw)
    return previews


def artwork_identity(item, thumb):
    # Exclude favourite, display and matte settings: changing any of them keeps the same photo.
    fields = ("image_date", "date", "creation_time", "created_at", "file_name", "title",
              "width", "height", "image_width", "image_height", "file_size")
    metadata = {key: item[key] for key in fields if item.get(key) is not None}
    if not thumb and not any(metadata.get(key) for key in fields[:6]):
        return ""
    data = json.dumps(metadata, sort_keys=True, ensure_ascii=True) + thumb
    return hashlib.sha256(data.encode()).hexdigest()


@dataclass
class FramePage:
    rows: list
    ids: set
    page: int
    current: str
    message: str


def _catalog(art, store, host, items=None):
    if items is None:
        items = artwork_list(art)
    receipts = {cid: (digest, name) for digest, (name, cid) in store.records(host).items() if cid}
    message = ""
    try:
        current = str((call_tv(art, art.get_current) or {}).get("content_id") or "")
    except Exception:
        current = ""
    # Some Frames include untitled server references alongside the photo library.
    # Keep known uploads and the currently displayed work even in this category.
    items = [item for item in items if not (
        item.get("category_id") == "MY-C0008" and item.get("content_type") == "server"
        and str(item.get("slideshow")).lower() == "true" and not item.get("title")
        and str(item["content_id"]) not in receipts and str(item["content_id"]) != current
    )]
    ids = {str(item["content_id"]) for item in items}
    if not current and items:
        message = "The Frame did not report which artwork is showing."
    return items, receipts, ids, current, message


def _read_page(art, page, items, receipts, ids, current, message):
    page = max(0, min(page, (len(items) - 1) // PAGE_SIZE))
    entries = items[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
    previews = thumbnails(art, [str(item["content_id"]) for item in entries])
    rows = []
    for item in entries:
        cid = str(item["content_id"])
        digest, name = receipts.get(cid, ("", ""))
        thumb = previews.get(cid, "")
        rows.append(dict(cid=cid, identity=artwork_identity(item, thumb), name=name or item.get("title") or cid, digest=digest,
                         category=item.get("category_id") or "", thumb=thumb,
                         current=bool(current and cid == current), metadata=item))
    return FramePage(rows, ids, page, current, message)


def frame_page(art, store, host, page=0, *, items=None):
    return _read_page(art, page, *_catalog(art, store, host, items))


def load_gallery(art, task):
    catalog = _catalog(art, task.store, task.host)
    total = max(1, (len(catalog[0]) + PAGE_SIZE - 1) // PAGE_SIZE)
    pages = []
    for index in range(total):
        if task.cancelled.is_set():
            raise InterruptedError("Gallery loading cancelled.")
        task.progress.emit(index, total, f"Loading gallery page {index + 1} of {total}…")
        pages.append(_read_page(art, index, *catalog))
    task.progress.emit(total, total, "Gallery loaded.")
    return pages


def show_artwork(art, store, host, page, cid):
    items = artwork_list(art)
    item = next((item for item in items if str(item["content_id"]) == cid), None)
    if item is None:
        result = frame_page(art, store, host, page, items=items)
        result.message = "This artwork is no longer on the Frame."
        return result
    call_tv(art, art.select_image, cid, category=item.get("category_id"), show=True)
    result = frame_page(art, store, host, page, items=items)
    if result.current != cid:
        result.message = "Display requested. Refresh to check what is showing."
    return result


def remove_artwork(art, store, host, page, cid, identity):
    items = artwork_list(art)
    item = next((item for item in items if str(item["content_id"]) == cid), None)
    if item is not None:
        thumb = thumbnails(art, [cid]).get(cid, "")
        if not identity or artwork_identity(item, thumb) != identity:
            result = frame_page(art, store, host, page, items=items)
            result.message = "The photo changed or could not be identified. Review the refreshed gallery."
            return result
    added_marks = store.mark_removing(host, cid)
    if item is not None:
        # A list read verifies the outcome on TVs that omit the normal delete acknowledgement.
        try:
            acknowledged = call_tv(art, art.delete, cid)
            items = artwork_list(art)
        except Exception as exc:
            raise RuntimeError("Could not confirm removal. Refresh On the Frame to check the photo.") from exc
        if any(str(item["content_id"]) == cid for item in items):
            if not acknowledged:
                store.undo_removal_intent(host, added_marks)
            raise RuntimeError("The artwork is still on the Frame. Check the TV and refresh.")
    store.confirm_removal(host, cid)
    result = frame_page(art, store, host, page, items=items)
    result.message = "Removed from this Frame."
    return result


class Gallery(QObject):
    changed = Signal()

    def __init__(self, backend):
        super().__init__(backend)
        self.backend = backend
        self.snapshot = None
        self._pages = []
        self._expires = 0

    @Property("QVariantList", notify=changed)
    def rows(self):
        return self.snapshot.rows if self.snapshot else []

    @Property(bool, notify=changed)
    def loaded(self):
        return self.snapshot is not None

    @Property("QVariantMap", notify=changed)
    def state(self):
        if self.snapshot is None:
            return dict(page=0, pages=0, count=0, message="")
        count = len(self.snapshot.ids)
        return dict(page=self.snapshot.page, pages=(count + PAGE_SIZE - 1) // PAGE_SIZE,
                    count=count, message=self.snapshot.message)

    def reset(self):
        self.invalidatePages()
        self.snapshot = None
        self.changed.emit()

    @Slot()
    def invalidatePages(self):
        self._pages.clear()

    def _apply(self, snapshot):
        self.snapshot = snapshot
        self.backend.updateMuseumOnFrame(snapshot.ids)
        self.changed.emit()

    def apply_action(self, snapshot):
        self.invalidatePages()
        self._apply(snapshot)

    def _start(self, operation, page, message, **kwargs):
        backend = self.backend
        # Capture the target on the GUI thread before starting network work.
        work = partial(operation, store=backend.store, host=backend.host.strip(), page=page, **kwargs)
        backend.run_tv_operation(work, self._apply, message)

    def _preload(self, index):
        if not self.backend.paired:
            return

        def loaded(pages):
            self._pages = pages
            self._expires = monotonic() + PAGE_CACHE_SECONDS
            self._apply(pages[min(index, len(pages) - 1)])

        self.backend.start_task(load_gallery, loaded, "Loading gallery…", gallery_loading=True)

    @Slot()
    def refresh(self):
        self._pages.clear()
        self._preload(self.state["page"])

    @Slot()
    def openPage(self):
        self._visit(self.state["page"])

    @Slot(int)
    def goToPage(self, index):
        if 0 <= index < self.state["pages"]:
            self._visit(index)

    def _visit(self, index):
        if index < len(self._pages) and monotonic() < self._expires:
            if self.snapshot is not self._pages[index]:
                self.snapshot = self._pages[index]
                self.backend.set_message("")
                self.changed.emit()
            return
        self._preload(index)

    @Slot(str)
    def show(self, cid):
        if any(row["cid"] == cid for row in self.rows):
            self._pages.clear()
            self._start(show_artwork, self.state["page"], "Displaying artwork…", cid=cid)

    @Slot(str, str)
    def remove(self, cid, identity):
        if identity and any(row["cid"] == cid and row["identity"] == identity for row in self.rows):
            self._pages.clear()
            self._start(remove_artwork, self.state["page"], "Removing artwork from the Frame…",
                        cid=cid, identity=identity)

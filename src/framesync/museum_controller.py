"""Discover views, saved artwork and the museum preview lifecycle."""
from functools import partial
from pathlib import Path
import sqlite3
from urllib.error import URLError

from PySide6.QtCore import QObject, Property, QUrl, Signal, Slot

from . import museum
from .core import sync_files
from .gallery import show_artwork


class MuseumController(QObject):
    changed = Signal()
    museumPreviewReady = Signal("QVariantMap")
    museumPreviewSaved = Signal()
    museumFileNeeded = Signal(str, str, str)

    def __init__(self, backend):
        super().__init__(backend)
        self.backend = backend
        self.store = backend.store
        self._shutting_down = False
        self._sending_preview = None
        self.museum_task = None
        self.museum_preview = None
        self.last_museum_upload = None
        self.museum_library = museum.MuseumLibrary(self.store)
        self.saved_works = self.museum_library.saved()
        self.museum_on_frame = set()
        self.featured_source = ""
        self.museum_view = "Featured"
        self.museum_filters = museum.Filters()
        self.works = museum.featured_works()
        self.search_query = ""
        self.next_offset = 0
        self.more = False
        self.museum_message = ""

    @Property("QVariantMap", notify=changed)
    def state(self):
        works = self.saved_works.values() if self.museum_view == "Saved" else self.works
        rows = [
            dict(work, saved=work["key"] in self.saved_works, onFrame=work["key"] in self.museum_on_frame,
                 fit=museum.fit_label(work["width"], work["height"]), museum=museum.SOURCES[work["source"]])
            for work in works
        ]
        return dict(works=rows, query=self.search_query, more=self.more and self.museum_view == "Results",
                    sending=self._sending_preview is not None,
                    featuredMuseum=museum.SOURCES.get(self.featured_source, "All museums"),
                    view=self.museum_view, message=self.museum_message, busy=bool(self.museum_task),
                    canShowUpload=self.last_museum_upload is not None)

    def update_on_frame(self, content_ids):
        if self.last_museum_upload and self.last_museum_upload[1] not in content_ids:
            self.last_museum_upload = None
            self.museum_message = "This crop is no longer on the Frame." if self.backend.paired else ""
        self.museum_on_frame = self.museum_library.keys_on_tv(self.backend.host.strip(), set(content_ids))
        self.changed.emit()

    @Slot(str)
    @Slot(str, str)
    def museumView(self, view, source=""):
        if self.museum_task or view not in ("Featured", "Saved") or (source and source not in museum.SOURCES):
            return
        self.last_museum_upload = None
        self.museum_view = view
        self.museum_message = ""
        if view == "Featured":
            self.featured_source = source
            self.works = museum.featured_works(source)
            self.search_query = ""
            self.more = False
        self.changed.emit()

    def museum_work(self, key):
        if self.museum_view == "Saved":
            return self.saved_works.get(key)
        return next((work for work in self.works if work["key"] == key), None)

    @Slot(str)
    def saveMuseumWork(self, key):
        work = self.museum_work(key)
        if work:
            try:
                self.museum_library.toggle(work)
                self.saved_works = self.museum_library.saved()
            except sqlite3.Error as exc:
                self.museum_message = str(exc)
            self.changed.emit()

    def start_museum_task(self, operation, message):
        self.last_museum_upload = None
        self.museum_task = museum.MuseumTask(operation)
        self.museum_task.setParent(self)
        self.museum_task.finished.connect(self.finish_museum_task)
        self.museum_message = message
        self.changed.emit()
        self.museum_task.start()

    @Slot(str, "QVariantMap", bool)
    def searchMuseum(self, query, form, more):
        if self.backend.task or self.museum_task:
            return
        if more:
            if self.museum_view != "Results" or not self.more:
                return
        else:
            try:
                filters = museum.Filters.from_form(form)
                if filters.source not in museum.SOURCES:
                    raise ValueError("Choose a museum to search.")
            except (TypeError, ValueError) as exc:
                self.museum_message = str(exc)
                self.changed.emit()
                return
            self.museum_view = "Results"
            self.search_query = query.strip()
            self.museum_filters = filters
            self.next_offset = 0
            self.more = False
            self.works = []
        operation = partial(museum.search, self.search_query, self.next_offset,
                            filters=self.museum_filters, library=self.museum_library)
        self.start_museum_task(operation, f"Searching {museum.SOURCES[self.museum_filters.source]}…")

    @Slot(str)
    def importMuseum(self, key):
        self.start_museum_import(key)

    @Slot(str, QUrl)
    def importMuseumFile(self, key, url):
        if url.isLocalFile():
            self.start_museum_import(key, Path(url.toLocalFile()))

    def start_museum_import(self, key, local_file=None):
        if self.backend.task or self.museum_task:
            return
        work = self.museum_work(key)
        if work:
            self.discardMuseumPreview()

            def download(cancelled):
                try:
                    return museum.preview_work(work, cancelled, local_file)
                except (URLError, TimeoutError):
                    if local_file or work["source"] != "chicago":
                        raise
                    return museum.LocalImageNeeded(key, work["link"], work["title"])

            self.start_museum_task(download, "Preparing artwork preview…")

    @Slot()
    def discardMuseumPreview(self):
        if self.museum_preview and not self.museum_task:
            self.museum_preview.directory.cleanup()
            self.museum_preview = None

    def can_use_preview(self, zoom, x, y):
        return (self.museum_preview is not None and not self.backend.task and not self.museum_task
                and 1 <= zoom <= 3 and 0 <= x <= 1 and 0 <= y <= 1)

    @Slot(QUrl, float, float, float, result=bool)
    def saveMuseumPreview(self, url, zoom, x, y):
        if not self.can_use_preview(zoom, x, y) or not url.isLocalFile():
            return False
        target = Path(url.toLocalFile())
        if target.suffix.lower() not in (".jpg", ".jpeg"):
            self.museum_message = "Choose a JPEG filename ending in .jpg or .jpeg."
            self.changed.emit()
            return False
        preview = self.museum_preview

        self.start_museum_task(partial(preview.save_copy, target, (zoom, x, y)), "Saving cropped copy…")
        return True

    @Slot(float, float, float, result=bool)
    def sendMuseumPreview(self, zoom, x, y):
        if not self.can_use_preview(zoom, x, y) or not self.backend.paired or self.backend.uncertain:
            return False
        preview = self.museum_preview

        receipt = None

        def confirmed(digest, cid, existing):
            nonlocal receipt
            receipt = (digest, cid, existing)

        def sent(summary):
            self._finish_sending()
            self.backend.set_message(summary)
            if receipt:
                digest, cid, existing = receipt
                self.last_museum_upload = (digest, cid, preview.title)
                status = "Already on your Frame" if existing else "Added to your Frame"
                self.museum_message = f"{status} · {preview.title}"
            else:
                self.museum_message = summary
            self.changed.emit()

        def send(art, task):
            crops = {str(preview.path): preview.crop((zoom, x, y))}
            return sync_files(art, task.store, task.host, [preview.path], task.cancelled,
                              task.progress.emit, resend=True, snapshot=task.snapshot,
                              crop_overrides=crops, work_key=preview.key, confirmed=confirmed)

        def send_failed(error):
            self._finish_sending()
            self.museum_message = error
            if self.backend.uncertain:
                self.museum_message += " Open Local photos to resolve the interrupted upload."
            self.changed.emit()

        if not self.backend.start_task(send, sent, "Preparing artwork for the Frame…", syncing=True,
                                       failed=send_failed):
            self._show_error(self.backend.message)
            return False
        self._sending_preview = preview
        self.museum_preview = None
        self.museum_message = "Sending artwork to the Frame…"
        self.changed.emit()
        return True

    def _finish_sending(self):
        if self._sending_preview:
            self._sending_preview.directory.cleanup()
            self._sending_preview = None

    def _show_error(self, error):
        self.museum_message = error
        self.changed.emit()

    @Slot()
    def showMuseumUpload(self):
        if not self.last_museum_upload or not self.backend.paired or self.backend.task or self.museum_task:
            return
        digest, cid, title = self.last_museum_upload
        host = self.backend.host.strip()
        if self.store.records(host).get(digest, (None, None))[1] != cid:
            self.last_museum_upload = None
            self.museum_message = "This crop is no longer on the Frame."
            self.changed.emit()
            return
        operation = partial(show_artwork, store=self.store, host=host, page=self.backend.gallery.state["page"], cid=cid)

        def displayed(snapshot):
            self.backend.gallery.apply_action(snapshot)
            if cid not in snapshot.ids or snapshot.current == cid:
                self.last_museum_upload = None
            self.museum_message = snapshot.message or f"Now showing · {title}"
            self.changed.emit()

        if self.backend.run_tv_operation(operation, displayed, "Displaying artwork…", failed=self._show_error):
            self.museum_message = "Displaying artwork…"
            self.changed.emit()

    @Slot()
    def finish_museum_task(self):
        task = self.sender() or self.museum_task
        if task is not None:
            task.deleteLater()
        if self._shutting_down or task is not self.museum_task:
            return
        self.museum_task = None
        self.museum_message = task.error
        photo = None
        if isinstance(task.result, museum.SearchPage):
            seen = {work["key"] for work in self.works}
            self.works.extend(work for work in task.result.works if work["key"] not in seen)
            self.more = task.result.more
            self.next_offset = task.result.next_offset
            if not task.result.works and task.result.excluded_by_filters:
                self.museum_message = "No images on this page match your filters."
            elif not self.works:
                self.museum_message = "No Open Access images on this page."
        elif isinstance(task.result, museum.LocalImageNeeded):
            self.museum_message = "Image download unavailable. You can import a copy from the museum record."
            self.museumFileNeeded.emit(task.result.key, task.result.link, task.result.title)
        elif isinstance(task.result, museum.MuseumPreview):
            if task.isInterruptionRequested():
                task.result.directory.cleanup()
            else:
                self.museum_preview = task.result
                photo = self.backend.photo_row(task.result.path, None)
                photo["name"] = task.result.title
        elif isinstance(task.result, Path):
            self.museum_message = f"Saved cropped copy to {task.result.name}."
            try:
                if self.museum_preview:
                    self.museum_library.remember_import(task.result, self.museum_preview.key)
                if self.backend.folder and task.result.parent.resolve() == self.backend.folder.resolve():
                    self.backend.load_folder(self.backend.folder)
            except (OSError, ValueError, sqlite3.Error) as exc:
                self.museum_message += f" Could not update the local library: {exc}"
            self.museumPreviewSaved.emit()
        self.changed.emit()
        if photo:
            self.museumPreviewReady.emit(photo)

    def reset_target(self):
        self.last_museum_upload = None
        self.museum_message = ""
        self.update_on_frame(set())

    def cancel(self):
        if self.museum_task:
            self.museum_task.requestInterruption()
            self._show_error("Cancelling museum request…")

    def begin_shutdown(self):
        self._shutting_down = True
        if self.museum_task:
            self.museum_task.requestInterruption()

    def cleanup(self):
        if self.museum_task and isinstance(self.museum_task.result, museum.MuseumPreview):
            self.museum_task.result.directory.cleanup()
        self.museum_task = None
        self.discardMuseumPreview()
        self._finish_sending()

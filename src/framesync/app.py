from __future__ import annotations

from functools import partial
from pathlib import Path
import sqlite3
import sys
import threading
from urllib.error import URLError

from PIL import Image
from PySide6.QtCore import QObject, Property, QLockFile, Qt, QThread, QUrl, Signal, Slot
from PySide6.QtWidgets import QApplication, QMessageBox
from PySide6.QtQml import QQmlApplicationEngine
from .core import EXTENSIONS, Store, content_ids, photos_in, revision, sync_files, sync_folder, validate_host
from .gallery import Gallery, show_artwork
from .frame_settings import FrameSettings
from .tv import artwork_connection
from . import museum


class Task(QThread):
    progress = Signal(int, int, str)
    status = Signal(str)

    def __init__(self, store, host, port, operation, completed, syncing, tv_lock, *, reconnect=False):
        super().__init__()
        self.store, self.host, self.port = store, host, port
        self.operation = operation
        self.completed = completed
        self.syncing = syncing
        self.tv_lock = tv_lock
        self.reconnect = reconnect
        self.cancelled = threading.Event()
        self.output = None
        self.error = ""
        self.content_snapshot = None
        self.showing_museum_upload = False
        self.museum_preview = None

    def snapshot(self, ids):
        self.content_snapshot = set(ids)

    def run(self):
        try:
            with self.tv_lock:
                if self.cancelled.is_set():
                    raise InterruptedError("TV operation cancelled.")
                options = {"reconnect": True} if self.reconnect else {}
                with artwork_connection(self.store, self.host, self.port, self.status.emit, **options) as art:
                    self.output = self.operation(art, self)
        except Exception as exc:
            self.error = str(exc) or type(exc).__name__


def read_library(art, task):
    try:
        ids = content_ids(art)
    except Exception as exc:
        raise RuntimeError(f"The artwork channel opened, but reading the photo library failed. ({exc})") from exc
    task.snapshot(ids)
    return len(ids)


COLOR_SCHEMES = {
    "system": Qt.ColorScheme.Unknown,
    "light": Qt.ColorScheme.Light,
    "dark": Qt.ColorScheme.Dark,
}


class Controller(QObject):
    appearanceChanged = Signal()
    changed = Signal()
    photosChanged = Signal()
    selectionChanged = Signal()
    museumChanged = Signal()
    museumPreviewReady = Signal("QVariantMap")
    museumPreviewSaved = Signal()
    museumFileNeeded = Signal(str, str, str)

    def __init__(self, store):
        super().__init__()
        self.store = store
        settings = store.settings()
        preference = settings.get("appearance")
        self._appearance = preference if preference in ("light", "dark") else "system"
        self.host = settings.get("host", "")
        self.port = settings.get("port", 8002)
        self.last_connection = settings.get("last_connection") or {}
        if isinstance(self.last_connection, dict) and self.last_connection.get("port") in (8001, 8002):
            self.host = self.last_connection.get("host", self.host)
            self.port = self.last_connection["port"]
        self.reconnect_task = None
        self.startup_checked = False
        self.known_target = self.last_connection == {"host": self.host, "port": self.port}
        self.folder = None
        self.folder_paths = []
        self.extra_paths = []
        self.selected = set()
        self.paths = []
        self.rows = []
        self.paired = False
        self.task = None
        self.tv_lock = threading.Lock()
        self.gallery_task = None
        self.gallery_done = self.gallery_total = 0
        self.gallery_message = ""
        self.museum_task = None
        self.museum_preview = None
        self.last_museum_upload = None
        self.museum_library = museum.MuseumLibrary(store)
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
        self.message = "Connect your TV to start syncing."
        self.done = self.total = 0
        self.uncertain = []
        self.gallery = Gallery(self)
        self._frame_settings = FrameSettings(self)
        self._frame_settings.artworkChanged.connect(self.gallery.invalidatePages)
        self.read_journal()
        folder = settings.get("folder")
        if folder and Path(folder).is_dir():
            try:
                self.load_folder(Path(folder))
            except OSError as exc:
                self.message = str(exc)

    @Property(str, notify=appearanceChanged)
    def appearance(self):
        return self._appearance

    @Slot(str)
    def setAppearance(self, value):
        if value not in COLOR_SCHEMES or value == self._appearance:
            return
        try:
            settings = self.store.settings()
            settings["appearance"] = value
            self.store.save_settings(settings)
        except OSError as exc:
            self.set_message(f"Could not save appearance: {exc}")
            self.appearanceChanged.emit()
            return
        self._appearance = value
        QApplication.styleHints().setColorScheme(COLOR_SCHEMES[value])
        self.appearanceChanged.emit()

    @Property(QObject, constant=True)
    def frameGallery(self):
        return self.gallery

    @Property(QObject, constant=True)
    def frameSettings(self):
        return self._frame_settings

    @Property("QVariantMap", notify=changed)
    def state(self):
        return dict(host=self.host, port=self.port, paired=self.paired, knownTarget=self.known_target, reconnecting=self.reconnect_task is not None, busy=bool(self.task or self.museum_task),
                    museumSending=bool(self.task and self.task.museum_preview),
                    galleryLoading=self.gallery_task is not None, galleryDone=self.gallery_done,
                    galleryTotal=self.gallery_total, galleryMessage=self.gallery_message,
                    syncing=bool(self.task and self.task.syncing), folder=str(self.folder or ""),
                    selectedCount=len(self.selected), folderCount=len(self.folder_paths),
                    ready=self.paired and bool(self.folder_paths) and not (self.task or self.museum_task) and not self.uncertain,
                    message=self.message, done=self.done, total=self.total, uncertain=self.uncertain)

    @Property("QVariantMap", notify=selectionChanged)
    def selectedUrls(self):
        return {QUrl.fromLocalFile(str(path)).toString(): True for path in self.selected}

    @Property("QVariantList", notify=photosChanged)
    def photos(self):
        return self.rows

    @Property("QVariantMap", notify=museumChanged)
    def collection(self):
        works = self.saved_works.values() if self.museum_view == "Saved" else self.works
        rows = [
            dict(work, saved=work["key"] in self.saved_works, onFrame=work["key"] in self.museum_on_frame,
                 fit=museum.fit_label(work["width"], work["height"]), museum=museum.SOURCES[work["source"]])
            for work in works
        ]
        return dict(works=rows, query=self.search_query, more=self.more and self.museum_view == "Results",
                    featuredMuseum=museum.SOURCES.get(self.featured_source, "All museums"),
                    view=self.museum_view, message=self.museum_message, busy=bool(self.museum_task),
                    canShowUpload=self.last_museum_upload is not None)

    def updateMuseumOnFrame(self, content_ids):
        if self.last_museum_upload and self.last_museum_upload[1] not in content_ids:
            self.last_museum_upload = None
            self.museum_message = "This crop is no longer on the Frame." if self.paired else ""
        self.museum_on_frame = self.museum_library.keys_on_tv(self.host.strip(), set(content_ids))
        self.museumChanged.emit()

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
        self.museumChanged.emit()

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
            self.museumChanged.emit()

    def start_museum_task(self, operation, message):
        self.last_museum_upload = None
        self.museum_task = museum.MuseumTask(operation)
        self.museum_task.finished.connect(self.finish_museum_task)
        self.museum_message = message
        self.museumChanged.emit()
        self.changed.emit()
        self.museum_task.start()

    @Slot(str, "QVariantMap", bool)
    def searchMuseum(self, query, form, more):
        if self.task or self.museum_task:
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
                self.museumChanged.emit()
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
        if self.task or self.museum_task:
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
        return (self.museum_preview is not None and not self.task and not self.museum_task
                and 1 <= zoom <= 3 and 0 <= x <= 1 and 0 <= y <= 1)

    @Slot(QUrl, float, float, float, result=bool)
    def saveMuseumPreview(self, url, zoom, x, y):
        if not self.can_use_preview(zoom, x, y) or not url.isLocalFile():
            return False
        target = Path(url.toLocalFile())
        if target.suffix.lower() not in (".jpg", ".jpeg"):
            self.museum_message = "Choose a JPEG filename ending in .jpg or .jpeg."
            self.museumChanged.emit()
            return False
        preview = self.museum_preview

        self.start_museum_task(partial(preview.save_copy, target, (zoom, x, y)), "Saving cropped copy…")
        return True

    @Slot(float, float, float, result=bool)
    def sendMuseumPreview(self, zoom, x, y):
        if not self.can_use_preview(zoom, x, y) or not self.paired or self.uncertain:
            return False
        preview = self.museum_preview

        receipt = None

        def confirmed(digest, cid, existing):
            nonlocal receipt
            receipt = (digest, cid, existing)

        def sent(summary):
            self.set_message(summary)
            if receipt:
                digest, cid, existing = receipt
                self.last_museum_upload = (digest, cid, preview.title)
                status = "Already on your Frame" if existing else "Added to your Frame"
                self.museum_message = f"{status} · {preview.title}"
            else:
                self.museum_message = summary
            self.museumChanged.emit()

        def send(art, task):
            crops = {str(preview.path): preview.crop((zoom, x, y))}
            return sync_files(art, task.store, task.host, [preview.path], task.cancelled,
                              task.progress.emit, resend=True, snapshot=task.snapshot,
                              crop_overrides=crops, work_key=preview.key, confirmed=confirmed)

        if not self.start_task(send, sent, "Preparing artwork for the Frame…", syncing=True):
            self.museum_message = self.message
            self.museumChanged.emit()
            return False
        self.museum_message = "Sending artwork to the Frame…"
        self.museumChanged.emit()
        self.task.museum_preview = preview
        self.museum_preview = None
        self.changed.emit()
        return True

    @Slot()
    def showMuseumUpload(self):
        if not self.last_museum_upload or not self.paired or self.task or self.museum_task:
            return
        digest, cid, title = self.last_museum_upload
        host = self.host.strip()
        if self.store.records(host).get(digest, (None, None))[1] != cid:
            self.last_museum_upload = None
            self.museum_message = "This crop is no longer on the Frame."
            self.museumChanged.emit()
            return
        operation = partial(show_artwork, store=self.store, host=host, page=self.gallery.state["page"], cid=cid)

        def displayed(snapshot):
            self.gallery.apply_action(snapshot)
            if cid not in snapshot.ids or snapshot.current == cid:
                self.last_museum_upload = None
            self.museum_message = snapshot.message or f"Now showing · {title}"
            self.museumChanged.emit()

        if self.run_tv_operation(operation, displayed, "Displaying artwork…"):
            self.task.showing_museum_upload = True
            self.museum_message = "Displaying artwork…"
            self.museumChanged.emit()

    @Slot()
    def finish_museum_task(self):
        task = self.museum_task
        self.museum_task = None
        task.deleteLater()
        self.museum_message = task.error
        photo = None
        if isinstance(task.result, museum.SearchPage):
            seen = {work["key"] for work in self.works}
            self.works.extend(work for work in task.result.works if work["key"] not in seen)
            self.more = task.result.more
            self.next_offset = task.result.next_offset
            if not self.works:
                self.museum_message = "No Open Access images on this page."
        elif isinstance(task.result, museum.LocalImageNeeded):
            self.museum_message = "Image download unavailable. You can import a copy from the museum record."
            self.museumFileNeeded.emit(task.result.key, task.result.link, task.result.title)
        elif isinstance(task.result, museum.MuseumPreview):
            if task.isInterruptionRequested():
                task.result.directory.cleanup()
            else:
                self.museum_preview = task.result
                photo = self.photo_row(task.result.path, None)
                photo["name"] = task.result.title
        elif isinstance(task.result, Path):
            self.museum_message = f"Saved cropped copy to {task.result.name}."
            try:
                self.museum_library.remember_import(task.result, self.museum_preview.key)
                if self.folder and task.result.parent == self.folder:
                    self.load_folder(self.folder)
            except (OSError, ValueError, sqlite3.Error) as exc:
                self.museum_message += f" Could not update the local library: {exc}"
            self.museumPreviewSaved.emit()
        self.museumChanged.emit()
        self.changed.emit()
        if photo:
            self.museumPreviewReady.emit(photo)

    def read_journal(self):
        self.uncertain = [name for name, cid in self.store.records(self.host.strip()).values() if cid is None]

    @Slot(str, int)
    def setTarget(self, host, port):
        if self.task or self.museum_task or (host, port) == (self.host, self.port):
            return
        self.last_museum_upload = None
        self.museum_message = ""
        self.cancel_reconnect()
        self.host, self.port = host, port
        self.known_target = self.last_connection == {"host": host.strip(), "port": port}
        self.paired = False
        self.cancel_gallery()
        self.gallery.reset()
        self._frame_settings.reset()
        self.updateMuseumOnFrame(set())
        self.message = "Connect your TV to start syncing."
        self.read_journal()
        self.changed.emit()

    def save(self):
        self.store.save_settings(dict(host=self.host.strip(), port=self.port, folder=str(self.folder or ""),
                                      appearance=self.appearance, last_connection=self.last_connection))

    def load_folder(self, path):
        paths = photos_in(path)
        if path != self.folder:
            self.extra_paths.clear()
            self.selected.clear()
        self.folder, self.folder_paths = path, paths
        self.reload_photos()

    def reload_photos(self):
        self.paths = list(dict.fromkeys(self.folder_paths + self.extra_paths))
        self.selected.intersection_update(self.paths)
        self.selectionChanged.emit()
        crops = self.store.crops()
        self.rows = [self.photo_row(p, crops.get(str(p))) for p in self.paths]
        self.photosChanged.emit()

    @Slot("QVariantList")
    def addPhotos(self, urls):
        if self.task or self.museum_task:
            return
        added = []
        for value in urls:
            url = QUrl(value)
            if url.isLocalFile():
                path = Path(url.toLocalFile())
                if path.is_file() and path.suffix.lower() in EXTENSIONS:
                    added.append(path)
        if not added:
            self.set_message("Choose JPG or PNG files to send.")
            return
        self.extra_paths = list(dict.fromkeys(self.extra_paths + added))
        self.selected.update(added)
        self.reload_photos()
        message = f"{len(self.selected)} selected for sending."
        if len(added) < len(urls):
            message += " Only JPG and PNG files were added."
        self.set_message(message)

    @Slot(int, bool)
    def selectPhoto(self, index, selected):
        if self.task or self.museum_task or not 0 <= index < len(self.paths):
            return
        path = self.paths[index]
        if selected:
            self.selected.add(path)
        else:
            self.selected.discard(path)
        self.selectionChanged.emit()
        self.changed.emit()

    def photo_row(self, path, crop):
        width = height = 0
        stamp = ""
        try:
            with Image.open(path) as image:
                width, height = image.size
                if image.getexif().get(274) in (5, 6, 7, 8):
                    width, height = height, width
            stamp = revision(path.stat())
        except (OSError, ValueError, Image.DecompressionBombError):
            width = height = 0
        cropped = bool(crop and crop[0] == stamp)
        return dict(name=path.name, url=QUrl.fromLocalFile(str(path)).toString(),
                    w=width, h=height, stamp=stamp, cropped=cropped,
                    exact=cropped or (width, height) == (3840, 2160),
                    framing=crop[1:] if cropped else [1.0, 0.5, 0.5])

    @Slot(int, float, float, float, bool, result=bool)
    def applyCrop(self, index, zoom, x, y, reset):
        if self.task or self.museum_task or not 0 <= index < len(self.paths):
            return False
        if not (1 <= zoom <= 3 and 0 <= x <= 1 and 0 <= y <= 1):
            return False
        path = self.paths[index]
        try:
            stamp = revision(path.stat())
            if stamp != self.rows[index]["stamp"]:
                self.reload_photos()
                self.set_message("The photo changed. Open it again to crop the updated image.")
                return False
            values = None if reset else [stamp, zoom, x, y]
            self.store.set_crop(path, values)
            self.rows[index] = self.photo_row(path, values)
        except (OSError, sqlite3.Error) as exc:
            self.set_message(str(exc))
            return False
        self.photosChanged.emit()
        self.set_message("Whole photo restored." if reset else "Crop ready for sync.")
        return True

    @Slot(QUrl)
    def chooseFolder(self, url):
        if self.museum_task or self.task or not url.isLocalFile():
            return
        try:
            self.load_folder(Path(url.toLocalFile()))
            self.save()
            self.message = "Folder ready." if self.paths else "Choose a folder containing JPG or PNG photos."
        except (ValueError, OSError) as exc:
            self.message = str(exc)
        self.changed.emit()

    def reconnect_on_startup(self):
        if self.startup_checked or self.paired or self.task:
            return
        self.startup_checked = True
        try:
            host = validate_host(self.host)
            # Previous secure installations already have a per-TV pairing token.
            if self.port == 8002:
                if not self.store.load_token(host):
                    return
            elif self.port != 8001 or self.last_connection != {"host": host, "port": 8001}:
                return
        except (ValueError, OSError):
            return
        task = Task(self.store, host, self.port, read_library, self.connected, False,
                    self.tv_lock, reconnect=True)
        task.setParent(self)
        self.known_target = True
        self.reconnect_task = task
        task.finished.connect(self.reconnect_finished)
        task.start()
        self.changed.emit()

    def cancel_reconnect(self):
        if self.reconnect_task:
            self.reconnect_task.cancelled.set()
            self.reconnect_task = None

    @Slot()
    def reconnect_finished(self):
        task = self.sender()
        task.deleteLater()
        if self.reconnect_task is not task:
            return
        self.reconnect_task = None
        if task.error:
            self.set_message("Couldn’t reconnect to the Frame. Retry when the TV is ready.")
        else:
            self.updateMuseumOnFrame(task.content_snapshot)
            self.connected(task.output)

    @Slot(bool)
    def launch(self, sync):
        if sync:
            if self.state["ready"]:
                self.start_send()
        elif not self.task and not self.museum_task and not self.reconnect_task:
            self.paired = False
            self.gallery.reset()
            self._frame_settings.reset()
            self.updateMuseumOnFrame(set())
            self.start_task(read_library, self.connected, "Connecting to your TV…")

    def connected(self, count):
        self.paired = True
        self.known_target = True
        target = {"host": self.host.strip(), "port": self.port}
        message = f"Connected. {count} artworks on your TV. Ready to sync."
        if self.last_connection != target:
            self.last_connection = target
            try:
                self.save()
            except OSError as exc:
                message += f" Could not save the reconnect preference: {exc}"
        self.gallery.openPage()
        self.set_message(message)

    @Slot()
    def sendSelected(self):
        if self.paired and self.selected and not self.uncertain:
            self.start_send([path for path in self.paths if path in self.selected])

    def start_send(self, paths=None):
        folder = self.folder

        def send(art, task):
            if paths is not None:
                return sync_files(art, task.store, task.host, paths, task.cancelled, task.progress.emit,
                                  resend=True, snapshot=task.snapshot)
            return sync_folder(art, task.store, task.host, folder, task.cancelled, task.progress.emit,
                               snapshot=task.snapshot)

        self.start_task(send, self.set_message, "Preparing to send photos…", syncing=True)

    def run_tv_operation(self, operation, finished, message):
        if not self.paired:
            self.set_message("Connect to your Frame first.")
            return False
        return self.start_task(lambda art, _: operation(art), finished, message)

    def start_task(self, operation, completed, message, syncing=False, gallery_loading=False):
        if self.task or (self.museum_task and not gallery_loading) or (gallery_loading and self.gallery_task):
            return False
        try:
            host = validate_host(self.host)
            if not gallery_loading:
                self.save()
        except (ValueError, OSError) as exc:
            self.set_message(str(exc))
            return False
        if gallery_loading:
            task = Task(self.store, host, self.port, operation, completed, False, self.tv_lock)
            task.setParent(self)
            self.gallery_task = task
            self.gallery_done = self.gallery_total = 0
            self.gallery_message = message
            task.progress.connect(self.gallery_progress)
            task.finished.connect(self.gallery_finished)
            task.start()
            self.changed.emit()
            return True
        resume_gallery = self.gallery_task is not None
        self.cancel_gallery()
        self.task = Task(self.store, host, self.port, operation, completed, syncing, self.tv_lock)
        self.task.resume_gallery = resume_gallery
        self.task.progress.connect(self.progress)
        self.task.status.connect(self.set_message)
        self.task.finished.connect(self.finished)
        self.done = self.total = 0
        self.set_message(message)
        self.task.start()
        return True

    def cancel_gallery(self):
        if self.gallery_task:
            self.gallery_task.cancelled.set()
            self.gallery_task = None
        self.gallery_message = ""

    @Slot(int, int, str)
    def gallery_progress(self, done, total, message):
        if self.sender() is self.gallery_task:
            self.gallery_done, self.gallery_total, self.gallery_message = done, total, message
            self.changed.emit()

    @Slot()
    def gallery_finished(self):
        task = self.sender()
        task.deleteLater()
        if task is not self.gallery_task:
            return
        self.gallery_task = None
        self.gallery_message = task.error
        if not task.error:
            task.completed(task.output)
        self.changed.emit()

    def shutdown(self):
        self.cancel_reconnect()
        self.discardMuseumPreview()
        self.cancel_gallery()
        for task in self.findChildren(Task):
            task.wait()

    @Slot(int, int, str)
    def progress(self, done, total, message):
        self.done, self.total = done, total
        self.set_message(message)

    @Slot(str)
    def set_message(self, message):
        self.message = message
        self.changed.emit()

    @Slot()
    def finished(self):
        task, self.task = self.task, None
        task.deleteLater()
        if task.museum_preview:
            task.museum_preview.directory.cleanup()
        if task.syncing:
            self.gallery.reset()
        self.read_journal()
        if task.content_snapshot is not None:
            self.updateMuseumOnFrame(task.content_snapshot)
        if task.error:
            if task.museum_preview or task.showing_museum_upload:
                self.museum_message = task.error
                if task.museum_preview and self.uncertain:
                    self.museum_message += " Open Local photos to resolve the interrupted upload."
                self.museumChanged.emit()
            self.set_message(task.error)
        else:
            self.message = ""
            task.completed(task.output)
        if self.paired and (task.syncing or task.resume_gallery):
            self.gallery.openPage()
        self.changed.emit()

    @Slot()
    def stop(self):
        if self.museum_task:
            self.museum_task.requestInterruption()
            self.museum_message = "Cancelling museum request…"
            self.museumChanged.emit()
        if self.task:
            self.task.cancelled.set()
            self.set_message("Waiting for the current TV operation to finish…")

    @Slot()
    def resolve(self):
        if not self.task:
            self.store.clear_uncertain(self.host.strip())
            self.read_journal()
            self.set_message("Ready to retry. Send your selection or sync the folder again.")


def load_window(controller):
    QApplication.styleHints().setColorScheme(COLOR_SCHEMES[controller.appearance])
    engine = QQmlApplicationEngine()
    engine.setInitialProperties({"backend": controller})
    engine.load(QUrl.fromLocalFile(str(Path(__file__).with_name("Main.qml"))))
    if not engine.rootObjects():
        raise RuntimeError("FrameSync could not load its interface.")
    controller.reconnect_on_startup()
    return engine


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("FrameSync")
    root = Path.home() / "Library" / "Application Support" / "FrameSync"
    store = Store(root)
    lock = QLockFile(str(root / "app.lock"))
    if not lock.tryLock(0):
        QMessageBox.information(None, "FrameSync is already open", "Use the existing FrameSync window.")
        return
    controller = Controller(store)
    engine = load_window(controller)
    result = app.exec()
    controller.shutdown()
    del engine
    return result


if __name__ == "__main__":
    sys.exit(main())

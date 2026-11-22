from __future__ import annotations

from pathlib import Path
import sqlite3
import sys
import threading

from PIL import Image
from PySide6.QtCore import QObject, Property, QLockFile, Qt, QThread, QUrl, Signal, Slot
from PySide6.QtWidgets import QApplication, QMessageBox
from PySide6.QtQml import QQmlApplicationEngine
from .core import EXTENSIONS, Store, content_ids, photos_in, revision, sync_files, sync_folder, validate_host
from .gallery import Gallery
from .frame_settings import FrameSettings
from .tv import ReadCancellation, artwork_connection
from .museum_controller import MuseumController


class Task(QThread):
    progress = Signal(int, int, str)
    status = Signal(str)

    def __init__(self, store, host, port, operation, completed, syncing, tv_lock, *, reconnect=False, read_only=False, failed=None):
        super().__init__()
        self.store, self.host, self.port = store, host, port
        self.operation = operation
        self.completed = completed
        self.failed = failed
        self.syncing = syncing
        self.tv_lock = tv_lock
        self.reconnect = reconnect
        self.cancelled = threading.Event()
        self.read_cancellation = ReadCancellation(self.cancelled) if read_only or reconnect else None
        self.output = None
        self.error = ""
        self.content_snapshot = None

    def snapshot(self, ids):
        self.content_snapshot = set(ids)

    def cancel(self):
        if self.read_cancellation is not None:
            self.read_cancellation.cancel()
        else:
            self.cancelled.set()

    def run(self):
        try:
            with self.tv_lock:
                if self.cancelled.is_set():
                    raise InterruptedError("TV operation cancelled.")
                options = {"reconnect": True} if self.reconnect else {}
                if self.read_cancellation is not None:
                    options["cancellation"] = self.read_cancellation
                with artwork_connection(self.store, self.host, self.port, self.status.emit, **options) as art:
                    self.output = self.operation(art, self)
            if self.read_cancellation is not None:
                self.read_cancellation.check()
        except Exception as exc:
            if self.read_cancellation is not None and self.cancelled.is_set():
                self.error = "TV operation cancelled."
                self.content_snapshot = None
            else:
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
        self._shutting_down = False
        self.task = None
        self.tv_lock = threading.Lock()
        self.gallery_task = None
        self.gallery_message = ""
        self._museum = MuseumController(self)
        self._museum.changed.connect(self.changed)
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

    @Property(QObject, constant=True)
    def museum(self):
        return self._museum

    @Property("QVariantMap", notify=changed)
    def state(self):
        return dict(host=self.host, port=self.port, paired=self.paired, knownTarget=self.known_target, reconnecting=self.reconnect_task is not None, busy=bool(self.task or self._museum.museum_task),
                    connecting=bool(self.task and self.task.read_cancellation is not None),
                    galleryLoading=self.gallery_task is not None, galleryMessage=self.gallery_message,
                    syncing=bool(self.task and self.task.syncing), folder=str(self.folder or ""),
                    selectedCount=len(self.selected), folderCount=len(self.folder_paths), addedCount=len(self.extra_paths),
                    ready=self.paired and bool(self.folder_paths) and not (self.task or self._museum.museum_task) and not self.uncertain,
                    message=self.message, done=self.done, total=self.total, uncertain=self.uncertain)

    @Property("QVariantMap", notify=selectionChanged)
    def selectedUrls(self):
        return {QUrl.fromLocalFile(str(path)).toString(): True for path in self.selected}

    @Property("QVariantList", notify=photosChanged)
    def photos(self):
        return self.rows

    def read_journal(self):
        self.uncertain = [name for name, cid in self.store.records(self.host.strip()).values() if cid is None]

    @Slot(str, int)
    def setTarget(self, host, port):
        if self.task or self._museum.museum_task or (host, port) == (self.host, self.port):
            return
        self.cancel_reconnect()
        self.host, self.port = host, port
        self.known_target = self.last_connection == {"host": host.strip(), "port": port}
        self.paired = False
        self.gallery.reset()
        self._frame_settings.reset()
        self._museum.reset_target()
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
        else:
            self.extra_paths = [photo for photo in self.extra_paths if photo.is_file()]
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
        if self.task or self._museum.museum_task:
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

    @Slot()
    def refreshFolder(self):
        if self.task or self._museum.museum_task or self.folder is None:
            return
        try:
            self.load_folder(self.folder)
        except (OSError, ValueError) as exc:
            self.set_message(str(exc))
            return
        self.set_message("Folder refreshed.")

    @Slot()
    def clearAddedPhotos(self):
        if self.task or self._museum.museum_task or not self.extra_paths:
            return
        self.extra_paths.clear()
        self.reload_photos()
        self.set_message("Manually added photos cleared from the list.")

    @Slot()
    def clearSelection(self):
        if self.task or self._museum.museum_task or not self.selected:
            return
        self.selected.clear()
        self.selectionChanged.emit()
        self.set_message("Selection cleared.")

    @Slot(int, bool)
    def selectPhoto(self, index, selected):
        if self.task or self._museum.museum_task or not 0 <= index < len(self.paths):
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
        if self.task or self._museum.museum_task or not 0 <= index < len(self.paths):
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
        if self._museum.museum_task or self.task or not url.isLocalFile():
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
            self.reconnect_task.cancel()
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
            self._museum.update_on_frame(task.content_snapshot)
            self.connected(task.output)

    @Slot(bool)
    def launch(self, sync):
        if sync:
            if self.state["ready"]:
                self.start_send()
        elif not self.task and not self._museum.museum_task and not self.reconnect_task:
            self.paired = False
            self.gallery.reset()
            self._frame_settings.reset()
            self._museum.update_on_frame(set())
            self.start_task(read_library, self.connected, "Connecting to your TV…", read_only=True)

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

    def run_tv_operation(self, operation, finished, message, *, failed=None):
        if not self.paired:
            self.set_message("Connect to your Frame first.")
            return False
        return self.start_task(lambda art, _: operation(art), finished, message, failed=failed)

    def start_task(self, operation, completed, message, syncing=False, gallery_loading=False, *, read_only=False, failed=None):
        if self.task or (self._museum.museum_task and not gallery_loading) or (gallery_loading and self.gallery_task):
            return False
        try:
            host = validate_host(self.host)
            if not gallery_loading:
                self.save()
        except (ValueError, OSError) as exc:
            self.set_message(str(exc))
            return False
        if gallery_loading:
            task = Task(self.store, host, self.port, operation, completed, False, self.tv_lock, read_only=True)
            task.setParent(self)
            self.gallery_task = task
            self.gallery_message = message
            task.finished.connect(self.gallery_finished)
            task.start()
            self.changed.emit()
            return True
        resume_gallery = self.gallery_task is not None
        self.cancel_gallery()
        self.task = Task(self.store, host, self.port, operation, completed, syncing, self.tv_lock, read_only=read_only, failed=failed)
        self.task.setParent(self)
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
            self.gallery_task.cancel()
            self.gallery_task = None
        self.gallery_message = ""

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
        self._shutting_down = True
        self.cancel_reconnect()
        self.cancel_gallery()
        if self.task:
            self.task.cancel()
        self._museum.begin_shutdown()
        for thread in self.findChildren(QThread):
            thread.wait()
        self._museum.cleanup()
        self.task = None

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
        task = self.sender() or self.task
        if task is not None:
            task.deleteLater()
        if self._shutting_down or task is not self.task:
            return
        self.task = None
        if task.syncing:
            self.gallery.reset()
        self.read_journal()
        cancelled_read = task.read_cancellation is not None and task.cancelled.is_set()
        if task.content_snapshot is not None and not cancelled_read:
            self._museum.update_on_frame(task.content_snapshot)
        if cancelled_read:
            self.set_message("TV operation cancelled.")
        elif task.error:
            if task.failed:
                task.failed(task.error)
            self.set_message(task.error)
        else:
            self.message = ""
            task.completed(task.output)
        if not task.error and self.paired and (task.syncing or task.resume_gallery):
            self.gallery.openPage()
        self.changed.emit()

    @Slot()
    def cancelGallery(self):
        self.cancel_gallery()
        self.changed.emit()

    @Slot()
    def cancelConnection(self):
        if self.reconnect_task:
            self.cancel_reconnect()
            self.set_message("Reconnect cancelled.")
        elif self.task and self.task.read_cancellation is not None:
            self.task.cancel()
            self.set_message("Cancelling TV connection…")

    @Slot()
    def stop(self):
        self._museum.cancel()
        if self.task:
            if self.task.read_cancellation is not None:
                self.cancelConnection()
            else:
                self.task.cancel()
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
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = QLockFile(str(root / "app.lock"))
    if not lock.tryLock(0):
        QMessageBox.information(None, "FrameSync is already open", "Use the existing FrameSync window.")
        return
    store = Store(root)
    controller = Controller(store)
    engine = load_window(controller)
    result = app.exec()
    controller.shutdown()
    del engine
    return result


if __name__ == "__main__":
    sys.exit(main())

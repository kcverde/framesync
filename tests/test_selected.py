"""Exercise single-file and folder sends through the same QML upload controls."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch

from PIL import Image
from PySide6.QtCore import QMimeData, QObject, QPointF, Qt, QUrl
from PySide6.QtGui import QDragEnterEvent, QDropEvent
from PySide6.QtWidgets import QApplication

from framesync.app import Controller, load_window
from framesync.core import Store


class SelectedPhotoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_send_selected_without_changing_folder_or_uploading_neighbors(self):
        sent = []

        class TV:
            def available(self):
                return [{"content_id": str(i)} for i in range(len(sent))]

            def upload(self, payload, **kwargs):
                sent.append(payload)
                return str(len(sent) - 1)

        @contextmanager
        def connection(*args, **kwargs):
            yield TV()

        with TemporaryDirectory() as directory, patch("framesync.app.artwork_connection", connection):
            root = Path(directory)
            folder = root / "library"
            folder.mkdir()
            Image.new("RGB", (60, 40), "blue").save(folder / "one.jpg")
            outside = root / "selected.png"
            Image.new("RGB", (40, 60), "red").save(outside)
            store = Store(root / "state")
            controller = Controller(store)
            engine = load_window(controller)
            window = engine.rootObjects()[0]

            def click_and_wait(name):
                control = window.findChild(QObject, name)
                self.assertTrue(control.property("enabled"))
                control.clicked.emit()
                deadline = time.monotonic() + 5
                while controller.task and time.monotonic() < deadline:
                    self.app.processEvents()
                    time.sleep(0.005)
                self.assertIsNone(controller.task)

            controller.chooseFolder(QUrl.fromLocalFile(str(folder)))
            controller.setTarget("192.168.1.50", 8002)
            click_and_wait("connect")
            self.app.processEvents()
            mime = QMimeData()
            mime.setUrls([QUrl.fromLocalFile(str(outside))] * 2)
            actions = Qt.DropAction.CopyAction
            button, modifiers = Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier
            enter = QDragEnterEvent(QPointF(550, 300), actions, mime, button, modifiers)
            self.app.sendEvent(window, enter)
            self.assertTrue(enter.isAccepted())
            drop = QDropEvent(QPointF(550, 300), actions, mime, button, modifiers)
            self.app.sendEvent(window, drop)
            self.assertTrue(drop.isAccepted())
            self.assertEqual(controller.state["selectedCount"], 1)
            self.assertEqual(controller.folder, folder)
            self.assertTrue(controller.applyCrop(controller.paths.index(outside), 1.5, 0.4, 0.2, False))
            click_and_wait("sendSelected")
            self.assertEqual({name for name, _ in store.records(controller.host).values()}, {"selected.png"})
            click_and_wait("sendSelected")
            self.assertEqual(len(sent), 1)
            click_and_wait("sync")
            self.assertEqual(len(sent), 2)
            self.assertEqual(store.settings()["folder"], str(folder))
            self.assertEqual(list(folder.iterdir()), [folder / "one.jpg"])
            window.close()
            del engine

    def test_send_selected_available_without_a_folder(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            photo = root / "single.jpg"
            Image.new("RGB", (32, 32)).save(photo)
            controller = Controller(Store(root / "state"))
            controller.addPhotos([QUrl.fromLocalFile(str(photo))])
            self.assertIsNone(controller.folder)
            self.assertEqual(controller.selected, {photo})
            self.assertFalse(controller.state["ready"])
            controller.selectPhoto(0, False)
            self.assertFalse(controller.selected)

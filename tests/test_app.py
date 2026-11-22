import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from pathlib import Path
import tempfile
import time
from threading import Event
import unittest
from unittest.mock import patch

from PIL import Image
from PySide6.QtWidgets import QApplication

from PySide6.QtCore import QObject, QUrl

from framesync.app import Controller, load_window
from framesync.core import Store


class FakeTV:
    instances = []

    def __init__(self, *args, **kwargs):
        self.token = kwargs.get("token")
        self.ids = []
        self.closed = False
        self.instances.append(self)

    def open(self, resources=None):
        pass

    def _abort_d2d_sockets(self):
        pass

    def available(self):
        return [{"content_id": cid} for cid in self.ids]

    def upload(self, *args, **kwargs):
        self.ids.append("uploaded-photo")
        return "uploaded-photo"

    def close(self):
        self.closed = True


class FakeRemote(FakeTV):
    def open(self):
        self.token = "test-token"


class AppTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.enterContext(patch("framesync.tv._open_read_channel",
                                side_effect=lambda channel, *_: channel.open()))

    def window(self, store):
        controller = Controller(store)
        engine = load_window(controller)
        self.addCleanup(controller.shutdown)
        self.addCleanup(engine.deleteLater)
        root = engine.rootObjects()[0]
        self.addCleanup(root.close)
        return controller, lambda name: root.findChild(QObject, name)

    def wait_for_task(self, window):
        deadline = time.monotonic() + 5
        while (window.task or window.gallery_task or window.reconnect_task) and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.01)
        self.assertIsNone(window.task)
        self.assertIsNone(window.gallery_task)
        self.assertIsNone(window.reconnect_task)

    def test_pair_choose_sync_and_changed_ip_invalidates_connection(self):
        with tempfile.TemporaryDirectory() as directory, patch("framesync.tv.FrameArt", FakeTV), patch("framesync.tv.SamsungTVWS", FakeRemote):
            root = Path(directory)
            folder = root / "photos"
            folder.mkdir()
            Image.new("RGB", (100, 100), "blue").save(folder / "test.jpg")
            window, control = self.window(Store(root / "state"))
            self.assertFalse(control("sync").property("enabled"))
            window.setTarget("192.168.1.50", 8002)
            window.chooseFolder(QUrl.fromLocalFile(str(folder)))
            control("connect").clicked.emit()
            self.wait_for_task(window)
            self.assertTrue(control("sync").property("enabled"))
            control("sync").clicked.emit()
            self.wait_for_task(window)
            self.assertIn("1 added", window.message)
            self.assertTrue(FakeTV.instances[-1].closed)
            window.setTarget("192.168.1.51", 8002)
            self.assertFalse(control("sync").property("enabled"))

    def test_gallery_preloads_while_browsing_and_joins_the_existing_read(self):
        from framesync.gallery import load_gallery

        started, release = Event(), Event()

        def delayed_load(art, task, **kwargs):
            started.set()
            release.wait(3)
            return load_gallery(art, task, **kwargs)

        with tempfile.TemporaryDirectory() as directory, patch("framesync.tv.FrameArt", FakeTV), \
                patch("framesync.tv.SamsungTVWS", FakeRemote), \
                patch("framesync.gallery.load_gallery", side_effect=delayed_load) as load:
            controller, control = self.window(Store(Path(directory)))
            controller.setTarget("192.168.1.50", 8002)
            control("connect").click()
            deadline = time.monotonic() + 2
            while not started.is_set() and time.monotonic() < deadline:
                self.app.processEvents()
                time.sleep(.005)
            try:
                self.assertTrue(started.is_set())
                worker = controller.gallery_task
                self.assertFalse(controller.state["busy"])
                self.assertTrue(control("addPhotos").property("enabled"))
                control("discover").click()
                control("framePage").click()
                self.app.processEvents()
                self.assertTrue(control("galleryLoadProgress").property("visible"))
                controller.gallery.openPage()
                self.assertIs(controller.gallery_task, worker)
            finally:
                release.set()
                self.wait_for_task(controller)
            self.assertTrue(controller.gallery.loaded)
            self.assertEqual(load.call_count, 1)

    def test_startup_uses_saved_token_while_discover_and_local_photos_are_available(self):
        entered, release = Event(), Event()

        class SlowFrame(FakeTV):
            def available(self):
                entered.set()
                release.wait(3)
                return []

        with tempfile.TemporaryDirectory() as directory, patch("framesync.tv.FrameArt", SlowFrame), \
                patch("framesync.tv.SamsungTVWS") as pairing:
            store = Store(Path(directory))
            store.save_settings({"host": "192.168.1.50", "port": 8002})
            store.save_token("192.168.1.50", "existing-token")
            controller, control = self.window(store)
            try:
                self.assertTrue(entered.wait(2))
                self.assertFalse(controller.state["busy"])
                self.assertTrue(control("addPhotos").property("enabled"))
                control("discover").click()
                self.assertIn("Reconnecting", control("connectionStatus").property("text"))
                self.assertFalse(control("connectionSetup").property("visible"))
                worker = controller.reconnect_task
                controller.launch(False)
                self.assertIs(controller.reconnect_task, worker)
            finally:
                release.set()
                self.wait_for_task(controller)
            self.assertTrue(controller.paired)
            self.assertTrue(controller.gallery.loaded)
            self.assertEqual(store.settings()["last_connection"], {"host": "192.168.1.50", "port": 8002})
            pairing.assert_not_called()

    def test_unpaired_legacy_target_skipped_and_offline_known_tv_can_retry(self):
        host = "192.168.1.20"
        with tempfile.TemporaryDirectory() as directory, patch("framesync.tv.FrameArt") as art:
            store = Store(Path(directory))
            for port in (8002, 8001):
                store.save_settings({"host": host, "port": port})
                controller, control = self.window(store)
                self.assertIsNone(controller.reconnect_task)
                self.assertTrue(control("connectionSetup").property("visible"))
            art.assert_not_called()
            store.save_settings({"host": host, "port": 8001, "last_connection": {"host": host, "port": 8001}})
            art.side_effect = TimeoutError("TV asleep")
            controller, control = self.window(store)
            self.wait_for_task(controller)
            controller.reconnect_on_startup()
            self.assertEqual(art.call_count, 1)
            self.assertFalse(control("connectionSetup").property("visible"))
            self.assertTrue(control("retryConnection").property("visible"))
            self.assertFalse(controller.paired)
            art.side_effect = FakeTV
            control("retryConnection").click()
            self.wait_for_task(controller)
            self.assertTrue(controller.paired)
            self.assertFalse(control("retryConnection").property("visible"))
            self.assertFalse(control("connectionSetup").property("visible"))
            control("connectionStatus").click()
            self.assertTrue(control("host").property("visible"))
            self.assertEqual(control("host").property("text"), host)
            control("connectionStatus").click()
            self.assertFalse(control("connectionSetup").property("visible"))

    def test_connection_failure_restores_controls(self):
        with tempfile.TemporaryDirectory() as directory, patch("framesync.tv.SamsungTVWS", side_effect=TimeoutError("TV unavailable")):
            window, control = self.window(Store(Path(directory)))
            window.setTarget("192.168.1.50", 8002)
            control("connect").clicked.emit()
            self.wait_for_task(window)
            self.assertIn("TV unavailable", window.message)
            self.assertTrue(control("connect").property("enabled"))
            self.assertFalse(control("sync").property("enabled"))

    def test_resolve_then_retry_from_qml(self):
        with tempfile.TemporaryDirectory() as directory, patch("framesync.tv.FrameArt", FakeTV), patch("framesync.tv.SamsungTVWS", FakeRemote):
            root = Path(directory)
            Image.new("RGB", (32, 32)).save(root / "photo.png")
            window, control = self.window(Store(root / "state"))
            window.chooseFolder(QUrl.fromLocalFile(str(root)))
            window.setTarget("192.168.1.50", 8002)
            control("connect").clicked.emit()
            self.wait_for_task(window)
            with patch.object(FakeTV, "upload", side_effect=TimeoutError("lost confirmation")):
                control("sync").clicked.emit()
                self.wait_for_task(window)
            self.assertFalse(control("sync").property("enabled"))
            control("recover").clicked.emit()
            self.assertTrue(control("retryDialog").property("visible"))
            control("retryDialog").reject()
            self.assertFalse(control("sync").property("enabled"))
            control("recover").clicked.emit()
            control("confirmRetry").click()
            self.assertTrue(control("sync").property("enabled"))
            control("sync").clicked.emit()
            self.wait_for_task(window)
            self.assertIn("1 added", window.message)

    def test_exact_badge_uses_oriented_dimensions_and_saved_crop(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            exif = Image.Exif()
            exif[274] = 6
            Image.new("RGB", (2160, 3840)).save(root / "a.jpg", exif=exif)
            Image.new("RGB", (3840, 2159)).save(root / "b.jpg")
            window = Controller(Store(root / "state"))
            window.chooseFolder(QUrl.fromLocalFile(str(root)))
            self.assertEqual([row["exact"] for row in window.photos], [True, False])
            self.assertTrue(window.applyCrop(1, 2, 1, 0, False))
            reopened = Controller(window.store)
            self.assertTrue(reopened.photos[1]["exact"])
            self.assertEqual(reopened.photos[1]["framing"], [2, 1, 0])
            self.assertTrue(reopened.applyCrop(1, 1, 0.5, 0.5, True))
            self.assertFalse(reopened.photos[1]["exact"])

    def test_shutdown_interrupts_and_waits_for_museum_and_tv_tasks(self):
        import socket
        from framesync.museum_providers import tracked_response

        museum_started = Event()
        tv_started, tv_cancelled = Event(), Event()
        client, server = socket.socketpair()
        client.settimeout(5.0)

        class SocketResponse:
            def __init__(self, sock):
                self.fp = sock.makefile("rb")
                self._sock = sock

            def read(self, size=-1):
                return self.fp.read(size)

            def close(self):
                self.fp.close()
                self._sock.close()

        try:
            with tempfile.TemporaryDirectory() as directory, patch("framesync.tv.FrameArt", FakeTV), \
                    patch("framesync.tv.SamsungTVWS", FakeRemote):
                controller = Controller(Store(Path(directory)))
                controller.setTarget("192.168.1.50", 8002)

                def slow_museum(cancelled):
                    resp = SocketResponse(client)
                    try:
                        with tracked_response(resp):
                            museum_started.set()
                            resp.read(16)
                    finally:
                        resp.close()
                    return None

                def slow_tv(art, task):
                    tv_started.set()
                    if task.cancelled.wait(5):
                        tv_cancelled.set()
                    return 0

                controller.start_task(slow_tv, lambda _: None, "Working…")
                self.assertTrue(tv_started.wait(2))
                controller.start_museum_task(slow_museum, "Searching…")
                self.assertTrue(museum_started.wait(2))
                time.sleep(0.05)
                started_at = time.monotonic()
                controller.shutdown()
                self.assertLess(time.monotonic() - started_at, 1.0)
                self.assertTrue(tv_cancelled.is_set())
        finally:
            server.close()

    def test_sync_failure_does_not_trigger_gallery_reload(self):
        with tempfile.TemporaryDirectory() as directory, patch("framesync.tv.FrameArt", FakeTV), \
                patch("framesync.tv.SamsungTVWS", FakeRemote):
            root = Path(directory)
            Image.new("RGB", (32, 32)).save(root / "photo.png")
            controller, control = self.window(Store(root / "state"))
            controller.chooseFolder(QUrl.fromLocalFile(str(root)))
            controller.setTarget("192.168.1.50", 8002)
            control("connect").clicked.emit()
            self.wait_for_task(controller)
            with patch.object(FakeTV, "upload", side_effect=TimeoutError("lost confirmation")), \
                    patch.object(controller.gallery, "openPage") as reload_gallery:
                control("sync").clicked.emit()
                self.wait_for_task(controller)
                reload_gallery.assert_not_called()

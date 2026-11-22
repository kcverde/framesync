import hashlib
import io
import os
from contextlib import nullcontext
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PIL import Image
from PySide6.QtCore import QObject, QUrl
from PySide6.QtWidgets import QApplication

from framesync.app import Controller, load_window
from framesync.core import Store


class MuseumPreviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.store = Store(self.root / "state")
        self.controller = Controller(self.store)
        self.addCleanup(self.controller.shutdown)
        self.work = next(work for work in self.controller.museum.works if work["source"] == "cleveland")
        self.previews = []
        self.controller.museum.museumPreviewReady.connect(self.previews.append)
        source = Image.new("RGB", (320, 90), "red")
        source.paste("blue", (160, 0, 320, 90))
        jpeg = io.BytesIO()
        source.save(jpeg, "JPEG")
        self.enterContext(patch("framesync.museum.urlopen", side_effect=lambda *a, **kw: io.BytesIO(jpeg.getvalue())))

    def wait_for_workers(self):
        deadline = time.monotonic() + 5
        while self.controller.task or self.controller.museum.museum_task or self.controller.gallery_task:
            self.assertLess(time.monotonic(), deadline, "FrameSync worker timed out")
            self.app.processEvents()
            time.sleep(.005)

    def open_preview(self):
        previous_count = len(self.previews)
        self.controller.museum.importMuseum(self.work["key"])
        self.wait_for_workers()
        self.assertEqual(len(self.previews), previous_count + 1, self.controller.museum.museum_message)
        item = self.previews[-1]
        self.assertEqual((item["w"], item["h"]), (320, 90))
        path = Path(QUrl(item["url"]).toLocalFile())
        self.assertTrue(path.is_file())
        return path

    def fake_tv(self):
        # All connections, including the automatic gallery refresh, see one library.
        tv = Mock()
        ids = []
        tv.connection = None
        tv.available.side_effect = lambda: [dict(content_id=cid) for cid in ids]

        def upload(payload, **options):
            cid = f"MY_F{len(ids) + 1}"
            ids.append(cid)
            return cid

        tv.upload.side_effect = upload
        self.enterContext(patch("framesync.app.artwork_connection", return_value=nullcontext(tv)))
        self.controller.setTarget("192.168.1.10", 8002)
        self.controller.paired = True
        return tv

    def test_connect_from_preview_keeps_crop_through_error_and_success(self):
        tv = self.fake_tv()
        self.controller.paired = False
        engine = load_window(self.controller)
        window = engine.rootObjects()[0]
        window.setProperty("page", "discover")

        def control(name):
            return window.findChild(QObject, name)

        path = self.open_preview()
        action = control("sendMuseumCrop")
        deadline = time.monotonic() + 3
        while not action.property("enabled") and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(.005)
        self.assertTrue(action.property("enabled"))
        control("cropZoom").setProperty("value", 2)
        self.assertIn("Connect", action.property("text"))
        action.click()
        dialog = control("cropConnection")
        self.assertTrue(dialog.property("visible"))
        address = control("cropTVAddress")
        self.assertEqual(address.property("text"), self.controller.host)
        address.setProperty("text", "invalid")
        control("cropConnect").click()
        self.assertTrue(dialog.property("visible"))
        self.assertIn("IPv4", control("cropConnectionStatus").property("text"))
        address.setProperty("text", "192.168.1.10")
        control("cropConnect").click()
        self.wait_for_workers()
        self.assertFalse(dialog.property("visible"))
        self.assertTrue(control("cropEditor").property("visible"))
        self.assertTrue(path.exists())
        self.assertEqual(control("cropZoom").property("value"), 2)
        self.assertEqual(action.property("text"), "Send to Frame")
        self.assertTrue(action.property("enabled"))
        tv.upload.assert_not_called()
        control("cropEditor").reject()
        window.close()

    def assert_blue_4k_crop(self, source):
        with Image.open(source) as result:
            self.assertEqual((result.format, result.size), ("JPEG", (3840, 2160)))
            for xy in [(20, 20), (1920, 1080), (3819, 2139)]:
                r, g, b = result.getpixel(xy)
                self.assertGreater(b, 240)
                self.assertLess(max(r, g), 16)

    def test_cancel_discards_preview_and_leaves_chosen_folder_untouched(self):
        path = self.open_preview()
        self.assertIsNone(self.controller.folder)
        self.controller.museum.discardMuseumPreview()
        self.assertFalse(path.exists())

        folder = self.root / "photos"
        folder.mkdir()
        existing = folder / "existing.jpg"
        Image.new("RGB", (4, 4), "green").save(existing)
        original = existing.read_bytes()
        self.controller.chooseFolder(QUrl.fromLocalFile(str(folder)))
        path = self.open_preview()
        self.controller.museum.discardMuseumPreview()
        self.assertFalse(path.exists())
        self.assertEqual(list(folder.iterdir()), [existing])
        self.assertEqual(existing.read_bytes(), original)

    def test_export_saves_chosen_crop_without_a_folder_or_tv(self):
        path = self.open_preview()
        saved = []
        self.controller.museum.museumPreviewSaved.connect(lambda: saved.append(True))
        # A save failure leaves the source available to try another destination.
        invalid = self.root / "absent" / "framing.jpg"
        self.controller.museum.saveMuseumPreview(QUrl.fromLocalFile(str(invalid)), 2, 1, .5)
        self.wait_for_workers()
        self.assertFalse(saved)
        self.assertFalse(invalid.exists())
        self.assertTrue(path.exists())
        target = self.root / "my-framing.jpg"
        self.assertTrue(self.controller.museum.saveMuseumPreview(QUrl.fromLocalFile(str(target)), 2, 1, .5))
        self.wait_for_workers()
        self.assertEqual(saved, [True])
        self.assert_blue_4k_crop(target)
        self.assertTrue(path.exists())
        self.controller.museum.discardMuseumPreview()
        self.assertFalse(path.exists())
        self.assertFalse(self.controller.photos)
        self.assertIsNone(self.controller.folder)

    def test_direct_send_confirms_the_rendered_crop_and_deduplicates_fresh_preview(self):
        tv = self.fake_tv()
        for _ in range(2):
            path = self.open_preview()
            self.assertTrue(self.controller.museum.sendMuseumPreview(2, 1, .5))
            self.wait_for_workers()
            self.assertFalse(path.exists())
        tv.upload.assert_called_once()
        payload, = tv.upload.call_args.args
        self.assert_blue_4k_crop(io.BytesIO(payload))
        digest = hashlib.sha256(payload).hexdigest()
        _, cid = self.store.records(self.controller.host)[digest]
        self.assertEqual(cid, "MY_F1")
        self.assertEqual(self.controller.museum.museum_library.keys_on_tv(self.controller.host, {cid}), {self.work["key"]})
        self.assertFalse(self.controller.photos)
        self.assertIsNone(self.controller.folder)

    def test_discover_can_display_the_exact_new_or_previously_sent_crop(self):
        tv = self.fake_tv()
        current = {"content_id": ""}
        tv.get_current.side_effect = lambda: current.copy()
        tv.select_image.side_effect = lambda cid, **options: current.update(content_id=cid)
        engine = load_window(self.controller)
        window = engine.rootObjects()[0]
        window.setProperty("page", "discover")
        send = window.findChild(QObject, "sendMuseumCrop")
        show = window.findChild(QObject, "showMuseumUpload")
        zoom = window.findChild(QObject, "cropZoom")
        editor = window.findChild(QObject, "cropEditor")
        for framing, expected, existing in [(1, "MY_F1", False), (2, "MY_F2", False), (1, "MY_F1", True)]:
            self.open_preview()
            deadline = time.monotonic() + 3
            while not send.property("enabled") and time.monotonic() < deadline:
                self.app.processEvents()
                time.sleep(.005)
            zoom.setProperty("value", framing)
            before = tv.select_image.call_count
            send.click()
            self.wait_for_workers()
            self.assertFalse(editor.property("visible"))
            self.assertEqual(window.property("page"), "discover")
            self.assertEqual(tv.select_image.call_count, before)
            self.assertTrue(show.property("visible"))
            self.assertIn("Already on" if existing else "Added to", self.controller.museum.museum_message)
            show.click()
            self.wait_for_workers()
            tv.select_image.assert_called_with(expected, category=None, show=True)
            self.assertIn("Now showing", self.controller.museum.museum_message)
            self.assertFalse(show.property("visible"))
        self.assertEqual(tv.upload.call_count, 2)
        window.close()

    def test_show_after_removal_clears_action_without_selecting_another_work(self):
        tv = self.fake_tv()
        self.open_preview()
        self.assertTrue(self.controller.museum.sendMuseumPreview(2, 1, .5))
        self.wait_for_workers()
        tv.available.side_effect = lambda: [dict(content_id="another-photo")]
        self.controller.museum.showMuseumUpload()
        self.wait_for_workers()
        tv.select_image.assert_not_called()
        self.assertFalse(self.controller.museum.state["canShowUpload"])
        self.assertIn("no longer", self.controller.museum.museum_message)

    def test_lost_upload_confirmation_blocks_resend_until_explicit_resolution(self):
        tv = self.fake_tv()
        accept_upload = tv.upload.side_effect
        tv.upload.side_effect = TimeoutError("lost confirmation")
        self.open_preview()
        self.assertTrue(self.controller.museum.sendMuseumPreview(2, 1, .5))
        self.wait_for_workers()
        self.assertTrue(self.controller.uncertain)
        self.assertFalse(self.controller.museum.state["canShowUpload"])
        self.assertIn("Could not confirm", self.controller.museum.museum_message)
        self.assertIn(None, [cid for _, cid in self.store.records(self.controller.host).values()])
        staged = self.open_preview()
        self.assertFalse(self.controller.museum.sendMuseumPreview(2, 1, .5))
        self.assertTrue(staged.exists())
        tv.upload.assert_called_once()
        self.controller.resolve()
        tv.upload.side_effect = accept_upload
        self.assertTrue(self.controller.museum.sendMuseumPreview(2, 1, .5))
        self.wait_for_workers()
        self.assertFalse(staged.exists())
        self.assertFalse(self.controller.uncertain)
        self.assertEqual(tv.upload.call_count, 2)

    def test_save_copy_default_filename_handles_long_emoji_titles(self):
        engine = load_window(self.controller)
        window = engine.rootObjects()[0]
        editor = window.findChild(QObject, "cropEditor")
        save_button = window.findChild(QObject, "saveMuseumCopy")
        save_dialog = window.findChild(QObject, "museumSaveDialog")
        self.open_preview()
        photo = dict(editor.property("photo"))
        photo["name"] = "A" + "🎨" * 90
        editor.setProperty("photo", photo)
        deadline = time.monotonic() + 3
        while not save_button.property("enabled") and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.005)
        save_button.click()
        self.app.processEvents()
        selected = str(save_dialog.property("selectedFile").toString())
        self.assertTrue(selected.endswith(".jpg"))
        self.assertIn("🎨", selected)
        stem = Path(save_dialog.property("selectedFile").toLocalFile()).stem
        self.assertTrue(stem.startswith("A🎨"))
        self.assertLessEqual(len(stem.encode("utf-8")), 200)
        save_dialog.close()
        editor.reject()
        window.close()

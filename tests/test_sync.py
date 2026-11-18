import io
from pathlib import Path
import tempfile
import threading
import unittest

from PIL import Image

from framesync.core import Store, photos_in, prepare_photo, revision, sync_folder, validate_host


class FakeArt:
    def __init__(self):
        self.ids = {"existing-art"}
        self.sent = []
        self.fail = False
        self.visible = True

    def available(self):
        return [{"content_id": cid} for cid in self.ids]

    def upload(self, data, **kwargs):
        cid = f"photo-{len(self.sent)}"
        self.sent.append(data)
        if self.visible:
            self.ids.add(cid)
        if self.fail:
            raise TimeoutError("confirmation lost")
        return cid


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.folder = self.root / "photos"
        self.folder.mkdir()
        self.store = Store(self.root / "state")
        self.art = FakeArt()
        self.cancelled = threading.Event()
        self.photo = self.folder / "one.png"
        Image.new("RGB", (200, 100), "red").save(self.photo)

    def sync(self):
        return sync_folder(self.art, self.store, "192.168.1.50", self.folder, self.cancelled, lambda *args: None)

    def test_repeated_sync_and_renamed_copy_are_skipped(self):
        self.assertIn("1 added", self.sync())
        (self.folder / "copy.png").write_bytes(self.photo.read_bytes())
        self.assertIn("0 added · 2 already synced", self.sync())
        self.assertEqual(len(self.art.sent), 1)
        self.assertIn("existing-art", self.art.ids)
        self.assertEqual(len(Store(self.root / "state").records("192.168.1.50")), 1)

    def test_photo_removed_from_tv_is_uploaded_again(self):
        self.sync()
        self.art.ids = {"existing-art"}
        self.assertIn("1 added", self.sync())
        self.assertEqual(len(self.art.sent), 2)

    def test_lost_confirmation_blocks_retry_across_restart(self):
        self.art.fail = True
        with self.assertRaisesRegex(RuntimeError, "avoid duplicates"):
            self.sync()
        self.store = Store(self.root / "state")
        self.art.fail = False
        with self.assertRaisesRegex(RuntimeError, "previous upload"):
            self.sync()
        self.assertEqual(len(self.art.sent), 1)
        self.store.clear_uncertain("192.168.1.50")
        self.assertIn("1 added", self.sync())

    def test_missing_remote_confirmation_is_not_marked_synced(self):
        self.art.visible = False
        with self.assertRaisesRegex(RuntimeError, "not visible"):
            self.sync()
        self.assertTrue(all(cid is None for _, cid in self.store.records("192.168.1.50").values()))

    def test_modified_photo_is_new_and_local_delete_never_deletes_remote(self):
        self.sync()
        Image.new("RGB", (200, 100), "blue").save(self.photo)
        self.assertIn("1 added", self.sync())
        self.photo.unlink()
        self.assertIn("0 added", self.sync())
        self.assertEqual(len(self.art.ids), 3)

    def test_cancel_before_upload(self):
        self.cancelled.set()
        self.assertIn("Stopped", self.sync())
        self.assertFalse(self.art.sent)
        self.assertFalse(self.store.records("192.168.1.50"))

    def test_corrupt_photo_does_not_create_upload_receipt(self):
        self.photo.write_bytes(b"broken")
        with self.assertRaises(Exception):
            self.sync()
        self.assertFalse(self.art.sent)
        self.assertFalse(self.store.records("192.168.1.50"))

    def test_scan_ignores_subfolders_and_unsupported_files(self):
        (self.folder / "other.heic").write_bytes(b"ignored")
        nested = self.folder / "nested"
        nested.mkdir()
        Image.new("RGB", (10, 10)).save(nested / "hidden.jpg")
        self.assertEqual(photos_in(self.folder), [self.photo])

    def test_conversion_keeps_original_and_adds_borders(self):
        before = self.photo.read_bytes()
        image = Image.open(io.BytesIO(prepare_photo(self.photo)))
        self.assertEqual(image.size, (3840, 2160))
        self.assertEqual(image.format, "JPEG")
        self.assertEqual(image.getpixel((0, 0)), (0, 0, 0))
        self.assertEqual(self.photo.read_bytes(), before)

    def test_history_is_separate_for_each_tv(self):
        self.sync()
        self.assertFalse(self.store.records("192.168.1.51"))

    def test_cropped_pixels_are_uploaded_and_deduplicated_across_restart(self):
        image = Image.new("RGB", (400, 200), "red")
        image.paste("blue", (200, 0, 400, 200))
        image.save(self.photo)
        original = self.photo.read_bytes()
        stamp = revision(self.photo.stat())
        self.store.set_crop(self.photo, [stamp, 2, 1, 0.5])
        self.assertIn("1 added", self.sync())
        with Image.open(io.BytesIO(self.art.sent[0])) as cropped:
            self.assertEqual(cropped.size, (3840, 2160))
            for point in [(0, 0), (1920, 1080), (3839, 2159)]:
                self.assertGreater(cropped.getpixel(point)[2], 245)
        self.assertEqual(original, self.photo.read_bytes())
        self.store = Store(self.root / "state")
        self.assertIn("1 already synced", self.sync())
        self.store.set_crop(self.photo, [stamp, 2, 0, 0.5])
        self.assertIn("1 added", self.sync())
        with Image.open(io.BytesIO(self.art.sent[-1])) as changed:
            self.assertGreater(changed.getpixel((1920, 1080))[0], 245)

    def test_host_validation(self):
        self.assertEqual(validate_host(" 192.168.1.50 "), "192.168.1.50")
        for value in ["localhost", "127.0.0.1", "8.8.8.8", "0.0.0.0", "https://tv/"]:
            with self.assertRaises(ValueError):
                validate_host(value)


if __name__ == "__main__":
    unittest.main()

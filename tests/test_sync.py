import hashlib
import io
from pathlib import Path
import struct
import tempfile
import threading
import unittest
from unittest.mock import patch

from PIL import Image, ImageCms

from framesync import core
from framesync.core import Store, photos_in, prepare_photo, revision, sync_files, sync_folder, validate_host


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

    def test_cached_digest_skips_preparation_after_restart_and_removal(self):
        self.assertIn("1 added", self.sync())
        self.store = Store(self.root / "state")
        with patch.object(core, "_prepare_photo", side_effect=AssertionError("unexpected encoding")):
            self.assertIn("1 already synced", self.sync())
            self.store.mark_removing("192.168.1.50", "photo-0")
            self.store.confirm_removal("192.168.1.50", "photo-0")
            self.art.ids.remove("photo-0")
            self.assertIn("1 previously removed", self.sync())
        self.assertEqual(len(self.art.sent), 1)

    def test_upload_uses_digest_of_new_payload_even_with_cached_digest(self):
        self.sync()
        self.art.ids.remove("photo-0")
        payload = b"newly prepared bytes"
        original_prepare = core._prepare_photo

        def prepare(source, crop, *, legacy=False):
            return original_prepare(source, crop, legacy=True) if legacy else payload

        with patch.object(core, "_prepare_photo", side_effect=prepare) as mocked:
            self.assertIn("1 added", self.sync())
        self.assertEqual(sum(not call.kwargs.get("legacy") for call in mocked.call_args_list), 1)
        self.assertEqual(self.art.sent[-1], payload)
        self.assertIn(hashlib.sha256(payload).hexdigest(), self.store.records("192.168.1.50"))

    def test_encoder_version_change_requires_preparation(self):
        self.sync()
        with patch.object(core, "ENCODER_VERSION", "new version"), patch.object(
            core, "_prepare_photo", wraps=core._prepare_photo
        ) as prepare:
            self.assertIn("1 already synced", self.sync())
        self.assertEqual(sum(not call.kwargs.get("legacy") for call in prepare.call_args_list), 1)

    def test_symlink_retargeted_during_cache_lookup_is_prepared(self):
        original = self.root / "original.png"
        replacement = self.root / "replacement.png"
        self.photo.rename(original)
        Image.new("RGB", (200, 100), "blue").save(replacement)
        self.photo.symlink_to(original)
        self.assertIn("1 added", self.sync())
        lookup = self.store.prepared_digest

        def retarget(key):
            digest = lookup(key)
            self.photo.unlink()
            self.photo.symlink_to(replacement)
            return digest

        with patch.object(self.store, "prepared_digest", side_effect=retarget):
            self.assertIn("1 added", self.sync())
        self.assertEqual(self.art.sent[-1], prepare_photo(replacement))

    def test_symlink_retargeted_during_preparation_stops_before_upload(self):
        original = self.root / "original.png"
        replacement = self.root / "replacement.png"
        self.photo.rename(original)
        Image.new("RGB", (200, 100), "blue").save(replacement)
        self.photo.symlink_to(original)

        def retarget(source, crop):
            self.photo.unlink()
            self.photo.symlink_to(replacement)
            return b"old payload"

        with patch.object(core, "_prepare_photo", side_effect=retarget):
            with self.assertRaisesRegex(RuntimeError, "changed during preparation"):
                self.sync()
        self.assertFalse(self.art.sent)
        self.assertFalse(self.store.records("192.168.1.50"))

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

    def save_profiled_photo(self, *, transparent=False):
        # Give an RGB profile distinct primaries without requiring a platform ICC file.
        profile = bytearray(ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes())
        count, = struct.unpack_from(">I", profile, 128)
        tags = {tag: (offset, size) for tag, offset, size in struct.iter_unpack(
            ">4sII", profile[132:132 + count * 12]
        )}
        red, red_size = tags[b"rXYZ"]
        green, green_size = tags[b"gXYZ"]
        self.assertEqual(red_size, green_size)
        profile[red:red + red_size], profile[green:green + green_size] = (
            profile[green:green + green_size], profile[red:red + red_size]
        )
        size = (200, 100)
        image = Image.new("RGBA", size, (255, 0, 0, 128)) if transparent else Image.new("RGB", size, "red")
        image.save(self.photo, icc_profile=bytes(profile))

    def test_profile_conversion_preserves_alpha(self):
        self.save_profiled_photo(transparent=True)
        with Image.open(io.BytesIO(prepare_photo(self.photo))) as jpeg:
            red, green, blue = jpeg.getpixel((1920, 1080))
            self.assertLess(red, 5)
            self.assertAlmostEqual(green, 128, delta=5)
            self.assertLess(blue, 5)

    def test_bad_icc_profile_falls_back_to_original_colors(self):
        unprofiled = prepare_photo(self.photo)
        Image.new("RGB", (200, 100), "red").save(self.photo, icc_profile=b"invalid profile")
        self.assertEqual(prepare_photo(self.photo), unprofiled)

    def test_legacy_receipt_skips_renamed_color_corrected_photo(self):
        self.save_profiled_photo()
        with self.photo.open("rb") as source:
            legacy = hashlib.sha256(core._prepare_photo(source, None, legacy=True)).hexdigest()
        self.assertNotEqual(legacy, hashlib.sha256(prepare_photo(self.photo)).hexdigest())
        host = "192.168.1.50"
        self.store.record(host, legacy, self.photo.name, "existing-art", path=self.photo)
        renamed = self.photo.with_name("renamed.png")
        self.photo.rename(renamed)
        self.assertIn("1 already synced", self.sync())
        self.assertIn((str(renamed), "existing-art"), self.store.upload_paths(host))
        self.assertFalse(self.art.sent)
        self.store = Store(self.root / "state")
        with patch.object(core, "_prepare_photo", side_effect=AssertionError("unexpected encoding")):
            self.assertIn("1 already synced", self.sync())

    def test_original_cache_entry_can_skip_encoding_after_upgrade(self):
        self.save_profiled_photo()
        with self.photo.open("rb") as source:
            legacy = hashlib.sha256(core._prepare_photo(source, None, legacy=True)).hexdigest()
        with self.store.connect() as db:
            db.execute("INSERT OR REPLACE INTO prepared_digests VALUES (?, ?, ?, ?, ?)",
                       (str(self.photo), core._source_revision(self.photo.stat()), "null",
                        core.LEGACY_ENCODER_VERSION, legacy))
        self.store.record("192.168.1.50", legacy, self.photo.name, "existing-art")
        with patch.object(core, "_prepare_photo", side_effect=AssertionError("unexpected encoding")):
            self.assertIn("1 already synced", self.sync())

    def test_legacy_removal_is_preserved_until_confirmed_resend(self):
        self.save_profiled_photo()
        with self.photo.open("rb") as source:
            legacy = hashlib.sha256(core._prepare_photo(source, None, legacy=True)).hexdigest()
        host = "192.168.1.50"
        self.store.record(host, legacy, self.photo.name, "removed-photo")
        self.store.mark_removing(host, "removed-photo")
        self.store.confirm_removal(host, "removed-photo")
        self.assertIn("1 previously removed", self.sync())
        self.assertFalse(self.art.sent)
        result = sync_files(self.art, self.store, host, [self.photo], self.cancelled,
                            lambda *_: None, resend=True)
        self.assertIn("1 added", result)
        self.assertEqual(self.art.sent, [prepare_photo(self.photo)])
        self.assertFalse(self.store.suppressed(host))
        self.assertIn("1 already synced", self.sync())

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

import io
from pathlib import Path
import tempfile
from threading import Event
import unittest
from unittest.mock import Mock, patch

from PIL import Image

from framesync.core import Store, sync_files, sync_folder
from framesync.gallery import frame_page, remove_artwork, show_artwork


class GalleryTV:
    def __init__(self):
        self.ids = ["builtin"]
        self.current = "builtin"
        self.deleted = []
        self.uploads = 0
        self.lose_delete_reply = False
        self.reject_delete = False
        self.fallback = False
        self.thumb = io.BytesIO()
        Image.new("RGB", (20, 10), "blue").save(self.thumb, "PNG")

    def available(self):
        return [dict(content_id=cid, category_id="MY-C0002") for cid in self.ids]

    def get_current(self):
        return dict(content_id=self.current)

    def get_thumbnail_list(self, ids):
        if self.fallback:
            raise RuntimeError("unsupported")
        return {cid + ".jpg": self.thumb.getvalue() for cid in ids}

    def get_thumbnail(self, ids, as_dict):
        return {cid: self.thumb.getvalue() for cid in ids}

    def select_image(self, cid, *, category, show):
        assert category == "MY-C0002" and show
        self.current = cid

    def delete(self, cid):
        self.deleted.append(cid)
        if self.reject_delete:
            return False
        self.ids.remove(cid)
        if self.lose_delete_reply:
            raise TimeoutError("reply lost")
        return True

    def upload(self, payload, **kwargs):
        self.uploads += 1
        cid = f"MY_F{self.uploads}"
        self.ids.append(cid)
        return cid


class GalleryTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.store = Store(self.root / "state")
        self.art = GalleryTV()
        self.folder = self.root / "photos"
        self.folder.mkdir()
        self.photo = self.folder / "flower.png"
        Image.new("RGB", (32, 20), "pink").save(self.photo)
        self.host = "192.168.1.10"

    def sync(self, host=None):
        return sync_folder(self.art, self.store, host or self.host, self.folder, Event(), lambda *_: None)

    def identity(self, cid):
        page = frame_page(self.art, self.store, self.host)
        return next(row["identity"] for row in page.rows if row["cid"] == cid)

    def test_page_navigation_cache_expires_and_invalidates_after_changes(self):
        from framesync.app import Controller
        from framesync.gallery import PAGE_CACHE_SECONDS

        controller = Controller(self.store)
        gallery = controller.gallery
        self.art.ids += [f"photo-{index}" for index in range(15)]

        controller.paired = True
        task = Mock(store=self.store, host=self.host, cancelled=Event())

        def run(operation, finished, message, **kwargs):
            finished(operation(self.art, task))
            return True

        with patch.object(controller, "start_task", side_effect=run) as tv, \
                patch("framesync.gallery.monotonic", return_value=0) as clock, \
                patch.object(self.art, "available", wraps=self.art.available) as listing, \
                patch.object(self.art, "get_thumbnail_list", wraps=self.art.get_thumbnail_list) as thumbs:
            gallery.openPage()
            listing.assert_called_once()
            thumbs.assert_called_once_with(self.art.ids[:12])
            self.assertEqual(gallery.rows[0]["cid"], "builtin")
            self.assertEqual(gallery.state["pages"], 2)

            clock.return_value = 119
            gallery.goToPage(1)
            listing.assert_called_once()
            thumbs.assert_called_with(self.art.ids[12:])
            self.assertEqual(gallery.rows[0]["cid"], "photo-11")
            gallery.goToPage(0)
            self.assertEqual(tv.call_count, 2)
            self.assertEqual(thumbs.call_count, 2)

            clock.return_value = PAGE_CACHE_SECONDS
            gallery.goToPage(1)
            self.assertEqual(listing.call_count, 2)
            self.assertEqual(thumbs.call_count, 3)
            thumbs.assert_called_with(self.art.ids[12:])

            gallery.refresh()
            self.assertEqual(listing.call_count, 3)
            self.assertEqual(thumbs.call_count, 4)
            controller.frameSettings.artworkChanged.emit("photo-0")
            gallery.openPage()
            self.assertEqual(listing.call_count, 4)
            gallery.reset()
            gallery.openPage()
            self.assertEqual(listing.call_count, 5)
            thumbs.assert_called_with(self.art.ids[:12])

    def test_current_thumbnail_and_show(self):
        self.sync()
        snapshot = frame_page(self.art, self.store, self.host)
        row = snapshot.rows[-1]
        self.assertEqual(row["name"], self.photo.name)
        self.assertTrue(row["thumb"].startswith("data:image/jpeg;base64,"))
        self.assertTrue(snapshot.rows[0]["current"])
        self.art.fallback = True
        snapshot = show_artwork(self.art, self.store, self.host, 0, row["cid"])
        self.assertTrue(snapshot.rows[-1]["current"])
        self.assertTrue(snapshot.rows[-1]["thumb"])

    def test_unidentified_server_slideshow_entry_is_omitted_before_paging(self):
        entry = dict(content_id="SAM-S4509", category_id="MY-C0008", content_type="server",
                     slideshow="true", image_date="", width=3840, height=2160)
        photos = [dict(content_id=f"external-{i}", category_id="MY-C0002") for i in range(11)]
        titled = dict(entry, content_id="other-server-work", title="Named artwork")
        self.art.available = lambda: [entry, *photos, titled]
        page = frame_page(self.art, self.store, self.host, 1)
        self.assertEqual(page.page, 0)
        self.assertEqual(len(page.rows), 12)
        self.assertNotIn("SAM-S4509", page.ids)
        self.assertEqual(page.rows[-1]["name"], "Named artwork")

    def test_gallery_uses_one_row_per_id_and_selects_from_my_photos(self):
        # This Frame lists the same photo twice in MY-C0008 and once in My Photos.
        # The copy in My Photos can arrive on a later page of the raw list.
        ids = [f"MY_F{i:04}" for i in range(17)]
        listed = [{"content_id": ids[0], "category_id": "MY-C0008"}] * 2
        listed += [{"content_id": cid, "category_id": "MY-C0002"} for cid in ids[1:] + ids[:1]]
        self.art.available = lambda: listed
        first = frame_page(self.art, self.store, self.host)
        second = frame_page(self.art, self.store, self.host, 1)
        self.assertEqual([row["cid"] for row in first.rows + second.rows], ids)
        self.assertEqual(first.ids, set(ids))
        shown = show_artwork(self.art, self.store, self.host, 0, ids[0])
        self.assertTrue(shown.rows[0]["current"])

    def test_confirmed_removal_is_per_frame_and_explicit_resend_restores(self):
        self.sync()
        cid = self.art.ids[-1]
        identity = self.identity(cid)
        remove_artwork(self.art, self.store, self.host, 0, cid, identity)
        self.assertEqual(self.art.deleted, [cid])
        self.assertTrue(self.photo.exists())
        self.store = Store(self.store.root)
        self.assertIn("1 previously removed", self.sync())
        self.assertIn("1 added", self.sync("192.168.1.11"))
        snapshots = []
        sent = sync_files(self.art, self.store, self.host, [self.photo], Event(), lambda *_: None,
                          resend=True, snapshot=snapshots.append)
        self.assertIn("1 added", sent)
        self.assertEqual(len(snapshots), 2)
        self.assertNotIn(self.art.ids[-1], snapshots[0])
        self.assertIn(self.art.ids[-1], snapshots[-1])
        self.assertFalse(self.store.suppressed(self.host))
        self.assertIn("1 already synced", self.sync())

    def test_lost_delete_ack_preserves_intent_without_automatic_retry(self):
        self.sync()
        cid = self.art.ids[-1]
        identity = self.identity(cid)
        self.art.lose_delete_reply = True
        with self.assertRaisesRegex(RuntimeError, "confirm removal"):
            remove_artwork(self.art, self.store, self.host, 0, cid, identity)
        self.store = Store(self.store.root)
        self.assertIn("1 previously removed", self.sync())
        remove_artwork(self.art, self.store, self.host, 0, cid, identity)
        self.assertEqual(self.art.deleted, [cid])

    def test_tv_refusal_is_not_a_success(self):
        self.sync()
        self.art.reject_delete = True
        cid = self.art.ids[-1]
        identity = self.identity(cid)
        with self.assertRaisesRegex(RuntimeError, "still on the Frame"):
            remove_artwork(self.art, self.store, self.host, 0, cid, identity)
        self.assertFalse(self.store.suppressed(self.host))

        previous = self.store.mark_removing(self.host, cid)
        with self.assertRaisesRegex(RuntimeError, "still on the Frame"):
            remove_artwork(self.art, self.store, self.host, 0, cid, identity)
        self.assertEqual(self.store.suppressed(self.host), set(previous))

    def test_recycled_id_with_same_metadata_does_not_delete_replacement(self):
        self.sync()
        cid = self.art.ids[-1]
        original_identity = self.identity(cid)
        self.art.thumb = io.BytesIO()
        Image.new("RGB", (20, 10), "red").save(self.art.thumb, "PNG")
        refreshed = remove_artwork(self.art, self.store, self.host, 0, cid, original_identity)
        self.assertFalse(self.art.deleted)
        self.assertFalse(self.store.suppressed(self.host))
        self.assertNotEqual(refreshed.rows[-1]["identity"], original_identity)
        self.assertIn("Review", refreshed.message)

    def test_journal_maps_renamed_file_and_each_crop_version_to_upload(self):
        self.sync()
        first = self.art.ids[-1]
        copy = self.folder / "also-flower.png"
        copy.write_bytes(self.photo.read_bytes())
        self.sync()
        Image.new("RGB", (32, 20), "blue").save(self.photo)
        self.sync()
        second = self.art.ids[-1]
        self.assertCountEqual(self.store.upload_paths(self.host),
                              [(str(self.photo), first), (str(copy), first), (str(self.photo), second)])

    def test_thumbnail_timeout_propagates_instead_of_falling_back(self):
        from framesync.gallery import thumbnails

        self.art.get_thumbnail_list = Mock(side_effect=TimeoutError("timed out"))
        self.art.get_thumbnail = Mock()
        with self.assertRaises(TimeoutError):
            thumbnails(self.art, ["builtin"])
        self.art.get_thumbnail.assert_not_called()

    def test_thumbnail_timeout_after_confirmed_delete_or_show_still_succeeds(self):
        self.sync()
        cid = self.art.ids[-1]
        identity = self.identity(cid)
        self.art.get_thumbnail_list = Mock(side_effect=TimeoutError("thumbs timed out"))
        shown = show_artwork(self.art, self.store, self.host, 0, cid)
        self.assertEqual(shown.current, cid)
        self.assertEqual(shown.rows[-1]["thumb"], "")

        # Restore thumbnail read for pre-delete identity verification, then fail only post-delete refresh.
        calls = {"count": 0}
        thumb_bytes = self.art.thumb.getvalue()

        def fail_second_thumb(ids):
            calls["count"] += 1
            if calls["count"] == 1:
                return {item + ".jpg": thumb_bytes for item in ids}
            raise TimeoutError("post-delete thumbs timed out")

        self.art.get_thumbnail_list = fail_second_thumb
        removed = remove_artwork(self.art, self.store, self.host, 0, cid, identity)
        self.assertNotIn(cid, removed.ids)
        self.assertIn("Removed", removed.message)

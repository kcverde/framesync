import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

from framesync import museum


class MuseumTests(unittest.TestCase):
    def test_featured_collections_have_valid_offline_previews_and_fits(self):
        from urllib.parse import unquote, urlsplit

        works = museum.featured_works()
        self.assertEqual({w["source"] for w in works[:3]}, set(museum.SOURCES))
        self.assertEqual(len(works), len({w["key"] for w in works}))
        for work in works:
            with self.subTest(key=work["key"]):
                self.assertEqual(work["key"], f"{work['source']}:{work['id']}")
                self.assertGreater(min(work["width"], work["height"]), 0)
                self.assertTrue(work["original"].startswith("https://"))
                url = urlsplit(work["preview"])
                self.assertEqual(url.scheme, "file")
                with Image.open(unquote(url.path)) as img:
                    img.verify()
        for source in museum.SOURCES:
            self.assertEqual(museum.featured_works(source), [w for w in works if w["source"] == source])

    def test_search_skips_unavailable_images_and_advances_by_ids_examined(self):
        record = dict(objectID=7, isPublicDomain=True, primaryImage="original.jpg",
                      primaryImageSmall="preview.jpg", title="Garden", artistDisplayName="Artist", objectDate="1880")
        responses = [{"total": 30, "objectIDs": [6, 7]}, {**record, "isPublicDomain": False}, record]
        with patch.object(museum, "fetch_json", side_effect=responses) as fetch:
            page = museum.search("garden & trees", 12, lambda: False)
        self.assertIn("q=garden+%26+trees", fetch.call_args_list[0].args[0])
        self.assertEqual([work["id"] for work in page.works], [7])
        self.assertEqual(page.next_offset, 14)
        self.assertTrue(page.more)

    def test_import_reuses_identical_file_and_preserves_name_collision(self):
        jpeg = io.BytesIO()
        Image.new("RGB", (50, 80), "blue").save(jpeg, format="JPEG")
        work = dict(title="Portrait / study", id=7, original="https://example.org/image.jpg")
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            museum, "urlopen", side_effect=lambda *a, **kw: io.BytesIO(jpeg.getvalue())
        ):
            folder = Path(tmp)
            target = museum.import_work(work, folder, lambda: False)
            self.assertEqual(target.parent, folder)
            self.assertEqual(museum.import_work(work, folder, lambda: False), target)
            target.write_bytes(b"existing photo")
            with self.assertRaisesRegex(ValueError, "already exists"):
                museum.import_work(work, folder, lambda: False)
            self.assertEqual(target.read_bytes(), b"existing photo")
            self.assertEqual(list(folder.iterdir()), [target])

    def test_cancelled_or_invalid_download_leaves_folder_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            for cancelled, error in [(True, InterruptedError), (False, OSError)]:
                with patch.object(museum, "urlopen", return_value=io.BytesIO(b"broken")):
                    with self.assertRaises(error):
                        museum.import_work(dict(id=1, title="Test", original="https://example.org/image.jpg"), folder, lambda: cancelled)
                self.assertEqual(list(folder.iterdir()), [])

    def test_cleveland_uses_delivered_image_dimensions_and_applies_fit(self):
        item = dict(id=8, accession_number="2005.8", share_license_status="CC0", title="River",
                    creators=[{"description": "Artist"}], creation_date="1780", images={
                        "web": {"url": "preview", "width": "600", "height": "450"},
                        "print": {"url": "large", "width": "3900", "height": "3000"},
                        "full": {"url": "huge", "width": "5000", "height": "3990", "filesize": str(200 << 20)},
                    })
        page = dict(info={"total": 30}, data=[item, {**item, "share_license_status": "Copyrighted"}])
        form = dict(source="cleveland", artist="Turner", medium="oil", start="1770", end="1800",
                    orientation="Landscape", fits=True)
        with patch.object(museum, "fetch_json", return_value=page) as fetch:
            result = museum.search("river", 12, lambda: False, filters=museum.Filters.from_form(form))
        self.assertEqual((result.next_offset, result.more), (14, True))
        work, = result.works
        self.assertEqual((work["key"], work["original"], work["width"]), ("cleveland:8", "large", 3900))
        for value in ("skip=12", "artists=Turner", "medium=oil", "created_after=1769", "created_before=1801"):
            self.assertIn(value, fetch.call_args.args[0])

    def test_chicago_search_respects_rights_and_requested_size(self):
        from urllib.parse import urlparse, parse_qs
        import json
        item = dict(id=7, title="Garden", image_id="img-id", date_display="1900",
                    artist_title="Monet", is_public_domain=True, thumbnail={"width": 6100, "height": 5000})
        page = dict(data=[item, {**item, "is_public_domain": False}], config={"iiif_url": "https://cdn.test"},
                    pagination={"current_page": 2, "total_pages": 3})
        with patch.object(museum, "fetch_json", return_value=page) as fetch:
            result = museum.search("garden", 12, lambda: False,
                                   filters=museum.Filters(source="chicago", artist="Monet", field="Title"))
        params = json.loads(parse_qs(urlparse(fetch.call_args_list[0].args[0]).query)["params"][0])
        self.assertEqual(params["page"], 2)
        self.assertIn({"term": {"is_public_domain": True}}, params["query"]["bool"]["filter"])
        self.assertIn({"match": {"artist_title": {"query": "Monet", "operator": "and"}}}, params["query"]["bool"]["filter"])
        work, = result.works
        self.assertEqual(work["key"], "chicago:7")
        self.assertTrue(work["original"].endswith("/full/1686,/0/default.jpg"))
        self.assertEqual(work["width"], 0)  # Source metadata describes a different rendition.
        self.assertTrue(result.more)

    def test_chicago_chooses_public_domain_jpeg_matched_by_museum_id(self):
        item = dict(id=8, title="River", image_id="image", is_public_domain=True)
        page = dict(data=[item], config={"iiif_url": "https://museum.test"},
                    pagination={"current_page": 1, "total_pages": 1})
        ids = {"results": {"bindings": [
            {"id": {"value": "8"}, "image": {"value": "http://commons.wikimedia.org/wiki/Special:FilePath/River.jpg"}},
        ]}}
        image = dict(url="original", width=5000, height=3000, size=60000, mime="image/jpeg",
                     thumburl="preview", descriptionurl="credit",
                     extmetadata={"LicenseShortName": {"value": "Public domain"}, "Copyrighted": {"value": "False"}})
        images = {"query": {"pages": {"99": {"title": "File:River.jpg", "imageinfo": [image]}}}}
        sizes = {"query": {"pages": {"99": {"imageinfo": [{"thumburl": "rendition", "thumbwidth": 3840,
                                                            "thumbheight": 2304}]}}}}
        for license_name, allowed in [("Public domain", True), ("CC BY", False)]:
            image["extmetadata"]["LicenseShortName"]["value"] = license_name
            with patch.object(museum, "fetch_json", side_effect=[page, ids, images, sizes]):
                result = museum.search("", 0, lambda: False, filters=museum.Filters(source="chicago", fits=True))
            if allowed:
                work, = result.works
                self.assertEqual((work["key"], work["preview"], work["original"], work["width"], work["height"]),
                                 ("chicago:8", "preview", "rendition", 3840, 2304))
            else:
                self.assertEqual(result.works, [])

    def test_chicago_import_downloaded_png_keeps_museum_filename(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            original = folder / "source.png"
            Image.new("RGB", (200, 100), "blue").save(original)
            work = dict(source="chicago", id=123, title="Garden")
            with patch.object(museum, "urlopen") as download:
                target = museum.import_work(work, folder, lambda: False, original)
            download.assert_not_called()
            self.assertEqual(target.name, "Garden (AIC 123).jpg")
            with Image.open(target) as image:
                self.assertEqual((image.size, image.format), ((200, 100), "JPEG"))
            self.assertTrue(original.is_file())

    def test_exif_dimensions_cached_and_4k_fit_boundaries(self):
        from framesync.core import Store
        image = io.BytesIO()
        orientation = Image.Exif()
        orientation[274] = 6
        Image.new("RGB", (150, 100)).save(image, "JPEG", exif=orientation)
        with tempfile.TemporaryDirectory() as tmp:
            library = museum.MuseumLibrary(Store(Path(tmp)))
            with patch.object(museum, "urlopen", return_value=io.BytesIO(image.getvalue())) as download:
                for _ in range(2):
                    self.assertEqual(library.dimensions("https://example.org/source.jpg", lambda: False), (100, 150))
                download.assert_called_once()
        for size, label in [((3840, 2160), "Exact 4K"), ((3840, 3000), "4K crop possible"),
                            ((3881, 1972), "Needs upscaling"), ((0, 0), "Dimensions unavailable")]:
            self.assertEqual(museum.fit_label(*size), label)

    def test_saved_works_and_museum_badge_survive_restart(self):
        from framesync.core import Store
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "state")
            library = museum.MuseumLibrary(store)
            image = io.BytesIO()
            Image.new("RGB", (32, 64), "blue").save(image, "TIFF")
            work = dict(source="cleveland", key="cleveland:7", id=7, title="Tree", original="https://test.org/tree")
            library.toggle(work)
            with patch.object(museum, "urlopen", return_value=io.BytesIO(image.getvalue())):
                target = museum.import_work(work, root, lambda: False)
            with Image.open(target) as imported:
                self.assertEqual((imported.format, imported.size), ("JPEG", (32, 64)))
            library.remember_import(target, work["key"])
            store.record("tv", "full", target.name, "full-id", path=target)
            store.record("tv", "crop", target.name, "crop-id", path=target)
            store.record("tv", "old", "Garden (Met 7).jpg", "legacy-id")
            library = museum.MuseumLibrary(Store(root / "state"))
            self.assertEqual(library.saved(), {"cleveland:7": work})
            self.assertEqual(library.keys_on_tv("tv", {"crop-id", "legacy-id"}), {"cleveland:7", "met:7"})
            store.record("tv", "old", "Garden (Met 7).jpg", "legacy-id", path=root / "Garden (Met 7).jpg")
            self.assertEqual(library.keys_on_tv("tv", {"legacy-id"}), {"met:7"})
            self.assertEqual(library.keys_on_tv("tv", {"missing"}), set())
            self.assertEqual(library.keys_on_tv("other tv", {"crop-id"}), set())
            library.toggle(work)
            self.assertEqual(library.saved(), {})

    def test_museum_request_requires_https_and_skips_missing_commons_pages(self):
        import stat
        from framesync.museum_providers import _HttpsOnlyRedirectHandler, chicago_reproductions, met_artwork, museum_request

        for bad_url in ("file:///etc/passwd", "http://example.org/image.jpg", "ftp://example.org/a.jpg", "https://"):
            with self.subTest(url=bad_url), self.assertRaises(ValueError):
                museum_request(bad_url)
        req = museum_request("https://example.org/image.jpg")
        self.assertIn("https://github.com/kcverde/framesync", req.get_header("User-agent"))

        # Redirects to non-HTTPS schemes must be rejected.
        handler = _HttpsOnlyRedirectHandler()
        for bad_redirect in ("http://example.org/a.jpg", "ftp://example.org/a.jpg", "file:///etc/passwd"):
            with self.subTest(redirect=bad_redirect), self.assertRaises(ValueError):
                handler.redirect_request(req, None, 302, "Found", {}, bad_redirect)

        # Optional fields omitted by Met API should fall back cleanly instead of raising KeyError.
        minimal = met_artwork({"objectID": 42, "isPublicDomain": True, "primaryImage": "https://example.org/a.jpg"})
        self.assertEqual((minimal["title"], minimal["artist"], minimal["preview"]),
                         ("Untitled", "Unknown artist", "https://example.org/a.jpg"))

        # Missing Commons pages (negative page IDs) must not trigger a second Commons request.
        works = [dict(id=8, width=0, height=0)]
        responses = [
            {"results": {"bindings": [{"id": {"value": "8"}, "image": {"value": "https://commons.wikimedia.org/wiki/Special:FilePath/Missing.jpg"}}]}},
            {"query": {"pages": {"-1": {"title": "File:Missing.jpg", "missing": ""}}}},
        ]
        calls = []

        def fake_fetch(url):
            calls.append(url)
            return responses.pop(0)

        chicago_reproductions(works, fake_fetch, lambda: False)
        self.assertEqual(len(calls), 2)

        # Redirecting an alias to a canonical title must merge works instead of overwriting them.
        both = [dict(id=1, width=0, height=0), dict(id=2, width=0, height=0)]
        image = dict(url="https://upload.wikimedia.org/canon.jpg", width=4000, height=3000, size=50000,
                     mime="image/jpeg", thumburl="https://upload.wikimedia.org/preview.jpg",
                     descriptionurl="https://commons.wikimedia.org/wiki/File:Canon.jpg",
                     extmetadata={"LicenseShortName": {"value": "Public domain"}, "Copyrighted": {"value": "False"}})
        merge_responses = [
            {"results": {"bindings": [
                {"id": {"value": "1"}, "image": {"value": "https://commons.wikimedia.org/wiki/Special:FilePath/Alias.jpg"}},
                {"id": {"value": "2"}, "image": {"value": "https://commons.wikimedia.org/wiki/Special:FilePath/Canon.jpg"}},
            ]}},
            {"query": {
                "redirects": [{"from": "File:Alias.jpg", "to": "File:Canon.jpg"}],
                "pages": {"77": {"title": "File:Canon.jpg", "imageinfo": [image]}},
            }},
            {"query": {"pages": {"77": {"imageinfo": [{"thumburl": "https://upload.wikimedia.org/3840.jpg", "thumbwidth": 3840, "thumbheight": 2880}]}}}},
        ]
        chicago_reproductions(both, lambda url: merge_responses.pop(0), lambda: False)
        self.assertEqual([w["original"] for w in both], ["https://upload.wikimedia.org/3840.jpg"] * 2)

        # Overwriting an existing private JPEG must preserve its 0600 permissions.
        jpeg = io.BytesIO()
        Image.new("RGB", (50, 80), "blue").save(jpeg, format="JPEG")
        work = dict(key="met:7", source="met", id=7, title="Private", original="https://example.org/image.jpg")
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            museum, "urlopen", side_effect=lambda *a, **kw: io.BytesIO(jpeg.getvalue())
        ):
            preview = museum.preview_work(work, lambda: False)
            try:
                target = Path(tmp) / "private.jpg"
                target.write_bytes(b"old")
                target.chmod(0o600)
                preview.save_copy(target, (1.0, 0.5, 0.5), lambda: False)
                self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
            finally:
                preview.directory.cleanup()

    def test_socket_backed_fetch_json_enforces_total_deadline_and_shutdown_abort(self):
        import socket
        import threading
        import time
        from framesync import museum_providers

        class SocketResponse:
            def __init__(self, sock):
                self.fp = sock.makefile("rb")
                self._sock = sock

            def read(self, size=-1):
                return self.fp.read(size)

            def close(self):
                self.fp.close()
                self._sock.close()

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.close()

        client, server = socket.socketpair()
        try:
            client.settimeout(5.0)
            server.sendall(b'{"partial": ')
            with patch.object(museum_providers, "JSON_DEADLINE_SECONDS", 0.12), \
                    patch.object(museum_providers, "urlopen", return_value=SocketResponse(client)):
                started = time.monotonic()
                with self.assertRaises(TimeoutError):
                    museum_providers.fetch_json("https://example.org/slow.json")
                self.assertLess(time.monotonic() - started, 0.6)
        finally:
            server.close()

        # Cancellation must also wake a thread blocked on a real socket read immediately.
        client2, server2 = socket.socketpair()
        entered = threading.Event()
        errors = []
        try:
            client2.settimeout(5.0)
            server2.sendall(b'{"partial": ')

            def reader(cancelled):
                entered.set()
                try:
                    museum_providers.fetch_json("https://example.org/blocked.json")
                except Exception as exc:
                    errors.append(exc)

            with patch.object(museum_providers, "urlopen", return_value=SocketResponse(client2)):
                task = museum.MuseumTask(reader)
                task.start()
                self.assertTrue(entered.wait(2))
                time.sleep(0.05)
                started = time.monotonic()
                task.requestInterruption()
                self.assertTrue(task.wait(1000))
                self.assertLess(time.monotonic() - started, 0.5)
                self.assertTrue(any(isinstance(e, InterruptedError) for e in errors))
        finally:
            server2.close()

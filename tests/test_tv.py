"""Exercise actual samsungtvws handshake code against a simulated TV socket."""
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from samsungtvws.art import SamsungTVArt
from samsungtvws.exceptions import ConnectionFailure

from framesync.core import Store
from framesync.tv import FrameArt, artwork_connection


class TVSocket:
    def __init__(self, events):
        self.events = events
        self.closed = False

    def abort(self):
        self.close()

    def recv(self):
        return json.dumps(self.events.pop(0))

    def close(self):
        self.closed = True


class PairingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name))
        self.host = "192.168.1.50"
        self.calls = []
        self.sockets = []

    def socket(self, url, timeout, **kwargs):
        endpoint = urlparse(url).path.rsplit("/", 1)[-1]
        token = parse_qs(urlparse(url).query, keep_blank_values=True).get("token")
        self.calls.append((endpoint, token, timeout))
        if endpoint == "samsung.remote.control":
            events = [{"event": "ms.channel.connect", "data": {"token": "fake-pairing-token"}}]
        elif token == ["fake-pairing-token"]:
            events = [{"event": "ms.channel.connect"}, {"event": "ms.channel.ready"}]
        else:
            events = [{"event": "ms.channel.timeOut"}]
        socket = TVSocket(events)
        self.sockets.append(socket)
        return socket

    def test_original_art_only_handshake_reproduces_timeout(self):
        with patch("samsungtvws.connection.websocket.create_connection", self.socket):
            with self.assertRaisesRegex(ConnectionFailure, "ms.channel.timeOut"):
                SamsungTVArt(self.host, port=8002).open()

    def test_blank_token_pairs_remote_then_opens_art_and_reuses_token_after_restart(self):
        Path(self.store.token_file(self.host)).write_text("")
        updates = []
        with patch("samsungtvws.connection.websocket.create_connection", self.socket):
            with artwork_connection(self.store, self.host, 8002, updates.append):
                self.assertEqual(self.store.load_token(self.host), "fake-pairing-token")
            self.assertEqual(self.calls, [
                ("samsung.remote.control", None, 60),
                ("com.samsung.art-app", ["fake-pairing-token"], 30),
            ])
            self.assertTrue(all(socket.closed for socket in self.sockets))
            self.calls.clear()
            with artwork_connection(Store(self.store.root), self.host, 8002, updates.append):
                pass
            self.assertEqual(self.calls, [("com.samsung.art-app", ["fake-pairing-token"], 30)])
        self.assertEqual(Path(self.store.token_file(self.host)).stat().st_mode & 0o777, 0o600)

    def test_denied_pairing_stops_before_art_and_does_not_save_token(self):
        def rejected(url, timeout, **kwargs):
            self.calls.append(url)
            return TVSocket([{"event": "ms.channel.unauthorized"}])
        with patch("samsungtvws.connection.websocket.create_connection", rejected):
            with self.assertRaisesRegex(RuntimeError, "TV pairing did not complete"):
                with artwork_connection(self.store, self.host, 8002, lambda text: None):
                    self.fail("Must not open artwork after denied pairing")
        self.assertEqual(len(self.calls), 1)
        self.assertIsNone(self.store.load_token(self.host))

    def test_art_failure_keeps_successful_pairing_for_next_attempt(self):
        def fail_art(url, timeout, **kwargs):
            if "com.samsung.art-app" in url:
                return TVSocket([{"event": "ms.channel.timeOut"}])
            return self.socket(url, timeout, **kwargs)
        with patch("samsungtvws.connection.websocket.create_connection", fail_art):
            with self.assertRaisesRegex(RuntimeError, "artwork channel"):
                with artwork_connection(self.store, self.host, 8002, lambda text: None):
                    pass
        self.assertEqual(self.store.load_token(self.host), "fake-pairing-token")

    def test_client_presence_before_art_ready(self):
        events = [{"event": event} for event in (
            "ms.channel.connect", "ms.channel.clientConnect", "ms.channel.clientDisconnect", "ms.channel.ready")]
        self.store.save_token(self.host, "saved")
        sock = TVSocket(events)
        with patch("samsungtvws.connection.websocket.create_connection", return_value=sock):
            with artwork_connection(self.store, self.host, 8002, lambda _: None):
                self.assertFalse(events)
        self.assertTrue(sock.closed)

    def test_authenticated_but_silent_art_service_times_out(self):
        aborted = threading.Event()

        class SilentArt(TVSocket):
            def recv(self):
                if self.events:
                    return super().recv()
                if not aborted.wait(2):
                    raise AssertionError("Ready wait was never interrupted")
                raise OSError("Socket closed")

            def abort(self):
                aborted.set()

        sock = SilentArt([{"event": "ms.channel.connect"}])
        with patch("samsungtvws.connection.websocket.create_connection", return_value=sock):
            art = FrameArt(self.host, port=8002, token="saved", timeout=0.05)
            try:
                with self.assertRaisesRegex(RuntimeError, "TV connected, but its artwork service"):
                    art.open()
                self.assertTrue(aborted.is_set())
            finally:
                art.close()

    def test_artwork_list_read_has_a_deadline_after_ready(self):
        aborted = threading.Event()
        art = FrameArt(self.host, port=8002, token="saved", timeout=0.05)
        sock = TVSocket([{"event": "ms.channel.connect"}, {"event": "ms.channel.ready"}])
        sock.abort = aborted.set

        def silent_library(*args):
            if aborted.wait(0.5):
                raise OSError("Socket closed")
            self.fail("Artwork list read was never interrupted")

        with patch("samsungtvws.connection.websocket.create_connection", return_value=sock):
            try:
                art.open()
                with patch.object(SamsungTVArt, "available", silent_library):
                    with self.assertRaisesRegex(TimeoutError, "artwork list"):
                        art.available()
            finally:
                art.close()

    def test_legacy_port_does_not_require_remote_pairing(self):
        def legacy(url, timeout, **kwargs):
            self.calls.append(url)
            return TVSocket([{"event": "ms.channel.connect"}, {"event": "ms.channel.ready"}])
        with patch("samsungtvws.connection.websocket.create_connection", legacy):
            with artwork_connection(self.store, self.host, 8001, lambda text: None):
                pass
        self.assertEqual(len(self.calls), 1)
        self.assertIn("com.samsung.art-app", self.calls[0])

"""Exercise actual samsungtvws handshake code against a simulated TV socket."""
import base64
import hashlib
import json
import shutil
import socket
import ssl
import subprocess
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from samsungtvws.art import SamsungTVArt
from samsungtvws.exceptions import ConnectionFailure

from framesync.app import Task, read_library
from framesync.core import Store
from framesync import tv
from framesync.tv import FrameArt, ReadCancellation, artwork_connection, call_tv


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

    def test_upload_has_a_deadline(self):
        aborted = threading.Event()
        d2d_closed = threading.Event()
        art = FrameArt(self.host, port=8002, token="saved", timeout=0.05)
        sock = TVSocket([{"event": "ms.channel.connect"}, {"event": "ms.channel.ready"}])
        sock.abort = aborted.set

        class FakeD2DSocket:
            def settimeout(self, value):
                pass

            def shutdown(self, how):
                d2d_closed.set()

            def close(self):
                d2d_closed.set()

        def silent_upload(*args, **kwargs):
            with patch.object(SamsungTVArt, "_open_d2d_socket", return_value=FakeD2DSocket()):
                art._open_d2d_socket({"ip": self.host, "port": 1234})
            if not d2d_closed.wait(0.5):
                self.fail("D2D socket was never closed on upload deadline")
            return "late-content-id"

        with patch("samsungtvws.connection.websocket.create_connection", return_value=sock):
            try:
                art.open()
                with patch.object(SamsungTVArt, "upload", silent_upload):
                    with self.assertRaisesRegex(TimeoutError, "uploading artwork"):
                        art.upload(b"jpeg-bytes", file_type="JPEG")
                self.assertTrue(aborted.is_set())
                self.assertTrue(d2d_closed.is_set())
            finally:
                art.close()

    def test_call_tv_aborts_stalled_d2d_socket_for_thumbnails(self):
        from framesync.tv import call_tv

        d2d_closed = threading.Event()
        art = FrameArt(self.host, port=8002, token="saved", timeout=0.05)
        sock = TVSocket([{"event": "ms.channel.connect"}, {"event": "ms.channel.ready"}])

        class FakeD2DSocket:
            def settimeout(self, value):
                pass

            def shutdown(self, how):
                d2d_closed.set()

            def close(self):
                d2d_closed.set()

        def stalled_thumbs(ids):
            with patch.object(SamsungTVArt, "_open_d2d_socket", return_value=FakeD2DSocket()):
                art._open_d2d_socket({"ip": self.host, "port": 1234})
            if not d2d_closed.wait(0.5):
                self.fail("D2D socket was never aborted on call_tv deadline")
            raise OSError("D2D socket aborted")

        with patch("samsungtvws.connection.websocket.create_connection", return_value=sock):
            try:
                art.open()
                with patch.object(art, "get_thumbnail_list", side_effect=stalled_thumbs):
                    with self.assertRaisesRegex(TimeoutError, "timed out"):
                        call_tv(art, art.get_thumbnail_list, ["x"])
                self.assertTrue(d2d_closed.is_set())
            finally:
                art.close()

    def test_read_cancellation_releases_tv_lock_during_connection(self):
        for stage in ("upgrade", "samsung greeting", "art ready"):
            with self.subTest(stage=stage), socket.socket() as listener:
                listener.settimeout(2)
                listener.bind(("127.0.0.1", 0))
                listener.listen()
                lock = threading.Lock()
                host, port = listener.getsockname()
                task = Task(self.store, host, port, read_library, None, False, lock, read_only=True)
                task.start()
                try:
                    peer, _ = listener.accept()
                    with peer:
                        peer.settimeout(2)
                        request = b""
                        while b"\r\n\r\n" not in request:
                            part = peer.recv(4096)
                            self.assertTrue(part)
                            request += part
                        if stage != "upgrade":
                            key = next(line.split(b":", 1)[1].strip() for line in request.splitlines()
                                       if line.lower().startswith(b"sec-websocket-key:"))
                            accept = base64.b64encode(hashlib.sha1(
                                key + b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
                            ).digest())
                            peer.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                                         b"Connection: Upgrade\r\nSec-WebSocket-Accept: " + accept + b"\r\n\r\n")
                        if stage == "art ready":
                            greeting = b'{"event":"ms.channel.connect"}'
                            peer.sendall(bytes((0x81, len(greeting))) + greeting)
                        self.assertTrue(lock.locked())
                        task.cancel()
                        self.assertTrue(task.wait(1000))
                        self.assertEqual(task.error, "TV operation cancelled.")
                        self.assertTrue(lock.acquire(blocking=False))
                        lock.release()
                finally:
                    task.cancel()
                    task.wait()

    def test_partial_upgrade_headers_cannot_extend_connection_deadline(self):
        with socket.socket() as listener:
            listener.settimeout(2)
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            lock = threading.Lock()
            host, port = listener.getsockname()
            task = Task(self.store, host, port, read_library, None, False, lock, read_only=True)
            original = FrameArt

            def short_connection(*args, **kwargs):
                return original(*args, **{**kwargs, "timeout": 0.25})

            with patch("framesync.tv.FrameArt", side_effect=short_connection):
                task.start()
                try:
                    peer, _ = listener.accept()
                    with peer:
                        peer.settimeout(2)
                        self.assertIn(b"GET ", peer.recv(4096))
                        for _ in range(40):
                            try:
                                peer.sendall(b"a")
                            except OSError:
                                break
                            if task.wait(20):
                                break
                        self.assertTrue(task.wait(100))
                        self.assertIn("Timed out opening the Frame connection", task.error)
                        self.assertFalse(lock.locked())
                finally:
                    task.cancel()
                    task.wait()

    @unittest.skipUnless(shutil.which("openssl"), "OpenSSL is needed to generate a loopback TLS certificate")
    def test_secure_read_cancellation_releases_lock_and_closes_peer(self):
        key = self.store.root / "local.key"
        cert = self.store.root / "local.crt"
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                        "-subj", "/CN=localhost", "-days", "1", "-keyout", str(key), "-out", str(cert)],
                       check=True, capture_output=True, timeout=10)
        server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server.load_cert_chain(cert, key)
        connect = tv._connect_read_socket

        def secure_connect(address, timeout, secure, cancellation, register):
            return connect(address, timeout, True, cancellation, register)

        for stage in ("TLS handshake", "established TLS read"):
            with self.subTest(stage=stage), socket.socket() as listener:
                listener.settimeout(2)
                listener.bind(("127.0.0.1", 0))
                listener.listen()
                lock = threading.Lock()
                task = Task(self.store, *listener.getsockname(), read_library, None, False, lock, read_only=True)
                with patch.object(tv, "_connect_read_socket", side_effect=secure_connect):
                    task.start()
                    try:
                        peer, _ = listener.accept()
                        with peer:
                            peer.settimeout(2)
                            if stage == "TLS handshake":
                                self.assertEqual(peer.recv(1), b"\x16")  # ClientHello
                                task.cancel()
                                while peer.recv(4096):
                                    pass
                            else:
                                with server.wrap_socket(peer, server_side=True) as tls:
                                    self.assertIn(b"GET ", tls.recv(4096))
                                    task.cancel()
                                    self.assertEqual(tls.recv(4096), b"")
                            self.assertTrue(task.wait(1000))
                            self.assertEqual(task.error, "TV operation cancelled.")
                            self.assertFalse(lock.locked())
                    finally:
                        task.cancel()
                        task.wait()

    def test_cancel_wakes_a_thumbnail_read_on_a_d2d_socket(self):
        cancellation = ReadCancellation(threading.Event())
        art = FrameArt(self.host, timeout=5)
        art.read_cancellation = cancellation
        art.connection = TVSocket([])
        started = threading.Event()
        errors = []

        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            ip, port = listener.getsockname()

            def read_thumbnail():
                with art._open_d2d_socket({"ip": ip, "port": port}) as sock:
                    started.set()
                    art._recv_exact(sock, 4)

            def read():
                try:
                    call_tv(art, read_thumbnail)
                except Exception as exc:
                    errors.append(exc)

            with cancellation.watch(art._abort_d2d_sockets):
                worker = threading.Thread(target=read)
                worker.start()
                try:
                    self.assertTrue(started.wait(2))
                    cancellation.cancel()
                    worker.join(1)
                    self.assertFalse(worker.is_alive())
                finally:
                    cancellation.cancel()
                    worker.join()
        self.assertIsInstance(errors[0], (OSError, ConnectionFailure))

    def test_mutating_task_cancel_does_not_abort_its_connection(self):
        operation_started, release = threading.Event(), threading.Event()
        sock = TVSocket([{"event": "ms.channel.connect"}, {"event": "ms.channel.ready"}])

        def operation(art, task):
            operation_started.set()
            self.assertTrue(release.wait(2))
            return "confirmed"

        task = Task(self.store, self.host, 8001, operation, None, True, threading.Lock())
        with patch("samsungtvws.connection.websocket.create_connection", return_value=sock):
            task.start()
            try:
                self.assertTrue(operation_started.wait(2))
                task.cancel()
                self.assertTrue(task.cancelled.is_set())
                self.assertFalse(sock.closed)
            finally:
                release.set()
                self.assertTrue(task.wait(2000))
        self.assertEqual(task.output, "confirmed")
        self.assertFalse(task.error)

    def test_legacy_port_does_not_require_remote_pairing(self):
        def legacy(url, timeout, **kwargs):
            self.calls.append(url)
            return TVSocket([{"event": "ms.channel.connect"}, {"event": "ms.channel.ready"}])
        with patch("samsungtvws.connection.websocket.create_connection", legacy):
            with artwork_connection(self.store, self.host, 8001, lambda text: None):
                pass
        self.assertEqual(len(self.calls), 1)
        self.assertIn("com.samsung.art-app", self.calls[0])

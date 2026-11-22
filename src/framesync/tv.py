"""Pair on Samsung's remote channel before opening its separate artwork channel."""
from contextlib import ExitStack, contextmanager
import errno
from select import select
import socket
from threading import Event, Lock, Timer
from time import monotonic

import websocket
from samsungtvws import helper
from samsungtvws.art import SamsungTVArt
from samsungtvws.connection import SamsungTVWSConnection
from samsungtvws.event import (
    IGNORE_EVENTS_AT_STARTUP, MS_CHANNEL_CLIENT_CONNECT_EVENT, MS_CHANNEL_CLIENT_DISCONNECT_EVENT,
    MS_CHANNEL_CONNECT_EVENT, MS_CHANNEL_READY_EVENT, MS_CHANNEL_UNAUTHORIZED,
)
from samsungtvws.exceptions import ConnectionFailure, UnauthorizedError
from samsungtvws.remote import SamsungTVWS

from .core import Store


class ReadCancellation:
    """One read-only task owns these abort callbacks, including during connection setup."""

    def __init__(self, cancelled):
        self.cancelled = cancelled
        self._lock = Lock()
        self._callbacks = set()

    def check(self):
        if self.cancelled.is_set():
            raise InterruptedError("TV operation cancelled.")

    @contextmanager
    def watch(self, abort):
        with self._lock:
            self.check()
            self._callbacks.add(abort)
        try:
            yield
        finally:
            with self._lock:
                self._callbacks.remove(abort)

    def cancel(self):
        with self._lock:
            self.cancelled.set()
            for abort in self._callbacks:
                abort()


def _shutdown_socket(sock):
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass


def _connect_read_socket(address, timeout, secure, cancellation, register):
    """Expose TCP and TLS sockets to cancellation before waiting on either handshake."""
    deadline = monotonic() + timeout
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        register(sock)
        sock.setblocking(False)
        result = sock.connect_ex(address)
        if result in (errno.EINPROGRESS, errno.EWOULDBLOCK, errno.EALREADY):
            while True:
                cancellation.check()
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise TimeoutError("Timed out connecting to the Frame.")
                _, writable, failed = select([], [sock], [sock], min(remaining, 0.05))
                if writable or failed:
                    result = sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
                    break
        cancellation.check()
        if result:
            raise OSError(result, "Could not connect to the Frame.")
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise TimeoutError("Timed out connecting to the Frame.")
        sock.settimeout(remaining)
        if secure:
            sock = helper.get_ssl_context().wrap_socket(sock, do_handshake_on_connect=False)
            register(sock)
            sock.do_handshake()
        cancellation.check()
        return sock
    except BaseException:
        sock.close()
        raise


def _open_read_channel(channel, cancellation, resources):
    # The library publishes .connection only after receiving the Samsung greeting.
    # Supply our own socket so cancellation also covers TCP, TLS and pairing approval.
    def register(sock):
        resources.enter_context(cancellation.watch(lambda: _shutdown_socket(sock)))

    deadline = monotonic() + channel.timeout
    sock = _connect_read_socket((channel.host, channel.port), channel.timeout,
                                channel.port == 8002, cancellation, register)
    resources.callback(sock.close)
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise TimeoutError("Timed out opening the Frame connection.")
    with response_deadline(lambda: _shutdown_socket(sock), remaining) as expired:
        try:
            connection = websocket.create_connection(
                channel._format_websocket_url(channel.endpoint), remaining,
                socket=sock, connection="Connection: Upgrade", redirect_limit=0,
            )
            channel.connection = connection
            while True:
                cancellation.check()
                response = helper.process_api_response(connection.recv())
                event = response.get("event", "*")
                channel._websocket_event(event, response)
                if event not in IGNORE_EVENTS_AT_STARTUP:
                    break
        except Exception as exc:
            if expired.is_set():
                raise TimeoutError("Timed out opening the Frame connection.") from exc
            raise
        if expired.is_set():
            raise TimeoutError("Timed out opening the Frame connection.")
    cancellation.check()
    if event == MS_CHANNEL_UNAUTHORIZED:
        raise UnauthorizedError(response)
    if event != MS_CHANNEL_CONNECT_EVENT:
        raise ConnectionFailure(response)
    channel._check_for_token(response)
    return connection


@contextmanager
def response_deadline(abort, seconds, *, on_timeout=None):
    # Presence traffic and WebSocket pings can keep the socket alive indefinitely.
    expired = Event()

    def interrupt():
        expired.set()
        try:
            abort()
        except Exception:
            pass
        if on_timeout is not None:
            try:
                on_timeout()
            except Exception:
                pass

    timer = Timer(seconds, interrupt)
    timer.start()
    try:
        yield expired
    finally:
        timer.cancel()
        timer.join()


def call_tv(art, operation, *args, **kwargs):
    """Bound one Art Mode command on an open connection, including unsolicited traffic."""
    connection = getattr(art, "connection", None)
    if connection is None:
        return operation(*args, **kwargs)
    deadline_ctx = getattr(art, "_deadline", None)
    context = deadline_ctx(connection, art.timeout) if deadline_ctx else response_deadline(connection.abort, art.timeout)
    with context as expired:
        try:
            result = operation(*args, **kwargs)
        except Exception as exc:
            if expired.is_set():
                name = getattr(operation, "__name__", "command")
                raise TimeoutError(f"The Frame timed out during {name}.") from exc
            raise
        if expired.is_set():
            name = getattr(operation, "__name__", "command")
            raise TimeoutError(f"The Frame timed out during {name}.")
        return result


class FrameArt(SamsungTVArt):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._d2d_lock = Lock()
        self._d2d_sockets = set()
        self._op_deadline = None
        self._op_expired = None
        self.read_cancellation = None

    def _abort_d2d_sockets(self):
        with self._d2d_lock:
            sockets = list(self._d2d_sockets)
        for sock in sockets:
            try:
                sock.shutdown(2)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass

    @contextmanager
    def _deadline(self, connection, seconds):
        with response_deadline(connection.abort, seconds, on_timeout=self._abort_d2d_sockets) as expired:
            previous_deadline = self._op_deadline
            previous_expired = self._op_expired
            self._op_deadline = monotonic() + seconds
            self._op_expired = expired
            try:
                yield expired
            finally:
                self._op_deadline = previous_deadline
                self._op_expired = previous_expired
                with self._d2d_lock:
                    self._d2d_sockets.clear()

    def _open_d2d_socket(self, conn_info):
        deadline = self._op_deadline
        expired = self._op_expired
        previous_timeout = self.timeout
        if deadline is not None:
            remaining = deadline - monotonic()
            if remaining <= 0 or (expired is not None and expired.is_set()):
                raise TimeoutError("Timed out communicating with the Frame.")
            self.timeout = remaining
        try:
            if self.read_cancellation is not None:
                sock = _connect_read_socket(
                    (conn_info["ip"], int(conn_info["port"])), self.timeout,
                    conn_info.get("secured", False), self.read_cancellation, self._register_read_d2d,
                )
            else:
                sock = super()._open_d2d_socket(conn_info)
        finally:
            self.timeout = previous_timeout
        with self._d2d_lock:
            if expired is not None and expired.is_set():
                try:
                    sock.close()
                except OSError:
                    pass
                raise TimeoutError("Timed out communicating with the Frame.")
            self._d2d_sockets.add(sock)
        if deadline is not None:
            remaining = deadline - monotonic()
            if remaining <= 0:
                self._abort_d2d_sockets()
                raise TimeoutError("Timed out communicating with the Frame.")
            try:
                sock.settimeout(remaining)
            except OSError:
                pass
        return sock

    def _register_read_d2d(self, sock):
        with self._d2d_lock:
            self.read_cancellation.check()
            if self._op_expired is not None and self._op_expired.is_set():
                raise TimeoutError("Timed out communicating with the Frame.")
            self._d2d_sockets.add(sock)

    def open(self, resources=None):
        if self.read_cancellation is None:
            connection = SamsungTVWSConnection.open(self)
        else:
            connection = _open_read_channel(self, self.read_cancellation, resources)
        try:
            with response_deadline(connection.abort, self.timeout):
                while True:
                    event, _ = self._recv_frame()
                    if event == MS_CHANNEL_READY_EVENT:
                        return connection
                    if event not in (MS_CHANNEL_CLIENT_CONNECT_EVENT, MS_CHANNEL_CLIENT_DISCONNECT_EVENT):
                        raise ConnectionFailure(event)
        except Exception as exc:
            raise RuntimeError(
                "TV connected, but its artwork service did not become ready. "
                "Display a picture in Art Mode, then click Connect."
            ) from exc

    def available(self, category=None):
        connection = self.connection or self.open()
        with self._deadline(connection, self.timeout) as expired:
            try:
                result = super().available(category)
            except Exception as exc:
                if expired.is_set():
                    raise TimeoutError("Timed out reading the TV’s artwork list.") from exc
                raise
            if expired.is_set():
                raise TimeoutError("Timed out reading the TV’s artwork list.")
            return result

    def upload(self, file, matte="shadowbox_polar", portrait_matte="shadowbox_polar", file_type="png", date=None):
        connection = self.connection or self.open()
        with self._deadline(connection, self.timeout * 2) as expired:
            try:
                result = super().upload(file, matte=matte, portrait_matte=portrait_matte, file_type=file_type, date=date)
            except Exception as exc:
                if expired.is_set():
                    raise TimeoutError("Timed out uploading artwork to the Frame.") from exc
                raise
            if expired.is_set():
                raise TimeoutError("Timed out uploading artwork to the Frame.")
            return result


def connection_detail(error):
    # Samsung error frames can include every connected client's pairing token.
    if error.args and isinstance(error.args[0], dict):
        return error.args[0].get("event", type(error).__name__)
    return str(error) or type(error).__name__


@contextmanager
def artwork_connection(store: Store, host: str, port: int, status, *, reconnect=False, cancellation=None):
    token = store.load_token(host)
    # A blank token file from an unsuccessful first pairing is not a token.
    if port == 8002 and token is None:
        if reconnect:
            raise RuntimeError("No saved pairing for this Frame.")
        status("Pairing… Accept the FrameSync request with your TV remote (up to 60 seconds).")
        remote = SamsungTVWS(host, port=port, token=None, name="FrameSync", timeout=60)
        try:
            with ExitStack() as resources:
                try:
                    if cancellation is None:
                        remote.open()
                    else:
                        _open_read_channel(remote, cancellation, resources)
                finally:
                    remote.close()
            token = remote.token
            if not token:
                raise RuntimeError("The TV did not return a pairing token after approval.")
            store.save_token(host, token)
        except Exception as exc:
            raise RuntimeError(
                f"TV pairing did not complete. Accept the FrameSync request on the TV, then try Connect again. ({connection_detail(exc)})"
            ) from exc

    status("Opening the TV’s artwork library…")
    art = FrameArt(host, port=port, token=token, name="FrameSync", timeout=5 if reconnect else 30)
    with ExitStack() as resources:
        try:
            try:
                if cancellation is None:
                    art.open()
                else:
                    art.read_cancellation = cancellation
                    resources.enter_context(cancellation.watch(art._abort_d2d_sockets))
                    art.open(resources)
                if art.token and art.token != token:
                    store.save_token(host, art.token)
            except RuntimeError:
                raise
            except Exception as exc:
                raise RuntimeError(
                    f"Could not open the TV’s artwork channel. Turn the TV on and switch to Art Mode, then try Connect again. ({connection_detail(exc)})"
                ) from exc
            yield art
        finally:
            art.close()

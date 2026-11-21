"""Pair on Samsung's remote channel before opening its separate artwork channel."""
from contextlib import contextmanager
from threading import Event, Lock, Timer
from time import monotonic

from samsungtvws.art import SamsungTVArt
from samsungtvws.connection import SamsungTVWSConnection
from samsungtvws.event import (
    MS_CHANNEL_CLIENT_CONNECT_EVENT, MS_CHANNEL_CLIENT_DISCONNECT_EVENT, MS_CHANNEL_READY_EVENT,
)
from samsungtvws.exceptions import ConnectionFailure
from samsungtvws.remote import SamsungTVWS

from .core import Store


@contextmanager
def response_deadline(connection, seconds, *, on_timeout=None):
    # Presence traffic and WebSocket pings can keep the socket alive indefinitely.
    expired = Event()

    def interrupt():
        expired.set()
        try:
            connection.abort()
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
    context = deadline_ctx(connection, art.timeout) if deadline_ctx else response_deadline(connection, art.timeout)
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
        with response_deadline(connection, seconds, on_timeout=self._abort_d2d_sockets) as expired:
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

    def open(self):
        connection = SamsungTVWSConnection.open(self)
        try:
            with response_deadline(connection, self.timeout):
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
def artwork_connection(store: Store, host: str, port: int, status, *, reconnect=False):
    token = store.load_token(host)
    # A blank token file from an unsuccessful first pairing is not a token.
    if port == 8002 and token is None:
        if reconnect:
            raise RuntimeError("No saved pairing for this Frame.")
        status("Pairing… Accept the FrameSync request with your TV remote (up to 60 seconds).")
        remote = SamsungTVWS(host, port=port, token=None, name="FrameSync", timeout=60)
        try:
            remote.open()
            token = remote.token
            if not token:
                raise RuntimeError("The TV did not return a pairing token after approval.")
            store.save_token(host, token)
        except Exception as exc:
            raise RuntimeError(
                f"TV pairing did not complete. Accept the FrameSync request on the TV, then try Connect again. ({connection_detail(exc)})"
            ) from exc
        finally:
            remote.close()

    status("Opening the TV’s artwork library…")
    art = FrameArt(host, port=port, token=token, name="FrameSync", timeout=5 if reconnect else 30)
    try:
        try:
            art.open()
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

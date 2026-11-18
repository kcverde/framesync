"""Pair on Samsung's remote channel before opening its separate artwork channel."""
from contextlib import contextmanager
from threading import Event, Timer

from samsungtvws.art import SamsungTVArt
from samsungtvws.connection import SamsungTVWSConnection
from samsungtvws.event import (
    MS_CHANNEL_CLIENT_CONNECT_EVENT, MS_CHANNEL_CLIENT_DISCONNECT_EVENT, MS_CHANNEL_READY_EVENT,
)
from samsungtvws.exceptions import ConnectionFailure
from samsungtvws.remote import SamsungTVWS

from .core import Store


@contextmanager
def response_deadline(connection, seconds):
    # Presence traffic and WebSocket pings can keep the socket alive indefinitely.
    expired = Event()

    def interrupt():
        expired.set()
        connection.abort()

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
    with response_deadline(connection, art.timeout) as expired:
        try:
            result = operation(*args, **kwargs)
        except Exception as exc:
            if expired.is_set():
                raise TimeoutError(f"The Frame timed out during {operation.__name__}.") from exc
            raise
        if expired.is_set():
            raise TimeoutError(f"The Frame timed out during {operation.__name__}.")
        return result


class FrameArt(SamsungTVArt):
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
        with response_deadline(connection, self.timeout) as expired:
            try:
                return super().available(category)
            except Exception as exc:
                if expired.is_set():
                    raise TimeoutError("Timed out reading the TV’s artwork list.") from exc
                raise


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

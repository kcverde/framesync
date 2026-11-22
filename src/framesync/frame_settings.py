"""Frame display settings and artwork presentation controls."""
from functools import partial
import json

from PySide6.QtCore import QObject, Property, Signal, Slot
from samsungtvws.exceptions import ResponseError

from .tv import call_tv


# Names on the wire come from the TV's get_artmode_settings reply.
DISPLAY = {
    "brightness": "Brightness",
    "color_temperature": "Colour temperature",
    "brightness_sensor_setting": "Automatic brightness",
    "motion_timer": "Sleep after inactivity (minutes)",
    "motion_sensitivity": "Motion sensitivity",
}


def _list(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return []
    return value if isinstance(value, list) else []


def _choices(raw, *keys):
    result = []
    for entry in _list(raw):
        if isinstance(entry, dict):
            entry = next((entry.get(key) for key in keys if entry.get(key) is not None), None)
        if isinstance(entry, (int, str)) and str(entry) and str(entry) not in result:
            result.append(str(entry))
    return result


def read_display(art):
    payload = call_tv(art, art.get_artmode_settings)
    rows = []
    for entry in _list(payload.get("data")):
        if not isinstance(entry, dict):
            continue
        key = entry.get("item")
        if key not in DISPLAY or entry.get("value") is None:
            continue
        value = str(entry["value"])
        choices = _choices(entry.get("valid_values"))
        if key == "brightness_sensor_setting" and not choices:
            choices = ["off", "on"]
        if not choices:
            try:
                low, high = int(entry["min"]), int(entry["max"])
                if 0 <= high - low <= 1000:
                    choices = [str(n) for n in range(low, high + 1)]
            except (KeyError, ValueError, TypeError):
                pass
        rows.append(dict(key=key, label=DISPLAY[key], value=value, choices=choices))
    return rows


def read_slideshow(art):
    # Older sets use auto_rotation for the same playback setting.
    try:
        data = call_tv(art, art.get_slideshow_status)
        api = "slideshow"
    except ResponseError:
        data = call_tv(art, art.get_auto_rotation_status)
        api = "auto_rotation"
    interval = data.get("value")
    if interval != "off" and not str(interval).isdigit():
        raise ValueError("The TV returned an unknown slideshow interval.")
    return dict(api=api, minutes=0 if interval == "off" else int(interval),
                shuffle=data.get("type") == "shuffleslideshow",
                category=data.get("category_id") or "MY-C0002")


def read_mattes(art):
    data = call_tv(art, art.get_matte_list)
    return dict(styles=_choices(data.get("matte_types"), "matte_type", "matte_id"),
                colours=_choices(data.get("matte_colors"), "color", "matte_color"))


def read_filters(art):
    return _choices(call_tv(art, art.get_photo_filter_list), "filter_id")


class FrameSettings(QObject):
    changed = Signal()
    artworkChanged = Signal(str)

    def __init__(self, backend):
        super().__init__(backend)
        self.backend = backend
        self.values = {}

    def reset(self):
        self.values.clear()
        self.changed.emit()

    @Property("QVariantMap", notify=changed)
    def state(self):
        return self.values

    def _store(self, section, result):
        self.values[section] = result
        self.changed.emit()

    @Slot(str)
    def load(self, section):
        readers = {"display": read_display, "slideshow": read_slideshow,
                   "mattes": read_mattes, "filters": read_filters}
        if section not in readers or self.backend.state["busy"]:
            return
        if section in ("mattes", "filters") and section in self.values:
            return
        self.values.pop(section, None)
        self.changed.emit()
        self.backend.run_tv_operation(readers[section], partial(self._store, section),
                                      f"Reading {section} from your Frame…")

    @Slot(str, str)
    def setDisplay(self, key, value):
        row = next((item for item in self.values.get("display", []) if item["key"] == key), None)
        if not row or value not in row["choices"]:
            return

        def update(art):
            # Brightness and colour temperature use JSON numbers; motion uses strings.
            argument = int(value) if key in ("brightness", "color_temperature") else value
            call_tv(art, getattr(art, "set_" + key), argument)
            return read_display(art)

        self.backend.run_tv_operation(update, partial(self._store, "display"), f"Updating {row['label'].lower()}…")

    @Slot(int, bool, str)
    def setSlideshow(self, minutes, shuffle, category):
        current = self.values.get("slideshow")
        if not current or minutes < 0 or category not in {"MY-C0002", "MY-C0004", current["category"]}:
            return

        def update(art):
            setter = art.set_slideshow_status if current["api"] == "slideshow" else art.set_auto_rotation_status
            call_tv(art, setter, duration=minutes, type=shuffle, category_id=category)
            return read_slideshow(art)

        self.backend.run_tv_operation(update, partial(self._store, "slideshow"), "Updating slideshow…")

    def _artwork_action(self, cid, command, message):
        if cid:
            self.backend.run_tv_operation(command, lambda _: self.artworkChanged.emit(cid), message)

    @Slot(str, bool)
    def setFavourite(self, cid, enabled):
        self._artwork_action(cid, lambda art: call_tv(art, art.set_favourite, cid, "on" if enabled else "off"),
                             "Updating TV favourites…")

    @Slot(str, str, str)
    def setMatte(self, cid, style, colour):
        options = self.values.get("mattes", {})
        if style not in options.get("styles", []):
            return
        matte_id = style
        if style != "none" and options["colours"]:
            if colour not in options["colours"]:
                return
            matte_id = f"{style}_{colour}"
        self._artwork_action(cid, lambda art: call_tv(art, art.change_matte, cid, matte_id), "Applying artwork matte…")

    @Slot(str, str)
    def setFilter(self, cid, filter_id):
        if filter_id in self.values.get("filters", []):
            self._artwork_action(cid, lambda art: call_tv(art, art.set_photo_filter, cid, filter_id), "Applying photo filter…")

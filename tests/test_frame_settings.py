import json
import unittest
from unittest.mock import Mock

from PySide6.QtCore import QObject
from samsungtvws.exceptions import ResponseError

from framesync.frame_settings import FrameSettings, read_display, read_slideshow


class Frame:
    def __init__(self):
        self.commands = []
        self.display = [
            {"item": "brightness", "value": "1", "min": "0", "max": "3"},
            {"item": "color_temperature", "value": "0", "min": "-2", "max": "2"},
            {"item": "brightness_sensor_setting", "value": "on"},
            {"item": "motion_timer", "value": "60", "valid_values": '["off","60","120"]'},
            {"item": "motion_sensitivity", "value": "2", "min": "1", "max": "3"},
        ]
        self.slideshow = {"value": "off", "type": "slideshow", "category_id": "MY-C0002"}

    def get_artmode_settings(self):
        return {"data": json.dumps(self.display)}

    def get_slideshow_status(self):
        return self.slideshow

    def set_slideshow_status(self, duration, type, category_id):
        self.commands.append(("slideshow", duration, type, category_id))
        self.slideshow = {"value": str(duration) if duration else "off",
                          "type": "shuffleslideshow" if type else "slideshow",
                          "category_id": category_id}

    def get_matte_list(self):
        return {"matte_types": [{"matte_type": "none"}, {"matte_type": "shadowbox"}],
                "matte_colors": [{"color": "polar"}]}

    def get_photo_filter_list(self):
        return [{"filter_id": "None"}, {"filter_id": "Pastel"}]


class Backend(QObject):
    def __init__(self):
        super().__init__()
        self.art = Frame()
        self.state = {"busy": False, "message": ""}

    def run_tv_operation(self, op, done, message):
        if self.state["busy"]:
            return False
        done(op(self.art))
        return True


class FrameSettingsTests(unittest.TestCase):
    def setUp(self):
        self.backend = Backend()
        self.settings = FrameSettings(self.backend)
        self.art = self.backend.art

    def test_choices_from_tv_and_writes_only_after_selection(self):
        self.settings.load("display")
        self.assertEqual(self.art.commands, [])
        choices = {s["key"]: s["choices"] for s in self.settings.state["display"]}
        self.assertEqual(choices["brightness"], ["0", "1", "2", "3"])
        self.assertEqual(choices["motion_timer"], ["off", "60", "120"])
        for key, value, argument in [
            ("brightness", "3", 3),
            ("color_temperature", "-2", -2),
            ("motion_timer", "120", "120"),
            ("motion_sensitivity", "3", "3"),
            ("brightness_sensor_setting", "off", "off"),
        ]:
            setter = Mock()
            setattr(self.art, "set_" + key, setter)
            with self.subTest(key=key):
                self.settings.setDisplay(key, "unexpected")
                setter.assert_not_called()
                self.settings.setDisplay(key, value)
                setter.assert_called_once_with(argument)

    def test_read_only_current_value_if_firmware_omits_range(self):
        self.art.display = [{"item": "color_temperature", "value": "4"}, {"value": None}]
        self.assertEqual(read_display(self.art), [
            {"key": "color_temperature", "label": "Colour temperature", "value": "4", "choices": []}
        ])

    def test_slideshow_and_legacy_rotation(self):
        self.settings.load("slideshow")
        self.settings.setSlideshow(15, True, "MY-C0004")
        self.assertEqual(self.art.commands, [("slideshow", 15, True, "MY-C0004")])
        self.assertEqual(self.settings.state["slideshow"]["minutes"], 15)
        self.art.get_slideshow_status = Mock(side_effect=ResponseError("unknown command"))
        self.art.get_auto_rotation_status = Mock(return_value=self.art.slideshow)
        self.art.set_auto_rotation_status = Mock()
        self.settings.load("slideshow")
        self.settings.setSlideshow(0, False, "MY-C0002")
        self.art.set_auto_rotation_status.assert_called_once_with(duration=0, type=False, category_id="MY-C0002")

    def test_lost_connection_never_retries_as_legacy(self):
        self.art.get_slideshow_status = Mock(side_effect=TimeoutError())
        self.art.get_auto_rotation_status = Mock()
        with self.assertRaises(TimeoutError):
            read_slideshow(self.art)
        self.art.get_auto_rotation_status.assert_not_called()

    def test_per_artwork_commands_use_offered_values_and_selected_id(self):
        self.art.change_matte = Mock()
        self.art.set_photo_filter = Mock()
        self.art.set_favourite = Mock()
        changed = []
        self.settings.artworkChanged.connect(changed.append)
        self.settings.load("mattes")
        self.settings.load("filters")
        self.settings.setMatte("abc", "shadowbox", "unknown")
        self.settings.setFilter("abc", "unknown")
        self.assertEqual(changed, [])
        self.settings.setMatte("abc", "shadowbox", "polar")
        self.art.change_matte.assert_called_once_with("abc", "shadowbox_polar")
        self.settings.setMatte("abc", "none", "polar")
        self.art.change_matte.assert_called_with("abc", "none")
        self.settings.setFilter("abc", "Pastel")
        self.art.set_photo_filter.assert_called_once_with("abc", "Pastel")
        self.settings.setFavourite("abc", False)
        self.art.set_favourite.assert_called_once_with("abc", "off")
        self.assertEqual(changed, ["abc"] * 4)
        self.settings.reset()
        self.settings.setFilter("abc", "Pastel")
        self.assertEqual(self.art.set_photo_filter.call_count, 1)

    def test_capabilities_are_cached_until_reset_even_when_empty(self):
        self.art.get_matte_list = Mock(wraps=self.art.get_matte_list)
        self.art.get_photo_filter_list = Mock(return_value=[])
        self.art.get_artmode_settings = Mock(wraps=self.art.get_artmode_settings)
        for _ in range(2):
            for section in ("mattes", "filters", "display"):
                self.settings.load(section)
        self.art.get_matte_list.assert_called_once()
        self.art.get_photo_filter_list.assert_called_once()
        self.assertEqual(self.art.get_artmode_settings.call_count, 2)

        self.settings.reset()
        self.settings.load("mattes")
        self.settings.load("filters")
        self.assertEqual(self.art.get_matte_list.call_count, 2)
        self.assertEqual(self.art.get_photo_filter_list.call_count, 2)

    def test_rejected_changes_do_not_emit_success(self):
        self.art.set_favourite = Mock(side_effect=ResponseError("unsupported"))
        events = []
        self.settings.artworkChanged.connect(events.append)
        with self.assertRaises(ResponseError):
            self.settings.setFavourite("abc", True)
        self.assertFalse(events)

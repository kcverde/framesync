import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from PySide6.QtWidgets import QApplication

from framesync.app import Controller
from framesync.core import Store


class AppearanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_change_and_restore_appearance_without_saving_an_unfinished_ip_edit(self):
        with TemporaryDirectory() as tmp:
            store = Store(Path(tmp))
            store.save_settings({"host": "192.168.1.50", "port": 8002, "folder": ""})
            controller = Controller(store)
            self.assertEqual(controller.appearance, "system")
            controller.setTarget("192.", 8002)
            controller.setAppearance("dark")
            self.assertEqual(store.settings()["host"], "192.168.1.50")
            reopened = Controller(store)
            self.assertEqual(reopened.appearance, "dark")
            reopened.save()
            self.assertEqual(store.settings()["appearance"], "dark")
            for mode in ("light", "system"):
                reopened.setAppearance(mode)
                self.assertEqual(Controller(store).appearance, mode)

    def test_selector_updates_an_open_discover_dialog_and_survives_relaunch(self):
        from PySide6.QtCore import QObject, QCoreApplication, QEvent
        from PySide6.QtQml import QQmlExpression, qmlContext
        from framesync.app import load_window

        def evaluate(obj, source):
            expression = QQmlExpression(qmlContext(obj), obj, source)
            value, _ = expression.evaluate()
            self.assertFalse(expression.hasError(), expression.error().toString())
            return value

        with TemporaryDirectory() as tmp:
            store = Store(Path(tmp))
            controller = Controller(store)
            engine = load_window(controller)
            window = engine.rootObjects()[0]
            window.setProperty("page", "discover")
            button = window.findChild(QObject, "appearanceButton")
            menu = window.findChild(QObject, "appearanceMenu")
            button.click()
            self.app.processEvents()
            self.assertTrue(menu.property("visible"))
            menu.close()
            dialog = window.findChild(QObject, "museumFilters")
            evaluate(dialog, "open()")
            for mode, dark in (("dark", True), ("light", False), ("dark", True)):
                choice = window.findChild(QObject, "appearance-" + mode)
                choice.click()
                self.app.processEvents()
                self.assertEqual(window.property("dark"), dark)
                self.assertTrue(dialog.property("visible"))
                # Check the actual dialog surface and header, including live theme changes.
                self.assertEqual(evaluate(dialog, "background.color.hslLightness < 0.5"), dark)
                self.assertEqual(evaluate(dialog, "header.color.hslLightness > 0.5"), dark)
            engine.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            reopened = Controller(store)
            engine = load_window(reopened)
            self.assertTrue(engine.rootObjects()[0].property("dark"))
            reopened.setAppearance("system")
            engine.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

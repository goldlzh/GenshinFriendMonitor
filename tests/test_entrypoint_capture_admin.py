"""Entry-point orchestration with fake GUI/elevation; no UAC or real capture."""
from tests import test_regressions as _baseline

import ast
from contextlib import ExitStack
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch

import main as entry
from app import capture
from app.ui import main_window


class EntrypointCaptureAdminTests(unittest.TestCase):
    def setup_entry(self, elevated):
        calls = []
        stack = ExitStack()
        self.addCleanup(stack.close)
        application = MagicMock()
        application.exec.return_value = 0
        def allowed():
            calls.append('privileges')
            return elevated
        stack.enter_context(patch.object(entry, 'ensure_administrator', side_effect=allowed, create=True))
        stack.enter_context(patch.object(entry, 'enable_dpi_awareness', side_effect=lambda: calls.append('dpi')))
        stack.enter_context(patch.object(entry.sys, 'argv', ['main.py']))
        qt = stack.enter_context(patch('PySide6.QtWidgets.QApplication',
                                       side_effect=lambda argv: (calls.append('qt'), application)[1]))
        install = stack.enter_context(patch.object(capture, 'initialize_window_capture',
                                                  side_effect=lambda app: calls.append('capture'), create=True))
        window = stack.enter_context(patch.object(main_window, 'MainWindow',
                                                  side_effect=lambda: (calls.append('window'), MagicMock())[1]))
        return calls, application, qt, install, window

    def test_denied_or_relaunched_startup_does_not_open_gui_or_capture(self):
        calls, app, qt, install, window = self.setup_entry(False)
        self.assertEqual(1, entry.main())
        self.assertEqual(['privileges'], calls)
        qt.assert_not_called()
        install.assert_not_called()
        window.assert_not_called()

    def test_elevated_startup_installs_capture_before_main_window(self):
        calls, app, qt, install, window = self.setup_entry(True)
        self.assertEqual(0, entry.main())
        self.assertEqual(['privileges', 'dpi', 'qt', 'capture', 'window'], calls)
        install.assert_called_once_with(app)
        app.exec.assert_called_once_with()


class PackagingCaptureAdminTests(unittest.TestCase):
    def spec_calls(self, name):
        root = Path(__file__).resolve().parents[1]
        tree = ast.parse((root / 'build.spec').read_text(encoding='utf-8-sig'))
        return [node for node in ast.walk(tree) if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name) and node.func.id == name]

    def test_exe_manifest_requests_administrator(self):
        values = {keyword.arg: keyword.value for keyword in self.spec_calls('EXE')[0].keywords}
        self.assertIn('uac_admin', values)
        self.assertIs(True, ast.literal_eval(values['uac_admin']))

    def test_window_capture_module_is_not_excluded_from_package(self):
        values = {keyword.arg: keyword.value for keyword in self.spec_calls('Analysis')[0].keywords}
        self.assertNotIn('PySide6.QtMultimedia', ast.literal_eval(values['excludes']))


if __name__ == '__main__':
    unittest.main()

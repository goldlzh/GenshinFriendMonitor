"""Window captures use only synthetic images and fake native capture sessions."""
from tests import test_regressions as baseline

import threading
import time
import unittest
import weakref
from unittest.mock import patch, MagicMock

import numpy as np
from PySide6.QtCore import QTimer, QThread, QObject, QLoggingCategory, qCDebug, qWarning
from PySide6.QtGui import QImage

from app import capture


class RoutingTests(unittest.TestCase):
    def test_identity_uses_window_target_but_explicit_capture_injection_wins(self):
        from app import account_identity as identity
        pixel = np.ones((100, 200, 3), np.uint8)
        targets = []
        def native(hwnd, rect):
            targets.append(hwnd)
            return pixel
        with patch.object(capture, '_capture_window_frame', side_effect=native), \
                patch('mss.mss', side_effect=AssertionError('desktop capture forbidden')), \
                patch.object(identity, 'read_account_uid', return_value='111111111'), \
                patch.object(identity.time, 'sleep'):
            self.assertEqual('111111111', identity.read_current_account((0,0,200,100), hwnd=123))
            self.assertEqual([123, 123], targets)
            self.assertEqual('111111111', identity.read_current_account(
                (0,0,200,100), lambda *args: pixel, hwnd=456))
            self.assertEqual([123, 123], targets)

    def test_scanner_confirmation_and_navigation_keep_selected_hwnd(self):
        from app import account_identity as identity, friend_screen
        from app.database import Database
        from app.scanner import Scanner
        db = Database(':memory:')
        self.addCleanup(db.close)
        db.set_account_uid('111111111')
        scanner = Scanner(db)
        scanner.set_window(123)
        scanner._get_rect = lambda: (0,0,200,100)
        targets = []
        def native(hwnd, rect):
            targets.append(hwnd)
            return np.full((100,200,3), 50, np.uint8)
        with patch.object(capture, '_capture_window_frame', side_effect=native), \
                patch('mss.mss', side_effect=AssertionError('desktop capture forbidden')), \
                patch.object(identity, 'read_account_uid', return_value='111111111'), \
                patch.object(identity.time, 'sleep'), \
                patch.object(scanner, '_wait', return_value=True), \
                patch.object(scanner, '_wait_for_game_foreground', return_value=True), \
                patch.object(friend_screen, 'gameplay_interface_mode', return_value='desktop'):
            scanner._confirm_account((0,0,200,100))
            self.assertEqual('desktop', scanner._read_interface_mode(np.zeros((2,2,3)), (0,0,200,100)))
        self.assertEqual([123, 123, 123], targets)

    def test_hwnd_scope_dispatches_without_desktop_and_is_cleared(self):
        result = np.full((2, 3, 3), 70, np.uint8)
        with patch.object(capture, '_capture_window_frame', return_value=result, create=True) as window:
            self.assertIs(result, capture.capture_window(123, 1, 2, 4, 4))
            window.assert_called_once_with(123, (1, 2, 4, 4))
            self.assertIsNone(getattr(capture._target, 'hwnd', None))

    def test_window_failure_never_opens_mss(self):
        with patch.object(capture, '_capture_window_frame', return_value=None, create=True), \
                patch('mss.mss', side_effect=AssertionError('desktop fallback')):
            self.assertIsNone(capture.capture_window(123, 1, 2, 4, 4))

    def test_explicit_manual_rect_keeps_rectangle_injection(self):
        with patch.object(capture, 'capture_region', side_effect=lambda *rect: rect):
            self.assertEqual((1, 2, 4, 4), capture.capture_window(None, 1, 2, 4, 4))

    def test_scope_does_not_leak_into_another_thread_or_after_exception(self):
        other = []
        def injected(*rect):
            worker = threading.Thread(target=lambda: other.append(getattr(capture._target, 'hwnd', None)))
            worker.start()
            worker.join()
            raise RuntimeError('injected failure')
        with patch.object(capture, 'capture_region', side_effect=injected):
            with self.assertRaises(RuntimeError):
                capture.capture_window(123, 1, 2, 4, 4)
        self.assertEqual([None], other)
        self.assertIsNone(getattr(capture._target, 'hwnd', None))


class WindowCaptureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from app import window_capture
        cls.wc = window_capture

    def geometry(self, **changes):
        values = dict(window_rect=(-108, -70, 108, 72),
                      frame_rect=(-100, -70, 100, 64),
                      client_rect=(-100, -40, 100, 60), dpi=144, identity=(7, 8))
        values.update(changes)
        return self.wc.WindowGeometry(**values)

    def test_extended_frame_crop_preserves_negative_coordinates_and_no_dpi_scaling(self):
        frame = np.zeros((134, 200, 3), np.uint8)
        frame[30:130] = (17, 91, 203)
        result = self.wc.crop_client(frame, self.geometry(), (-100, -40, 100, 60))
        self.assertEqual((100, 200, 3), result.shape)
        self.assertTrue(np.all(result == (17, 91, 203)))
        self.assertTrue(result.flags.owndata)

    def test_window_rect_frame_and_client_only_frame_have_exact_crops(self):
        geometry = self.geometry()
        for shape in ((142, 216, 3), (100, 200, 3)):
            frame = np.full(shape, 50, np.uint8)
            result = self.wc.crop_client(frame, geometry, geometry.client_rect)
            self.assertEqual((100, 200, 3), result.shape)

    def test_unmatched_or_ambiguous_dimensions_are_rejected(self):
        for frame, geometry in (
            (np.ones((67, 100, 3), np.uint8), self.geometry()),
            (np.ones((134, 200, 3), np.uint8), self.geometry(window_rect=(-101, -71, 99, 63))),
        ):
            with self.assertRaises(self.wc.CaptureError):
                self.wc.crop_client(frame, geometry, geometry.client_rect)

    def test_crop_cannot_reach_outside_client(self):
        with self.assertRaises(self.wc.CaptureError):
            self.wc.crop_client(np.ones((134, 200, 3), np.uint8), self.geometry(), (-101,-40,100,60))

    def test_rgb_conversion_owns_padded_qimage_memory(self):
        image = QImage(3, 2, QImage.Format.Format_RGB888)
        image.fill(0x112233)
        result = self.wc.frame_rgb(Frame(image))
        image.fill(0xffffff)
        self.assertEqual((2, 3, 3), result.shape)
        self.assertTrue(result.flags.owndata)
        self.assertTrue(np.all(result == (17, 34, 51)))

    def broker(self, plans, *, geometry=None):
        sessions = []
        def factory(hwnd, parent, on_frame, on_error):
            session = Session(hwnd, plans[len(sessions)], on_frame, on_error)
            sessions.append(session)
            return session
        broker = self.wc.WindowCaptureBroker(
            baseline.QT, geometry_reader=geometry or (lambda hwnd: self.geometry()),
            session_factory=factory)
        self.addCleanup(broker.close)
        return broker, sessions

    def request(self, broker, timeout_ms=100):
        return broker.capture(123, self.geometry().client_rect, timeout_ms=timeout_ms)

    def test_each_request_uses_new_session_and_only_its_fresh_frame(self):
        broker, sessions = self.broker([[(0, frame(40))], [(5, frame(90))]])
        first = self.request(broker)
        QTimer.singleShot(0, lambda: sessions[0].on_frame(frame(180)))
        second = self.request(broker)
        self.assertTrue(np.all(first == 40))
        self.assertTrue(np.all(second == 90))
        self.assertEqual([123, 123], [s.hwnd for s in sessions])
        self.assertTrue(all(s.closed for s in sessions))

    def test_completed_request_does_not_retain_pixels_in_late_frame_callbacks(self):
        broker, sessions = self.broker([[(0, frame(70))]])
        result = self.request(broker)
        reference = weakref.ref(result)
        del result
        self.assertIsNone(reference())
        # The still-retained old producer callback remains harmless without pixels.
        sessions[0].on_frame(frame(180))

    def test_invalid_frame_waits_for_next_fresh_frame(self):
        broker, sessions = self.broker([[(0, Frame(QImage())), (1, frame(80))]])
        self.assertTrue(np.all(self.request(broker) == 80))
        self.assertTrue(sessions[0].closed)

    def test_black_frame_is_failure(self):
        broker, sessions = self.broker([[(0, frame(0))]])
        self.assertIsNone(self.request(broker))
        self.assertTrue(sessions[0].closed)

    def test_timeout_is_bounded_and_late_frame_cannot_satisfy_next_request(self):
        broker, sessions = self.broker([[], [(5, frame(90))]])
        start = time.monotonic()
        self.assertIsNone(self.request(broker, 20))
        self.assertLess(time.monotonic() - start, .5)
        self.assertTrue(sessions[0].closed)
        QTimer.singleShot(0, lambda: sessions[0].on_frame(frame(180)))
        self.assertTrue(np.all(self.request(broker) == 90))

    def test_geometry_change_or_closed_window_discards_frame(self):
        for change in (self.geometry(dpi=192), self.geometry(client_rect=(-99,-40,101,60)), None):
            values = iter([self.geometry(), change])
            def geometry(hwnd):
                value = next(values)
                if value is None:
                    raise self.wc.CaptureError('closed')
                return value
            broker, sessions = self.broker([[(0, frame(70))]], geometry=geometry)
            self.assertIsNone(self.request(broker))
            self.assertTrue(sessions[0].closed)

    def test_native_geometry_failure_never_starts_capture(self):
        def unavailable(hwnd):
            raise self.wc.CaptureError('minimized')
        broker, sessions = self.broker([], geometry=unavailable)
        self.assertIsNone(self.request(broker))
        self.assertEqual([], sessions)

    def test_backend_error_releases_session(self):
        broker, sessions = self.broker([[(0, 'denied')]])
        self.assertIsNone(self.request(broker))
        self.assertTrue(sessions[0].closed)

    def test_close_cancels_wait_and_rejects_new_requests(self):
        broker, sessions = self.broker([[]])
        QTimer.singleShot(5, broker.close)
        self.assertIsNone(self.request(broker))
        self.assertTrue(sessions[0].closed)
        self.assertIsNone(self.request(broker))

    def test_busy_request_is_rejected_without_replacing_first(self):
        broker, sessions = self.broker([[(10, frame(80))]])
        busy = []
        QTimer.singleShot(0, lambda: busy.append(self.request(broker)))
        self.assertTrue(np.all(self.request(broker) == 80))
        self.assertEqual([None], busy)
        self.assertEqual(1, len(sessions))

    def test_worker_frame_conversion_and_cleanup_run_on_qt_main_thread(self):
        threads = []
        broker, sessions = self.broker([[(0, Frame(frame(60).image, threads))]])
        result = []
        worker = threading.Thread(target=lambda: result.append(self.request(broker)))
        worker.start()
        deadline = time.monotonic() + 1
        while worker.is_alive() and time.monotonic() < deadline:
            baseline.QT.processEvents()
            time.sleep(.001)
        worker.join(.1)
        self.assertFalse(worker.is_alive())
        self.assertTrue(np.all(result[0] == 60))
        self.assertEqual([baseline.QT.thread()], threads)
        self.assertEqual(baseline.QT.thread(), sessions[0].close_thread)

    def test_worker_timeout_cancels_queued_request_before_session_creation(self):
        broker, sessions = self.broker([])
        result = []
        worker = threading.Thread(target=lambda: result.append(self.request(broker, 20)))
        worker.start()
        worker.join(.5)  # Intentionally do not pump Qt until after the worker deadline.
        self.assertFalse(worker.is_alive())
        self.assertEqual([None], result)
        baseline.QT.processEvents()
        self.assertEqual([], sessions)

    def test_unsupported_qt_backend_is_rejected_before_gdi_fallback_can_deliver(self):
        def unsupported(*args):
            qWarning('Not supported QT_WINDOW_CAPTURE_BACKEND: uwp')
            return object()
        with patch.object(self.wc, 'QWindowCapture', side_effect=unsupported):
            with self.assertRaises(self.wc.CaptureError):
                self.wc.create_uwp_capture(None)

    def test_only_positive_uwp_evidence_accepts_qt_capture(self):
        native = QObject()
        def confirmed(*args):
            category = QLoggingCategory('qt.multimedia.ffmpeg.windowcapture.uwp')
            qCDebug(category, 'Creating UWP screen capture')
            return native
        with patch.object(self.wc, 'QWindowCapture', side_effect=confirmed):
            self.assertIs(native, self.wc.create_uwp_capture(None))
        with patch.object(self.wc, 'QWindowCapture', return_value=QObject()):
            with self.assertRaises(self.wc.CaptureError):
                self.wc.create_uwp_capture(None)

    def test_synchronous_initialization_error_cleans_session_without_starting_it(self):
        sessions = []
        def factory(hwnd, parent, on_frame, on_error):
            session = Session(hwnd, [], on_frame, on_error)
            session.started = False
            session.start = lambda: setattr(session, 'started', True)
            sessions.append(session)
            on_error('initialization failed')
            return session
        broker = self.wc.WindowCaptureBroker(baseline.QT,
            geometry_reader=lambda hwnd: self.geometry(), session_factory=factory)
        self.addCleanup(broker.close)
        self.assertIsNone(self.request(broker))
        self.assertTrue(sessions[0].closed)
        self.assertFalse(sessions[0].started)

    def test_qt_session_cleans_partial_setup_when_video_sink_cannot_be_created(self):
        foreign, capture, session = MagicMock(), MagicMock(), MagicMock()
        foreign.winId.return_value = 123
        target = MagicMock()
        target.isValid.return_value = True
        with patch.object(self.wc.QWindow, 'fromWinId', return_value=foreign), \
                patch.object(self.wc, 'QCapturableWindow', return_value=target), \
                patch.object(self.wc, 'create_uwp_capture', return_value=capture), \
                patch.object(self.wc, 'QMediaCaptureSession', return_value=session), \
                patch.object(self.wc, 'QVideoSink', side_effect=RuntimeError('sink failure')):
            with self.assertRaisesRegex(RuntimeError, 'sink failure'):
                self.wc._QtSession(123, None, lambda f: None, lambda e: None)
        foreign.deleteLater.assert_called_once()
        capture.stop.assert_called_once()
        capture.deleteLater.assert_called_once()
        session.deleteLater.assert_called_once()

    def test_native_window_minimized_does_not_query_pixels_and_restores_dpi_context(self):
        import ctypes
        gui, process, user32 = MagicMock(), MagicMock(), MagicMock()
        gui.IsWindow.return_value = True
        gui.IsIconic.return_value = True
        user32.SetThreadDpiAwarenessContext.return_value = 99
        with patch.dict('sys.modules', win32gui=gui, win32process=process), \
                patch.object(ctypes, 'WinDLL', return_value=user32):
            with self.assertRaisesRegex(self.wc.CaptureError, '最小化'):
                self.wc.read_geometry(123)
        gui.GetWindowRect.assert_not_called()
        self.assertEqual(99, user32.SetThreadDpiAwarenessContext.call_args.args[0])

    def test_capture_setup_past_deadline_never_starts_session(self):
        sessions = []
        def factory(hwnd, parent, on_frame, on_error):
            session = Session(hwnd, [], on_frame, on_error)
            session.started = False
            session.start = lambda: setattr(session, 'started', True)
            sessions.append(session)
            time.sleep(.025)
            return session
        broker = self.wc.WindowCaptureBroker(baseline.QT,
            geometry_reader=lambda hwnd: self.geometry(), session_factory=factory)
        self.addCleanup(broker.close)
        self.assertIsNone(self.request(broker, 10))
        self.assertTrue(sessions[0].closed)
        self.assertFalse(sessions[0].started)

    def test_static_native_window_gets_bounded_async_repaint_then_cancels_repaints(self):
        foreign, capture, session, sink = (MagicMock() for _ in range(4))
        foreign.winId.return_value = 123
        target = MagicMock()
        target.isValid.return_value = True
        capture.isActive.return_value = True
        gui, process = MagicMock(), MagicMock()
        gui.IsWindow.return_value = gui.IsWindowVisible.return_value = True
        gui.IsIconic.return_value = False
        process.GetWindowThreadProcessId.return_value = (7, 8)
        received = []
        def repaint(hwnd, rect, erase):
            received.append((hwnd, rect, erase))
            # A static target produces its first valid texture only after repaint.
            sink.videoFrameChanged.connect.call_args.args[0](frame(90))
        gui.InvalidateRect.side_effect = repaint
        broker = self.wc.WindowCaptureBroker(baseline.QT,
            geometry_reader=lambda hwnd: self.geometry())
        self.addCleanup(broker.close)
        with patch.object(self.wc.QWindow, 'fromWinId', return_value=foreign), \
                patch.object(self.wc, 'QCapturableWindow', return_value=target), \
                patch.object(self.wc, 'create_uwp_capture', return_value=capture), \
                patch.object(self.wc, 'QMediaCaptureSession', return_value=session), \
                patch.object(self.wc, 'QVideoSink', return_value=sink), \
                patch.dict('sys.modules', win32gui=gui, win32process=process):
            result = self.request(broker, 1200)
            self.assertIsNotNone(result)
            self.assertTrue(np.all(result == 90))
            self.assertEqual([(123, None, False)], received)
            # The second/third native repaint must be canceled after capture closes.
            loop = self.wc.QEventLoop()
            QTimer.singleShot(850, loop.quit)
            loop.exec()
            self.assertEqual([(123, None, False)], received)

    def test_repaint_never_targets_recycled_hwnd(self):
        foreign, capture, session, sink = (MagicMock() for _ in range(4))
        foreign.winId.return_value = 123
        target = MagicMock()
        target.isValid.return_value = True
        capture.isActive.return_value = True
        gui, process = MagicMock(), MagicMock()
        gui.IsWindow.return_value = gui.IsWindowVisible.return_value = True
        gui.IsIconic.return_value = False
        process.GetWindowThreadProcessId.side_effect = [(7, 8), (9, 10)]
        broker = self.wc.WindowCaptureBroker(baseline.QT,
            geometry_reader=lambda hwnd: self.geometry())
        self.addCleanup(broker.close)
        with patch.object(self.wc.QWindow, 'fromWinId', return_value=foreign), \
                patch.object(self.wc, 'QCapturableWindow', return_value=target), \
                patch.object(self.wc, 'create_uwp_capture', return_value=capture), \
                patch.object(self.wc, 'QMediaCaptureSession', return_value=session), \
                patch.object(self.wc, 'QVideoSink', return_value=sink), \
                patch.dict('sys.modules', win32gui=gui, win32process=process):
            self.assertIsNone(self.request(broker, 1200))
        gui.InvalidateRect.assert_not_called()
        self.assertEqual(2, process.GetWindowThreadProcessId.call_count)


class Frame:
    def __init__(self, image, threads=None):
        self.image = image
        self.threads = threads

    def isValid(self):
        return not self.image.isNull()

    def toImage(self):
        if self.threads is not None:
            self.threads.append(QThread.currentThread())
        return self.image


def frame(value):
    image = QImage(200, 134, QImage.Format.Format_RGB888)
    image.fill((value << 16) | (value << 8) | value)
    return Frame(image)


class Session:
    def __init__(self, hwnd, plan, on_frame, on_error):
        self.hwnd, self.plan, self.on_frame, self.on_error = hwnd, plan, on_frame, on_error
        self.closed = False

    def start(self):
        for delay, payload in self.plan:
            callback = self.on_error if isinstance(payload, str) else self.on_frame
            QTimer.singleShot(delay, lambda cb=callback, p=payload: cb(p))

    def close(self):
        self.closed = True
        self.close_thread = QThread.currentThread()

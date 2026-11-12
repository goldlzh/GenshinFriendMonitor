"""Fresh, exact-HWND Qt/WGC capture. All native Qt work stays on the GUI thread.

Qt 6.11's UWP backend captures a native window texture, including decorations.
Pixel dimensions must match known physical window bounds; guessing a scale or
falling back to desktop pixels would make identity/OCR checks unsafe.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import ctypes
from ctypes import wintypes
import os
import threading
import time

import numpy as np

# Must precede the first Qt Multimedia integration creation in this process.
os.environ['QT_MEDIA_BACKEND'] = 'ffmpeg'
os.environ['QT_WINDOW_CAPTURE_BACKEND'] = 'uwp'
from PySide6.QtCore import (QObject, QThread, QTimer, QEventLoop, Signal, Slot,
                            Qt, QLoggingCategory, qInstallMessageHandler)
from PySide6.QtGui import QGuiApplication, QImage, QWindow
from PySide6.QtMultimedia import (QCapturableWindow, QWindowCapture,
                                  QMediaCaptureSession, QVideoSink)

from app.logger import logger

Rect = tuple[int, int, int, int]


class CaptureError(RuntimeError):
    """A failed or unprovable capture; never replaced with desktop pixels."""


@dataclass(frozen=True)
class WindowGeometry:
    window_rect: Rect
    frame_rect: Rect
    client_rect: Rect
    dpi: int
    identity: tuple[int, int]  # thread and process IDs detect a recycled HWND.


def _contains(outer, inner):
    return (outer[0] <= inner[0] < inner[2] <= outer[2] and
            outer[1] <= inner[1] < inner[3] <= outer[3])


def read_geometry(hwnd: int) -> WindowGeometry:
    """Read physical Win32/DWM bounds under a temporary per-monitor DPI context."""
    import win32gui
    import win32process

    user32 = ctypes.WinDLL('user32', use_last_error=True)
    set_context = user32.SetThreadDpiAwarenessContext
    set_context.argtypes = [ctypes.c_void_p]
    set_context.restype = ctypes.c_void_p
    previous = set_context(ctypes.c_void_p(-4))  # PER_MONITOR_AWARE_V2
    if not previous:
        raise CaptureError('无法切换物理像素 DPI 上下文')
    try:
        if not win32gui.IsWindow(hwnd):
            raise CaptureError('窗口已关闭')
        if win32gui.IsIconic(hwnd) or not win32gui.IsWindowVisible(hwnd):
            raise CaptureError('窗口已最小化或不可见')
        native = tuple(win32gui.GetWindowRect(hwnd))
        client = win32gui.GetClientRect(hwnd)
        client = (*win32gui.ClientToScreen(hwnd, client[:2]),
                  *win32gui.ClientToScreen(hwnd, client[2:]))
        dwm = ctypes.WinDLL('dwmapi', use_last_error=True).DwmGetWindowAttribute
        dwm.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
        dwm.restype = ctypes.c_long
        bounds = wintypes.RECT()
        if dwm(hwnd, 9, ctypes.byref(bounds), ctypes.sizeof(bounds)) < 0:
            raise CaptureError('无法读取 DWM 窗口边界')
        extended = (bounds.left, bounds.top, bounds.right, bounds.bottom)
        get_dpi = user32.GetDpiForWindow
        get_dpi.argtypes = [wintypes.HWND]
        get_dpi.restype = wintypes.UINT
        dpi = get_dpi(hwnd)
        if not dpi or not _contains(native, client) or not _contains(extended, client):
            raise CaptureError('窗口客户区或 DPI 边界无效')
        return WindowGeometry(native, extended, client, dpi,
                              win32process.GetWindowThreadProcessId(hwnd))
    except CaptureError:
        raise
    except Exception as error:
        raise CaptureError(f'读取窗口几何失败: {error}') from error
    finally:
        set_context(previous)


def frame_rgb(frame):
    """Detach RGB bytes, including QImage row padding, before releasing Qt buffers."""
    if not frame.isValid():
        return None
    image = frame.toImage()
    if image.isNull():
        return None
    image = image.convertToFormat(QImage.Format.Format_RGB888)
    width, height, stride = image.width(), image.height(), image.bytesPerLine()
    data = np.frombuffer(image.constBits(), dtype=np.uint8, count=height * stride)
    return data.reshape(height, stride)[:, :width * 3].reshape(height, width, 3).copy()


def crop_client(rgb, geometry: WindowGeometry, rect: Rect):
    """Crop only if native texture size gives one unambiguous physical origin."""
    if rgb.ndim != 3 or rgb.shape[2] != 3 or not _contains(geometry.client_rect, rect):
        raise CaptureError('截图请求超出客户区')
    height, width = rgb.shape[:2]
    origins = {(bounds[0], bounds[1]) for bounds in
               (geometry.window_rect, geometry.frame_rect, geometry.client_rect)
               if (bounds[2] - bounds[0], bounds[3] - bounds[1]) == (width, height)}
    if len(origins) != 1:
        raise CaptureError(f'采集帧尺寸无法精确映射: frame={width}x{height}, bounds={geometry}')
    x, y = next(iter(origins))
    if not _contains((x, y, x + width, y + height), rect):
        raise CaptureError('采集帧未包含完整客户区请求')
    left, top, right, bottom = rect
    return rgb[top-y:bottom-y, left-x:right-x].copy()


def create_uwp_capture(parent):
    """Confirm Qt chose UWP, rejecting its documented silent GDI fallback.

    Qt's public API has no backend-name property. In Qt 6.11.2 its UWP
    constructor logs this category, and unsupported selections log a warning:
    qt/qtmultimedia/src/plugins/multimedia/ffmpeg/qffmpegmediaintegration.cpp
    and qffmpegwindowcapture_uwp.cpp. A changed/missing diagnostic fails closed.
    """
    evidence = {'uwp': False, 'unsupported': False}
    rules = os.environ.get('QT_LOGGING_RULES', '')
    # Preserve environment rules while enabling the one constructor diagnostic.
    QLoggingCategory.setFilterRules(rules + '\nqt.multimedia.ffmpeg.windowcapture.uwp.debug=true')
    previous = None

    def handler(kind, context, message):
        if 'Not supported QT_WINDOW_CAPTURE_BACKEND' in message:
            evidence['unsupported'] = True
        if (context.category == 'qt.multimedia.ffmpeg.windowcapture.uwp' and
                'Creating UWP screen capture' in message):
            evidence['uwp'] = True
        if previous is not None:
            previous(kind, context, message)

    previous = qInstallMessageHandler(handler)
    capture = None
    try:
        capture = QWindowCapture(parent)
    finally:
        qInstallMessageHandler(previous)
    if evidence['unsupported'] or not evidence['uwp']:
        if isinstance(capture, QObject):
            capture.deleteLater()
        raise CaptureError(f'Qt UWP 后端未确认，拒绝潜在 GDI 回退: {evidence}')
    logger.info('窗口采集 backend=ffmpeg/uwp evidence=Qt-UWP-constructor')
    return capture


class _QtSession:
    """One native session/sink per request, never reused for another frame."""
    def __init__(self, hwnd, parent, on_frame, on_error):
        self.foreign = self.capture = self.session = self.sink = None
        self.hwnd, self.parent = hwnd, parent
        self.repaint_timers = []
        self.identity = None
        self.on_frame = on_frame
        self.on_error = lambda *args: on_error(str(args))
        try:
            self.foreign = QWindow.fromWinId(hwnd)
            if self.foreign is None or int(self.foreign.winId()) != hwnd:
                raise CaptureError('不能按精确 HWND 绑定窗口')
            target = QCapturableWindow(self.foreign)
            if not target.isValid():
                raise CaptureError('Qt 目标窗口无效')
            self.capture = create_uwp_capture(parent)
            self.session = QMediaCaptureSession(parent)
            self.sink = QVideoSink(parent)
            self.capture.setWindow(target)
            self.session.setWindowCapture(self.capture)
            self.session.setVideoSink(self.sink)
            self.sink.videoFrameChanged.connect(self.on_frame)
            self.capture.errorOccurred.connect(self.on_error)
        except Exception:
            self.close()
            raise

    def start(self):
        import win32process
        self.identity = win32process.GetWindowThreadProcessId(self.hwnd)
        self.capture.start()
        if self.capture is None or not self.capture.isActive():
            return
        # Qt 6.11's WGC backend recreates its frame pool when ContentSize differs
        # from GetWindowRect and discards that initial frame. A static window may
        # otherwise never produce a second texture. Request a bounded asynchronous
        # repaint, without input, focus, synchronous messages, or geometry changes.
        for delay in (100, 400, 800):
            timer = QTimer(self.parent)
            timer.setSingleShot(True)
            timer.timeout.connect(self._request_repaint)
            self.repaint_timers.append(timer)
            timer.start(delay)

    def _request_repaint(self):
        import win32gui
        import win32process
        if self.capture is None or not self.capture.isActive():
            return
        try:
            if (not win32gui.IsWindow(self.hwnd) or win32gui.IsIconic(self.hwnd) or
                    not win32gui.IsWindowVisible(self.hwnd) or
                    win32process.GetWindowThreadProcessId(self.hwnd) != self.identity):
                raise CaptureError('窗口关闭、最小化或 HWND 身份已改变')
            win32gui.InvalidateRect(self.hwnd, None, False)
        except Exception as error:
            self.on_error(f'请求采集初始化重绘失败: {error}')

    def close(self):
        for timer in self.repaint_timers:
            timer.stop()
            timer.deleteLater()
        self.repaint_timers.clear()
        if self.sink is not None:
            self.sink.videoFrameChanged.disconnect(self.on_frame)
        if self.capture is not None:
            try:
                self.capture.errorOccurred.disconnect(self.on_error)
            except RuntimeError:
                pass
            self.capture.stop()
        if self.session is not None:
            self.session.setVideoSink(None)
            self.session.setWindowCapture(None)
        for obj in (self.sink, self.session, self.capture, self.foreign):
            if obj is not None:
                obj.deleteLater()
        self.foreign = self.capture = self.session = self.sink = None


@dataclass(eq=False)
class _Request:
    hwnd: int
    rect: Rect
    deadline: float
    done: threading.Event = field(default_factory=threading.Event)
    canceled: threading.Event = field(default_factory=threading.Event)
    result: object = None
    geometry: object = None
    session: object = None
    timer: object = None
    frames: int = 0
    empty_frames: int = 0


class WindowCaptureBroker(QObject):
    requested = Signal(object)
    canceled = Signal(object)
    finished = Signal(object)

    def __init__(self, app, *, geometry_reader=read_geometry, session_factory=_QtSession):
        if not isinstance(app, QGuiApplication) or QThread.currentThread() != app.thread():
            raise CaptureError('窗口采集必须在 QApplication 主线程初始化')
        super().__init__(app)
        self._geometry = geometry_reader
        self._factory = session_factory
        self._lock = threading.Lock()
        self._pending = None
        self._closed = False
        self.requested.connect(self._start, Qt.ConnectionType.QueuedConnection)
        self.canceled.connect(self._cancel, Qt.ConnectionType.QueuedConnection)

    def capture(self, hwnd, rect, *, timeout_ms=2500):
        if not hwnd or timeout_ms <= 0:
            logger.warning('窗口采集目标或期限无效 hwnd=%s', hwnd)
            return None
        request = _Request(int(hwnd), tuple(rect), time.monotonic() + timeout_ms / 1000)
        with self._lock:
            if self._closed or self._pending is not None:
                logger.warning('窗口采集关闭或忙碌 hwnd=%s', hwnd)
                return None
            self._pending = request
        if QThread.currentThread() == self.thread():
            loop = QEventLoop()
            def finished(candidate):
                if candidate is request:
                    loop.quit()
            self.finished.connect(finished)
            try:
                self._start(request)
                if not request.done.is_set():
                    loop.exec()
            finally:
                self.finished.disconnect(finished)
        else:
            self.requested.emit(request)
            if not request.done.wait(max(0, request.deadline - time.monotonic())):
                request.canceled.set()
                self.canceled.emit(request)
                logger.warning('窗口采集 worker 超时 hwnd=%s', hwnd)
                return None
        result = request.result
        # Late native callbacks may retain the request until their event is
        # discarded. Transfer the pixel array to the caller, rather than keeping
        # another full-resolution copy alive through those callbacks.
        request.result = None
        return result

    @Slot(object)
    def _start(self, request):
        if not self._current(request):
            return
        if self._expired(request):
            self._finish(request, None, '请求已取消或超时')
            return
        try:
            request.geometry = self._geometry(request.hwnd)
            if not _contains(request.geometry.client_rect, request.rect):
                raise CaptureError('请求矩形已超出当前客户区')
            request.timer = QTimer(self)
            request.timer.setSingleShot(True)
            request.timer.timeout.connect(lambda: self._finish(request, None, '等待新帧超时'))
            request.timer.start(max(1, int((request.deadline - time.monotonic()) * 1000)))
            request.session = self._factory(
                request.hwnd, self,
                lambda frame: self._frame(request, frame),
                lambda error: self._finish(request, None, f'Qt 采集错误: {error}'))
            if not self._current(request):
                # A native setup error can be emitted before the factory returns.
                request.session.close()
                return
            if self._expired(request):
                self._finish(request, None, '初始化完成时请求已取消或超时')
                return
            request.session.start()
        except Exception as error:
            self._finish(request, None, str(error))

    def _current(self, request):
        return self._pending is request and not request.done.is_set()

    def _expired(self, request):
        return self._closed or request.canceled.is_set() or time.monotonic() >= request.deadline

    def _frame(self, request, frame):
        if not self._current(request):
            return
        if self._expired(request):
            self._finish(request, None, '新帧到达时请求已超时或取消')
            return
        try:
            request.frames += 1
            rgb = frame_rgb(frame)
            if rgb is None:
                request.empty_frames += 1
                return  # WGC can emit empty frames before a fresh texture arrives.
            if self._geometry(request.hwnd) != request.geometry:
                raise CaptureError('窗口几何、DPI 或身份已改变')
            result = crop_client(rgb, request.geometry, request.rect)
            if not np.any(result > 3):
                raise CaptureError('窗口返回黑帧')
            if self._expired(request):
                raise CaptureError('帧转换完成时请求已超时或取消')
            self._finish(request, result)
        except Exception as error:
            self._finish(request, None, str(error))

    @Slot(object)
    def _cancel(self, request):
        if self._current(request):
            self._finish(request, None, '请求已取消')

    def _finish(self, request, result, error=None):
        if not self._current(request):
            return
        if request.timer is not None:
            request.timer.stop()
            request.timer.deleteLater()
        if request.session is not None:
            try:
                request.session.close()
            except Exception as cleanup_error:
                logger.error('窗口采集清理失败 hwnd=%s: %s', request.hwnd, cleanup_error)
                result = None
        if error:
            logger.warning('窗口采集失败 hwnd=%s rect=%s frames=%d empty=%d: %s',
                           request.hwnd, request.rect, request.frames, request.empty_frames, error)
        request.result = result
        request.done.set()
        with self._lock:
            if self._pending is request:
                self._pending = None
        self.finished.emit(request)

    @Slot()
    def close(self):
        if QThread.currentThread() != self.thread():
            raise CaptureError('窗口采集必须在 QApplication 主线程关闭')
        with self._lock:
            self._closed = True
            pending = self._pending
        if pending is not None:
            self._finish(pending, None, '应用关闭')

"""程序入口。"""
import sys

from app.privileges import ensure_administrator
from app.version import APP_NAME, VERSION


def enable_dpi_awareness():
    # Do not import window/logger/config until elevation has been checked.
    from app.window import enable_dpi_awareness as initialize
    initialize()


def main() -> int:
    if not ensure_administrator():
        return 1
    enable_dpi_awareness()

    verify_once = '--scan-once' in sys.argv

    from app.config import CONFIG
    if verify_once:
        # An entry-point default, visible and editable before manual start.
        # A later UI frequency of 0 or a positive number takes precedence.
        CONFIG.scan_frequency_minutes = None
    CONFIG.scan_once_only = False

    from PySide6.QtWidgets import QApplication
    from app.ui.main_window import MainWindow

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(VERSION)
    from app.capture import initialize_window_capture
    initialize_window_capture(app)

    win = MainWindow()
    win.show()
    if verify_once:
        # 验收入口默认频率留空；用户仍可改为连续或周期扫描。
        import json
        import time
        from pathlib import Path
        from dataclasses import asdict
        target = Path(CONFIG.log_dir)/'acceptance-latest.json'
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps({'status':'waiting_for_manual_start'}, ensure_ascii=False), encoding='utf-8')
        def record(result):
            data = asdict(result)
            data['account_uid'] = win.scanner.account_uid
            data['status'] = 'complete' if result.complete else 'incomplete'
            data['wall_seconds'] = (time.perf_counter()-win.scan_started_at
                                    if win.scan_started_at is not None else None)
            data['wall_clock_start'] = 'start_scan_button'
            account_target = win.scanner.evidence_root/'acceptance-latest.json'
            account_target.parent.mkdir(parents=True, exist_ok=True)
            account_target.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
            win.showNormal()
        win.bridge.result.connect(record)
        win._append_log('验收入口默认频率留空、扫描一次；可改为 0 连续或正数分钟周期，再手动点击「快速扫描」')
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())

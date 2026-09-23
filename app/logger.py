"""日志模块：常规运行日志 + OCR 识别失败日志。"""
from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler

from app.config import CONFIG

_initialized = False

# 通用日志
logger = logging.getLogger("genshin_scanner")

# OCR 失败日志（单独记录识别失败/低置信度的文本，便于排查）
ocr_fail_logger = logging.getLogger("genshin_ocr_fail")

# OCR 全量 dump 日志（记录每屏识别到的所有文本与坐标，用于排查识别不到昵称）
ocr_dump_logger = logging.getLogger("genshin_ocr_dump")


def _setup() -> None:
    global _initialized
    if _initialized:
        return
    _initialized = True

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # 控制台
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    logger.addHandler(console)
    logger.setLevel(logging.INFO)

    # 运行日志文件（滚动）
    run_file = os.path.join(CONFIG.log_dir, "scan.log")
    fh = RotatingFileHandler(run_file, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    # OCR 失败日志文件
    fail_file = os.path.join(CONFIG.log_dir, "ocr_fail.log")
    ffh = RotatingFileHandler(fail_file, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8")
    ffh.setFormatter(fmt)
    ocr_fail_logger.addHandler(ffh)
    ocr_fail_logger.setLevel(logging.WARNING)
    ocr_fail_logger.propagate = False

    # OCR 全量 dump 日志文件（较大，仅保留 2 份）
    dump_file = os.path.join(CONFIG.log_dir, "ocr_dump.log")
    dfh = RotatingFileHandler(dump_file, maxBytes=10 * 1024 * 1024, backupCount=2, encoding="utf-8")
    dfh.setFormatter(fmt)
    ocr_dump_logger.addHandler(dfh)
    ocr_dump_logger.setLevel(logging.INFO)
    ocr_dump_logger.propagate = False


_setup()

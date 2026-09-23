"""验证脚本：用示例图片端到端测试 OCR + parser。

打印识别到的好友条目（昵称/状态/等级/头像坐标），用于人工核对排版。

用法：
    python scripts/verify_parse.py [图片路径]
不带参数时默认读取项目根目录下的「示例图片.png」。
"""
import sys
import os

# 脚本位于 scripts/ 下，需把项目根目录加入 sys.path 以导入 app 包
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import numpy as np
from PIL import Image

from app import ocr_engine, parser
from app.ocr_engine import OcrResult

IMG = os.path.join(ROOT, "示例图片.png")


def main():
    img_path = sys.argv[1] if len(sys.argv) > 1 else IMG
    img = Image.open(img_path).convert("RGB")
    arr = np.asarray(img)
    print("图片尺寸:", arr.shape)

    results = ocr_engine.recognize(arr)
    print("\n=== OCR 结果（%d 条）===" % len(results))
    for r in results:
        h = parser._text_height(r)
        print("[%.2f] h=%.0f  (%d,%d)  %r" % (r.confidence, h, int(r.left), int(r.top), r.text))

    entries = parser.parse(results, img=arr)
    print("\n=== parse 结果（%d 个好友）===" % len(entries))
    for e in entries:
        print("昵称=%r  状态=%s  位置=%r  等级=%s  最近=%s  头像=(%d,%d)  conf=%.2f"
              % (e.nickname, e.status, e.location, e.level, e.last_seen, e.click_x, e.click_y, e.confidence))
        print("    来源: %s" % e.source)


if __name__ == "__main__":
    main()

"""图像处理: QImage<->numpy 互转、背景色/文字色估计、原文擦除。

核心算法:
- 背景色: 取 Box 外圈 3px 环带像素, 每通道 >>5 量化后取众数桶,
  再对桶内像素求均值 —— 对抗锯齿边缘与杂色噪声稳健。
- 文字色: Box 内与背景色 L1 距离 > 100 的像素视为墨水像素, 取其均值;
  对比度不足时按背景明度回退为纯黑/纯白, 保证译文可读。
- 擦除: 主背景色对外扩 2px 的四边形做纯色填充 (可选 TELEA inpaint)。
"""
from dataclasses import dataclass

import cv2
import numpy as np
from PySide6.QtGui import QImage

from .ocr.pipeline import OcrLine


# ---------------- QImage <-> numpy ----------------

def qimage_to_bgr(qimg: QImage) -> np.ndarray:
    img = qimg.convertToFormat(QImage.Format_BGR888)
    h, w = img.height(), img.width()
    buf = np.frombuffer(img.constBits(), np.uint8)
    return buf.reshape(h, img.bytesPerLine())[:, :w * 3].reshape(h, w, 3).copy()


def bgr_to_qimage(img: np.ndarray) -> QImage:
    img = np.ascontiguousarray(img)
    h, w = img.shape[:2]
    return QImage(img.data, w, h, img.strides[0], QImage.Format_BGR888).copy()


# ---------------- 颜色估计 ----------------

def _lum(c) -> float:
    b, g, r = float(c[0]), float(c[1]), float(c[2])
    return 0.114 * b + 0.587 * g + 0.299 * r


def _border_dominant_color(img: np.ndarray, rect, pad: int = 3) -> tuple:
    """Box 外圈环带的主色 = 量化众数桶内像素均值。"""
    x, y, w, h = rect
    ih, iw = img.shape[:2]
    x0, y0 = max(0, x - pad), max(0, y - pad)
    x1, y1 = min(iw, x + w + pad), min(ih, y + h + pad)
    region = img[y0:y1, x0:x1]
    mask = np.ones(region.shape[:2], bool)
    mask[y - y0:y - y0 + h, x - x0:x - x0 + w] = False       # 挖掉内部, 只留外圈
    px = region[mask]
    if px.size == 0:
        px = region.reshape(-1, 3)
    q = px >> 5                                              # 每通道 8 档量化
    keys = q[:, 0].astype(np.int32) * 64 + q[:, 1] * 8 + q[:, 2]
    top = np.bincount(keys).argmax()
    sel = px[keys == top]
    return tuple(int(v) for v in sel.mean(0))


def _text_color(img: np.ndarray, rect, bg: tuple, min_px: int = 12) -> tuple:
    x, y, w, h = rect
    roi = img[y:y + h, x:x + w].reshape(-1, 3).astype(np.int32)
    dist = np.abs(roi - np.array(bg, np.int32)).sum(1)
    ink = roi[dist > 100]                                    # 远离背景色 => 墨水像素
    if len(ink) < min_px:
        return (20, 20, 20) if _lum(bg) > 127 else (240, 240, 240)
    c = ink.mean(0)
    if abs(_lum(c) - _lum(bg)) < 60:                         # 对比度兜底: 黑或白
        return (20, 20, 20) if _lum(bg) > 127 else (240, 240, 240)
    return tuple(int(v) for v in c)


def _expand(box: np.ndarray, margin: float = 2.0) -> np.ndarray:
    """四边形各顶点沿远离质心方向外推 margin 像素, 盖住抗锯齿残边。"""
    c = box.mean(0)
    v = box - c
    n = np.linalg.norm(v, axis=1, keepdims=True)
    n[n == 0] = 1
    return box + v / n * margin


# ---------------- 分析 + 擦除 ----------------

@dataclass
class StyledLine:
    box: np.ndarray          # (4,2)
    text: str
    bg: tuple                # BGR 主背景色
    fg: tuple                # BGR 译文颜色


def analyze_and_erase(img: np.ndarray, lines: list[OcrLine],
                      mode: str = "solid") -> tuple[np.ndarray, list[StyledLine]]:
    ih, iw = img.shape[:2]
    styled: list[StyledLine] = []
    for ln in lines:
        x, y, w, h = cv2.boundingRect(ln.box.astype(np.int32))
        x, y = max(0, x), max(0, y)
        w, h = min(w, iw - x), min(h, ih - y)
        if w <= 1 or h <= 1:
            continue
        bg = _border_dominant_color(img, (x, y, w, h))
        fg = _text_color(img, (x, y, w, h), bg)
        styled.append(StyledLine(ln.box.copy(), ln.text, bg, fg))

    out = img.copy()
    if mode == "inpaint":
        mask = np.zeros((ih, iw), np.uint8)
        for s in styled:
            cv2.fillPoly(mask, [_expand(s.box).astype(np.int32)], 255)
        out = cv2.inpaint(out, mask, 3, cv2.INPAINT_TELEA)
    else:
        for s in styled:
            cv2.fillPoly(out, [_expand(s.box).astype(np.int32)],
                         tuple(int(c) for c in s.bg))
    return out, styled

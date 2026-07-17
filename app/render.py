"""译文自适应渲染: 二分字号 + CJK 折行, 把译文恰好塞进原文 Box。

Box 可能带旋转角(倾斜文字), 用 QPainter 平移到 Box 中心并旋转坐标系,
在局部坐标内按行水平居中、整体垂直居中绘制。
"""
import math
import re

import numpy as np
from PySide6.QtCore import QPointF
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QImage, QPainter

from .imgproc import StyledLine, bgr_to_qimage

# 拉丁词(带尾随空格)作为整体 token, 其余(CJK/标点)逐字成 token
_TOKEN_RE = re.compile(r"[A-Za-z0-9'\-]+\s?|.")


def _geometry(box: np.ndarray) -> tuple[float, float, np.ndarray, float]:
    tl, tr, br, bl = box
    w = (np.linalg.norm(tr - tl) + np.linalg.norm(br - bl)) / 2
    h = (np.linalg.norm(bl - tl) + np.linalg.norm(br - tr)) / 2
    angle = math.degrees(math.atan2(float(tr[1] - tl[1]), float(tr[0] - tl[0])))
    return float(w), float(h), box.mean(0), angle


def _wrap(text: str, fm: QFontMetricsF, max_w: float):
    """贪心折行; 返回 None 表示当前字号下有单字放不下 (需缩小字号)。"""
    toks = []
    for t in _TOKEN_RE.findall(text):
        if fm.horizontalAdvance(t) > max_w and len(t.strip()) > 1:
            toks.extend(list(t))                    # 超宽英文单词强制拆字符
        else:
            toks.append(t)
    lines, cur, cur_w = [], "", 0.0
    for t in toks:
        tw = fm.horizontalAdvance(t)
        if tw > max_w:
            return None
        if cur and cur_w + tw > max_w:
            lines.append(cur)
            cur, cur_w = "", 0.0
        if not cur and t == " ":                    # 行首空格丢弃
            continue
        cur += t
        cur_w += tw
    if cur:
        lines.append(cur)
    return lines or [""]


def _make_font(family: str, px: int) -> QFont:
    """Medium 字重 + 全休整提示: 小字号下笔画更实, 提升可读性。"""
    font = QFont(family)
    font.setPixelSize(px)
    font.setWeight(QFont.Weight.Medium)
    return font


def _fit(text: str, family: str, box_w: float, box_h: float):
    """二分查找最大字号, 使折行后的文本块 (行数*行高, 最宽行) 塞进 Box。"""
    lo, hi, best = 7, max(8, int(box_h)), None
    while lo <= hi:
        mid = (lo + hi) // 2
        font = _make_font(family, mid)
        fm = QFontMetricsF(font)
        lines = _wrap(text, fm, box_w)
        if lines is not None and len(lines) * fm.height() <= box_h + 1:
            best = (font, lines)
            lo = mid + 1
        else:
            hi = mid - 1
    if best is None:                                # 极小 Box: 用最小字号硬排
        font = _make_font(family, 7)
        best = (font, _wrap(text, QFontMetricsF(font), box_w) or [text])
    return best


def draw_translations(erased_bgr: np.ndarray, styled: list[StyledLine],
                      translations: list[str],
                      font_family: str = "Microsoft YaHei UI") -> QImage:
    qimg = bgr_to_qimage(erased_bgr).convertToFormat(QImage.Format_ARGB32_Premultiplied)
    p = QPainter(qimg)
    p.setRenderHint(QPainter.Antialiasing)
    p.setRenderHint(QPainter.TextAntialiasing)
    for s, tr in zip(styled, translations):
        text = (tr or s.text).strip()
        if text:
            _draw_one(p, s, text, font_family)
    p.end()
    return qimg


def _draw_one(p: QPainter, s: StyledLine, text: str, family: str):
    w, h, center, angle = _geometry(s.box)
    if w < 4 or h < 4:
        return
    font, lines = _fit(text, family, w * 0.98, h)
    fm = QFontMetricsF(font)
    p.save()
    p.translate(float(center[0]), float(center[1]))
    if abs(angle) > 1.0:                            # 跟随原文倾斜角
        p.rotate(angle)
    p.setFont(font)
    p.setPen(QColor(int(s.fg[2]), int(s.fg[1]), int(s.fg[0])))   # BGR -> RGB
    total_h = len(lines) * fm.height()
    y = -total_h / 2 + fm.ascent()
    for ln in lines:
        lw = fm.horizontalAdvance(ln)
        p.drawText(QPointF(-lw / 2, y), ln)         # 行内水平居中
        y += fm.height()
    p.restore()

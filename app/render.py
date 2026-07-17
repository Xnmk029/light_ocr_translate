"""译文段落渲染: 以原文真实字高为字号基准, 段内统一字号, 行距保真。

排版规则:
- 字号基准 = 段落原文墨水字高 (font_px), 只缩不放 => 译文与原文视觉等大,
  杜绝"小框巨字"; 放不下时逐级缩小 (下限 8px)。
- 段内所有行同一字号; 行距按原文行距等比缩放 => 版面节奏与原文一致。
- 多行段左对齐 + 顶端对齐 (跟随原段落), 单行块水平垂直居中。
- 避头尾: 句读类标点 (。，、！？…) 不落行首, 允许压行尾少量溢出。
- 倾斜单行块沿原文角度旋转渲染。
"""
import math
import re

import numpy as np
from PySide6.QtCore import QPointF
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QImage, QPainter

from .imgproc import StyledBlock, bgr_to_qimage

# 拉丁词(带尾随空格)作为整体 token, 其余(CJK/标点)逐字成 token
_TOKEN_RE = re.compile(r"[A-Za-z0-9'\-]+\s?|.")
# 不可落行首的标点 (避头)
_NO_HEAD = set("。，、！？；：…）】」』》〉%％°℃·,.;:!?)]}\"'")


def _make_font(family: str, px: int) -> QFont:
    """小字号加重字重: <=13px 用 DemiBold, 其余 Medium, 笔画更实更亮。"""
    font = QFont(family)
    font.setPixelSize(px)
    font.setWeight(QFont.Weight.DemiBold if px <= 13 else QFont.Weight.Medium)
    return font


def _geometry(box: np.ndarray) -> tuple[float, float, np.ndarray, float]:
    tl, tr, br, bl = box
    w = (np.linalg.norm(tr - tl) + np.linalg.norm(br - bl)) / 2
    h = (np.linalg.norm(bl - tl) + np.linalg.norm(br - tr)) / 2
    angle = math.degrees(math.atan2(float(tr[1] - tl[1]), float(tr[0] - tl[0])))
    return float(w), float(h), box.mean(0), angle


def _wrap(text: str, fm: QFontMetricsF, max_w: float):
    """贪心折行 + 避头尾; 返回 None 表示当前字号下有单 token 放不下。"""
    text = text.replace("\n", " ")
    toks: list[str] = []
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
            if t in _NO_HEAD:                       # 避头: 标点压到行尾 (允许微溢出)
                cur += t
                cur_w += tw
                continue
            lines.append(cur)
            cur, cur_w = "", 0.0
        if not cur and t == " ":
            continue
        cur += t
        cur_w += tw
    if cur:
        lines.append(cur)
    return lines or [""]


def _fit_block(text: str, family: str, box_w: float, box_h: float,
               target_px: float, pitch_ratio: float):
    """从原文字高开始只缩不放, 找到能塞进段落框的最大字号。

    返回 (font, fm, lines, line_h)。允许纵向溢出 10% (溢出区是原背景, 无碍)。
    """
    size = max(8, int(round(target_px)))
    while size >= 8:
        font = _make_font(family, size)
        fm = QFontMetricsF(font)
        lines = _wrap(text, fm, box_w)
        if lines is not None:
            line_h = max(fm.height(), size * pitch_ratio)
            total = (len(lines) - 1) * line_h + fm.height()
            if total <= box_h * 1.10 + 2:
                return font, fm, lines, line_h
        size -= 1
    font = _make_font(family, 8)
    fm = QFontMetricsF(font)
    lines = _wrap(text, fm, box_w) or [text]
    return font, fm, lines, fm.height()


def draw_translations(erased_bgr: np.ndarray, styled: list[StyledBlock],
                      translations: list[str],
                      font_family: str = "Microsoft YaHei UI") -> QImage:
    qimg = bgr_to_qimage(erased_bgr).convertToFormat(QImage.Format_ARGB32_Premultiplied)
    p = QPainter(qimg)
    p.setRenderHint(QPainter.Antialiasing)
    p.setRenderHint(QPainter.TextAntialiasing)
    for s, tr in zip(styled, translations):
        text = (tr or s.text).strip()
        if text:
            _draw_block(p, s, text, font_family)
    p.end()
    return qimg


def _draw_block(p: QPainter, s: StyledBlock, text: str, family: str):
    w, h, center, angle = _geometry(s.box)
    if w < 4 or h < 4:
        return
    target = getattr(s, "font_px", 0) or h * 0.75
    pitch = getattr(s, "line_pitch", 0) or target * 1.25
    pitch_ratio = pitch / max(target, 1.0)
    font, fm, lines, line_h = _fit_block(text, family, w * 0.99, h, target, pitch_ratio)

    p.save()
    p.translate(float(center[0]), float(center[1]))
    if s.angled and abs(angle) > 1.0:
        p.rotate(angle)
    p.setFont(font)
    p.setPen(QColor(int(s.fg[2]), int(s.fg[1]), int(s.fg[0])))   # BGR -> RGB
    total = (len(lines) - 1) * line_h + fm.height()
    if s.single or len(lines) == 1:
        y = -total / 2 + fm.ascent()                 # 单行/标题: 垂直居中
    else:
        y = -h / 2 + fm.ascent()                     # 多行段: 顶端对齐原段落
    for ln in lines:
        lw = fm.horizontalAdvance(ln)
        x = -lw / 2 if s.single else -w / 2          # 单行居中, 多行左对齐
        p.drawText(QPointF(x, y), ln)
        y += line_h
    p.restore()

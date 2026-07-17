"""版面分析: 把 OCR 文本行聚合成段落块 (在等待 LLM 回传前的本地二次分析)。

为什么不做"二次 OCR": 重跑检测模型只能给出同样的行级框 (+300ms);
真正缺的是行与行的"结构关系"和"真实字高", 二者用几何启发式 + 像素
投影即可拿到 (<5ms, 确定性)。

聚合规则 (相邻两行可堆叠为同段):
1. 均近似水平 (|angle| < 3°);
2. 字高相近 (比值 0.55 ~ 1.8);
3. 垂直间隙 < 0.8 倍行高 (允许轻微重叠);
4. 水平范围重叠 >= 40%, 或左边缘对齐 (缩进差 < 1.2 倍字高);
5. 不在同一视觉行上 (排除多栏并排的行)。

跨行合并文本: CJK 相邻直接拼接; 拉丁词间补空格; 行尾连字符断词还原。
"""
import math
from dataclasses import dataclass, field

import numpy as np

from .ocr.pipeline import OcrLine


def _rect(ln: OcrLine) -> tuple[float, float, float, float]:
    b = ln.box
    return (float(b[:, 0].min()), float(b[:, 1].min()),
            float(b[:, 0].max()), float(b[:, 1].max()))


def line_angle(ln: OcrLine) -> float:
    tl, tr = ln.box[0], ln.box[1]
    return math.degrees(math.atan2(float(tr[1] - tl[1]), float(tr[0] - tl[0])))


def _is_cjk(ch: str) -> bool:
    if not ch:
        return False
    o = ord(ch)
    return (0x2E80 <= o <= 0x9FFF or 0xAC00 <= o <= 0xD7AF
            or 0xF900 <= o <= 0xFAFF or 0xFF00 <= o <= 0xFFEF
            or 0x3000 <= o <= 0x303F)


def merge_text(lines: list[OcrLine]) -> str:
    """按排版语义合并多行原文。"""
    out = lines[0].text
    for nxt in lines[1:]:
        t = nxt.text
        if not t:
            continue
        if not out:
            out = t
            continue
        a, b = out[-1], t[0]
        if a == "-" and b.isalpha() and b.islower():
            out = out[:-1] + t              # 行尾连字符断词: cre- + ate -> create
        elif _is_cjk(a) or _is_cjk(b):
            out += t                        # CJK 直接拼接
        else:
            out += " " + t                  # 拉丁词间补空格
    return out


@dataclass
class Block:
    lines: list[OcrLine] = field(default_factory=list)

    @property
    def text(self) -> str:
        return merge_text(self.lines)


def _stackable(prev: OcrLine, nxt: OcrLine) -> float:
    """nxt 能否堆叠到 prev 下方成段; 返回负值=不能, 否则返回间隙 (越小越优)。"""
    if abs(line_angle(prev)) > 3 or abs(line_angle(nxt)) > 3:
        return -1.0
    x0a, y0a, x1a, y1a = _rect(prev)
    x0b, y0b, x1b, y1b = _rect(nxt)
    ha, hb = y1a - y0a, y1b - y0b
    if ha <= 0 or hb <= 0:
        return -1.0
    ratio = hb / ha
    if not (0.55 <= ratio <= 1.8):
        return -1.0
    h = max(ha, hb)
    gap = y0b - y1a
    if gap < -0.4 * h or gap > 0.8 * h:
        return -1.0
    cy_a, cy_b = (y0a + y1a) / 2, (y0b + y1b) / 2
    if abs(cy_b - cy_a) < 0.6 * h:          # 同一视觉行 => 多栏并排, 不合并
        return -1.0
    inter = min(x1a, x1b) - max(x0a, x0b)
    overlap_ok = inter > 0.4 * min(x1a - x0a, x1b - x0b)
    align_ok = abs(x0a - x0b) < 1.2 * h
    if not (overlap_ok or align_ok):
        return -1.0
    return max(gap, 0.0)


def group_blocks(lines: list[OcrLine]) -> list[Block]:
    """lines 需已按阅读序排序; 贪心地把每行挂到最近的可堆叠块尾部。"""
    blocks: list[list[OcrLine]] = []
    for ln in lines:
        best, best_gap = None, 1e9
        for b in blocks:
            g = _stackable(b[-1], ln)
            if g >= 0 and g < best_gap:
                best, best_gap = b, g
        if best is not None:
            best.append(ln)
        else:
            blocks.append([ln])
    return [Block(b) for b in blocks]

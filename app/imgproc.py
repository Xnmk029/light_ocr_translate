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


def _push_contrast(fg, bg, min_gap: float = 150.0) -> tuple:
    """沿亮度方向把前景色推离背景色, 并施加硬阈值 => 任何背景下文字高对比。

    规则: 深色字最终亮度 <= 40 (近黑), 浅色字最终亮度 >= 235 (近白);
    保留原文字色相 (与纯黑/纯白线性混合), 只在亮度余量不足时反转方向。
    中间灰背景下两个方向都会被推到极端, 彻底消除"灰字糊在灰底上"。
    """
    fl, bl = _lum(fg), _lum(bg)
    prefer_dark = fl <= bl
    if prefer_dark and bl < min_gap and (255 - bl) > bl:
        prefer_dark = False                          # 背景太暗, 压不出深色差 -> 转亮字
    elif not prefer_dark and (255 - bl) < min_gap and bl > (255 - bl):
        prefer_dark = True                           # 背景太亮 -> 转深字
    if prefer_dark:
        target = max(0.0, min(40.0, bl - min_gap))   # 硬阈值: 深字亮度封顶 40
        if fl <= target:
            return tuple(int(v) for v in fg)
        k = target / fl if fl > 0 else 0.0           # 与纯黑混合, 亮度线性缩放
        return tuple(int(round(v * k)) for v in fg)
    target = min(255.0, max(235.0, bl + min_gap))    # 硬阈值: 亮字亮度保底 235
    if fl >= target:
        return tuple(int(v) for v in fg)
    a = (target - fl) / (255.0 - fl) if fl < 255 else 0.0
    return tuple(int(round(v + (255 - v) * a)) for v in fg)


def _expand(box: np.ndarray, margin: float = 2.0) -> np.ndarray:
    """四边形各顶点沿远离质心方向外推 margin 像素, 盖住抗锯齿残边。"""
    c = box.mean(0)
    v = box - c
    n = np.linalg.norm(v, axis=1, keepdims=True)
    n[n == 0] = 1
    return box + v / n * margin


# ---------------- 分析 + 擦除 (段落级) ----------------

@dataclass
class StyledBlock:
    box: np.ndarray          # (4,2) 段落外接框 (倾斜单行时为原四点框)
    text: str                # 合并后的段落原文
    bg: tuple                # BGR 代表背景色
    fg: tuple                # BGR 译文颜色 (已强制高对比)
    font_px: float           # 原文真实墨水字高 (物理像素) => 译文字号基准
    line_pitch: float        # 原文行距 (相邻行中心距)
    single: bool             # 单行块 => 居中排版
    angled: bool = False     # 倾斜块 => 旋转渲染


def _ink_stats(img: np.ndarray, rect, bg: tuple, thresh: int = 100):
    """墨水投影: 返回 (真实字高, 墨水像素采样)。

    行框经 DB unclip 外扩约 30%, 直接用框高当字号会让译文偏大。
    以与背景的 L1 色距 > thresh 判定墨水像素, 对行方向做投影,
    首末有效行距离即为真实字形高度。
    """
    x, y, w, h = rect
    roi = img[y:y + h, x:x + w].astype(np.int32)
    dist = np.abs(roi - np.array(bg, np.int32)).sum(2)
    mask = dist > thresh
    rows = mask.sum(1)
    good = np.where(rows > max(2, w * 0.03))[0]
    ink_h = float(good[-1] - good[0] + 1) if len(good) else h * 0.7
    px = roi[mask]
    if len(px) > 1500:
        px = px[:: len(px) // 1500]
    return ink_h, px


def restore_regions(erased: np.ndarray, original: np.ndarray,
                    styled_pending: list["StyledBlock"], margin: float = 4.0) -> np.ndarray:
    """把未完成翻译的块从原图拷回擦除图 (增量上屏时未译块保持原文可读)。"""
    out = erased.copy()
    if not styled_pending:
        return out
    ih, iw = out.shape[:2]
    for s in styled_pending:
        x, y, w, h = cv2.boundingRect(_expand(s.box, margin).astype(np.int32))
        x0, y0 = max(0, x), max(0, y)
        x1, y1 = min(iw, x + w), min(ih, y + h)
        if x1 > x0 and y1 > y0:
            out[y0:y1, x0:x1] = original[y0:y1, x0:x1]
    return out


def analyze_and_erase(img: np.ndarray, blocks,
                      mode: str = "solid") -> tuple[np.ndarray, list[StyledBlock]]:
    """段落级分析: 逐行测色/测字高 -> 聚合出块级样式; 擦除仍逐行进行,
    并把行框垂直扩展到与邻行中点相接, 填掉行间隙防止译文行距不同时露出原文。
    """
    from .layout import line_angle
    ih, iw = img.shape[:2]
    styled: list[StyledBlock] = []
    rect_jobs: list[tuple] = []      # (x0,y0,x1,y1,bg) 轴对齐擦除
    poly_jobs: list[tuple] = []      # (poly, bg) 倾斜块擦除

    for blk in blocks:
        infos = []
        for ln in blk.lines:
            x, y, w, h = cv2.boundingRect(ln.box.astype(np.int32))
            x, y = max(0, x), max(0, y)
            w, h = min(w, iw - x), min(h, ih - y)
            if w <= 1 or h <= 1:
                continue
            bg = _border_dominant_color(img, (x, y, w, h))
            ink_h, ink_px = _ink_stats(img, (x, y, w, h), bg)
            infos.append((ln, (x, y, w, h), bg, ink_h, ink_px))
        if not infos:
            continue

        # 代表背景色: 按亮度取中位; 前景色: 全块墨水像素均值 + 强制对比
        bgs = sorted((i[2] for i in infos), key=_lum)
        bg_rep = bgs[len(bgs) // 2]
        inks = [i[4] for i in infos if len(i[4])]
        if inks and sum(len(p) for p in inks) >= 12:
            raw = np.concatenate(inks).mean(0)
            fg = _push_contrast(tuple(float(v) for v in raw), bg_rep)
        else:
            fg = (10, 10, 10) if _lum(bg_rep) > 127 else (252, 252, 252)

        font_px = max(7.0, float(np.median([i[3] for i in infos])))
        centers = [r[1] + r[3] / 2 for _, r, _, _, _ in infos]
        pitch = (float(np.median(np.diff(centers))) if len(centers) > 1
                 else font_px * 1.3)
        pitch = max(pitch, font_px * 1.05)

        angled = len(infos) == 1 and abs(line_angle(infos[0][0])) > 3
        if angled:
            box = infos[0][0].box.copy()
        else:
            x0 = min(r[0] for _, r, _, _, _ in infos)
            y0 = min(r[1] for _, r, _, _, _ in infos)
            x1 = max(r[0] + r[2] for _, r, _, _, _ in infos)
            y1 = max(r[1] + r[3] for _, r, _, _, _ in infos)
            box = np.array([[x0, y0], [x1, y0], [x1, y1], [x0, y1]], np.float32)

        styled.append(StyledBlock(box=box, text=blk.text, bg=bg_rep, fg=fg,
                                  font_px=font_px, line_pitch=pitch,
                                  single=len(infos) == 1, angled=angled))

        # 擦除 job: 行框垂直扩展到邻行中点 (填平行间隙), 水平 +2px
        for k, (ln, (x, y, w, h), bg, _, _) in enumerate(infos):
            if angled:
                poly_jobs.append((_expand(ln.box, 2), bg))
                continue
            if k > 0:
                py, ph = infos[k - 1][1][1], infos[k - 1][1][3]
                ext_t = min(max(2.0, (y - (py + ph)) / 2), h * 0.6)
            else:
                ext_t = min(3.0, h * 0.3)
            if k < len(infos) - 1:
                ny = infos[k + 1][1][1]
                ext_b = min(max(2.0, (ny - (y + h)) / 2), h * 0.6)
            else:
                ext_b = min(3.0, h * 0.3)
            rect_jobs.append((x - 2, y - ext_t, x + w + 2, y + h + ext_b, bg))

    out = img.copy()
    if mode == "inpaint":
        m = np.zeros((ih, iw), np.uint8)
        for x0, y0, x1, y1, _ in rect_jobs:
            m[max(0, int(y0)):min(ih, int(y1)), max(0, int(x0)):min(iw, int(x1))] = 255
        for poly, _ in poly_jobs:
            cv2.fillPoly(m, [poly.astype(np.int32)], 255)
        out = cv2.inpaint(out, m, 3, cv2.INPAINT_TELEA)
    else:
        for x0, y0, x1, y1, bg in rect_jobs:
            xa, ya = max(0, int(x0)), max(0, int(y0))
            xb, yb = min(iw, int(round(x1))), min(ih, int(round(y1)))
            if xb > xa and yb > ya:
                out[ya:yb, xa:xb] = bg
        for poly, bg in poly_jobs:
            cv2.fillPoly(out, [poly.astype(np.int32)], tuple(int(c) for c in bg))
    return out, styled

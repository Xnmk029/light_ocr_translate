"""OCR 管线: 检测 -> 透视裁剪 -> 识别 -> 阅读序排序。"""
import os
from dataclasses import dataclass

import cv2
import numpy as np

from .det import DBDetector
from .rec import CTCRecognizer


@dataclass
class OcrLine:
    box: np.ndarray      # (4,2) float32, 左上/右上/右下/左下
    text: str
    conf: float


def get_rotate_crop_image(img: np.ndarray, points: np.ndarray) -> np.ndarray:
    """四边形透视矫正为水平矩形; 竖排 (h/w>=1.5) 旋转 90° 供识别。"""
    pts = points.astype(np.float32)
    w = max(1, int(max(np.linalg.norm(pts[0] - pts[1]), np.linalg.norm(pts[2] - pts[3]))))
    h = max(1, int(max(np.linalg.norm(pts[0] - pts[3]), np.linalg.norm(pts[1] - pts[2]))))
    dst = np.float32([[0, 0], [w, 0], [w, h], [0, h]])
    m = cv2.getPerspectiveTransform(pts, dst)
    crop = cv2.warpPerspective(img, m, (w, h),
                               borderMode=cv2.BORDER_REPLICATE, flags=cv2.INTER_CUBIC)
    if h / w >= 1.5:
        crop = np.ascontiguousarray(np.rot90(crop))
    return crop


def _sort_reading_order(boxes: list[np.ndarray]) -> list[np.ndarray]:
    """先按垂直中心分行, 行内按 x 排序, 得到自然阅读顺序。"""
    if not boxes:
        return boxes
    boxes = sorted(boxes, key=lambda b: (float(b[:, 1].mean()), float(b[:, 0].mean())))
    rows, cur = [], [boxes[0]]
    for b in boxes[1:]:
        prev = cur[-1]
        line_h = max(float(prev[:, 1].max() - prev[:, 1].min()), 8.0)
        if abs(float(b[:, 1].mean()) - float(prev[:, 1].mean())) < line_h * 0.5:
            cur.append(b)
        else:
            rows.append(cur)
            cur = [b]
    rows.append(cur)
    return [b for row in rows for b in sorted(row, key=lambda x: float(x[:, 0].mean()))]


class OcrEngine:
    def __init__(self, det_path: str, rec_path: str, charset_path: str,
                 limit_side: int = 960):
        for p in (det_path, rec_path, charset_path):
            if not os.path.exists(p):
                raise FileNotFoundError(
                    f"缺少模型文件 {os.path.basename(p)}, 请将 PP-OCR ONNX 模型放入 models 目录")
        self.det = DBDetector(det_path, limit_side)
        self.rec = CTCRecognizer(rec_path, charset_path)

    def warmup(self) -> None:
        """预热: 首次推理包含内存分配/内核编译, 提前在后台完成。"""
        blank = np.full((64, 64, 3), 255, np.uint8)
        self.det(blank)
        self.rec([np.full((32, 128, 3), 255, np.uint8)])

    def run(self, img_bgr: np.ndarray) -> list[OcrLine]:
        boxes = _sort_reading_order(self.det(img_bgr))
        if not boxes:
            return []
        crops = [get_rotate_crop_image(img_bgr, b) for b in boxes]
        recs = self.rec(crops)
        return [OcrLine(b, t.strip(), c)
                for b, (t, c) in zip(boxes, recs) if c >= 0.4 and t.strip()]

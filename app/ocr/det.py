"""DB 文本检测 (PP-OCR mobile det, 兼容 v4/v5/v6 导出的 ONNX)。

流程: 等比缩放到 32 倍数 -> ImageNet 归一化 -> ONNX 推理得概率图
      -> 二值化取轮廓 -> 按得分过滤 -> unclip 外扩还原完整字形区域
      -> 最小外接矩形四点, 映射回原图坐标。
"""
import cv2
import numpy as np
import onnxruntime as ort
import pyclipper

_MEAN = np.array([0.485, 0.456, 0.406], np.float32)
_STD = np.array([0.229, 0.224, 0.225], np.float32)


def _order_points(pts: np.ndarray) -> np.ndarray:
    """四点按 左上/右上/右下/左下 排序。"""
    pts = pts[np.argsort(pts[:, 0])]
    left = pts[:2][np.argsort(pts[:2, 1])]
    right = pts[2:][np.argsort(pts[2:, 1])]
    return np.array([left[0], right[0], right[1], left[1]], np.float32)


class DBDetector:
    def __init__(self, model_path: str, limit_side: int = 960,
                 thresh: float = 0.3, box_thresh: float = 0.5, unclip_ratio: float = 1.6):
        so = ort.SessionOptions()
        so.log_severity_level = 3
        so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        so.enable_cpu_mem_arena = False   # 不用内存池: 大输入的激活内存用完即还
        self.sess = ort.InferenceSession(model_path, sess_options=so,
                                         providers=["CPUExecutionProvider"])
        self.input_name = self.sess.get_inputs()[0].name
        self.limit_side = limit_side
        self.thresh = thresh
        self.box_thresh = box_thresh
        self.unclip_ratio = unclip_ratio

    def __call__(self, img_bgr: np.ndarray) -> list[np.ndarray]:
        h, w = img_bgr.shape[:2]
        scale = min(1.0, self.limit_side / max(h, w))
        rh = max(32, int(round(h * scale / 32)) * 32)
        rw = max(32, int(round(w * scale / 32)) * 32)
        resized = cv2.resize(img_bgr, (rw, rh))
        x = (resized.astype(np.float32) / 255.0 - _MEAN) / _STD
        blob = x.transpose(2, 0, 1)[None]
        prob = self.sess.run(None, {self.input_name: blob})[0][0, 0]
        return self._postprocess(prob, w, h, rw / w, rh / h)

    def _postprocess(self, prob, ow, oh, ratio_w, ratio_h) -> list[np.ndarray]:
        seg = (prob > self.thresh).astype(np.uint8) * 255
        contours, _ = cv2.findContours(seg, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
        boxes = []
        for cnt in contours[:200]:
            box, sside = self._min_box(cnt)
            if sside < 3:
                continue
            if self._box_score(prob, cnt) < self.box_thresh:
                continue
            expanded = self._unclip(box)
            if expanded is None:
                continue
            box, sside = self._min_box(expanded.reshape(-1, 1, 2))
            if sside < 5:
                continue
            # 概率图坐标 -> 原图坐标
            box[:, 0] = np.clip(box[:, 0] / ratio_w, 0, ow - 1)
            box[:, 1] = np.clip(box[:, 1] / ratio_h, 0, oh - 1)
            boxes.append(box)
        return boxes

    @staticmethod
    def _min_box(cnt) -> tuple[np.ndarray, float]:
        rect = cv2.minAreaRect(cnt)
        return _order_points(cv2.boxPoints(rect)), min(rect[1])

    @staticmethod
    def _box_score(prob, cnt) -> float:
        """轮廓内概率均值作为置信度 (fast 模式: 只在包围盒内算)。"""
        x, y, w, h = cv2.boundingRect(cnt)
        mask = np.zeros((h, w), np.uint8)
        cv2.drawContours(mask, [cnt - (x, y)], -1, 1, -1)
        return cv2.mean(prob[y:y + h, x:x + w], mask)[0]

    def _unclip(self, box: np.ndarray):
        """DB 输出的是收缩核, 需按 面积*ratio/周长 外扩回真实文字边界。"""
        poly = box.reshape(-1, 2)
        area = abs(cv2.contourArea(poly))
        length = cv2.arcLength(poly.reshape(-1, 1, 2).astype(np.float32), True)
        if length < 1e-3:
            return None
        dist = area * self.unclip_ratio / length
        pco = pyclipper.PyclipperOffset()
        pco.AddPath([tuple(p) for p in poly.round().astype(np.int64)],
                    pyclipper.JT_ROUND, pyclipper.ET_CLOSEDPOLYGON)
        out = pco.Execute(dist)
        if not out:
            return None
        biggest = max(out, key=lambda p: abs(cv2.contourArea(np.array(p, np.float32))))
        return np.array(biggest, np.float32)

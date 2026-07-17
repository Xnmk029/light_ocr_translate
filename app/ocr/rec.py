"""CTC 文本行识别 (PP-OCR mobile rec, 兼容 v4/v5/v6 导出的 ONNX)。

字典映射约定: 索引 0 = CTC blank, 1..N = 字典行, N+1 = 空格。
预处理: 高度缩放到模型输入高 (通常 48), 宽度按长宽比动态, 右侧补零。

内存策略:
- enable_cpu_mem_arena=False: ORT 默认内存池只增不还, 动态宽度输入会让
  池按历史最大形状持续膨胀 (数百 MB); 关闭后每次推理即时归还系统。
- 宽度预算批处理: 批内 行数*补齐宽度 <= 预算, 限制单次推理的峰值激活内存,
  避免 "8 行 x 2560 宽" 这类极端批次。
"""
import cv2
import numpy as np
import onnxruntime as ort

MAX_LINE_W = 2560          # 单行最大补齐宽度 (px)
BATCH_W_BUDGET = 3072      # 批内 count*W 预算: 窄行多条并批, 宽行单条推理


class CTCRecognizer:
    def __init__(self, model_path: str, charset_path: str, batch: int = 8):
        so = ort.SessionOptions()
        so.log_severity_level = 3
        so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        so.enable_cpu_mem_arena = False
        self.sess = ort.InferenceSession(model_path, sess_options=so,
                                         providers=["CPUExecutionProvider"])
        self.input_name = self.sess.get_inputs()[0].name
        with open(charset_path, encoding="utf-8") as f:
            chars = [line.rstrip("\r\n") for line in f if line.rstrip("\r\n")]
        self.charset = ["<blank>"] + chars + [" "]
        shape = self.sess.get_inputs()[0].shape          # [N,3,H,W], H/W 可能为动态
        self.h = shape[2] if isinstance(shape[2], int) else 48
        self.fixed_w = shape[3] if isinstance(shape[3], int) else None
        self.batch = batch

    def _padded_w(self, img: np.ndarray) -> int:
        if self.fixed_w:
            return self.fixed_w
        ratio = img.shape[1] / img.shape[0]
        return min(max(int(np.ceil(self.h * ratio)), 16), MAX_LINE_W)

    def __call__(self, crops: list[np.ndarray]) -> list[tuple[str, float]]:
        if not crops:
            return []
        order = np.argsort([c.shape[1] / c.shape[0] for c in crops])
        results: list = [None] * len(crops)
        i, n = 0, len(order)
        while i < n:
            batch = [order[i]]
            j = i + 1
            while j < n and len(batch) < self.batch:
                w = self._padded_w(crops[order[j]])      # 排序后 j 处即当前批最大宽
                if (len(batch) + 1) * w > BATCH_W_BUDGET:
                    break
                batch.append(order[j])
                j += 1
            preds = self._infer([crops[k] for k in batch])
            for k, r in zip(batch, self._decode(preds)):
                results[k] = r
            i = j
        return results

    def _infer(self, imgs: list[np.ndarray]) -> np.ndarray:
        w = max(self._padded_w(im) for im in imgs)
        blob = np.zeros((len(imgs), 3, self.h, w), np.float32)
        for k, im in enumerate(imgs):
            rw = max(1, min(w, int(round(self.h * im.shape[1] / im.shape[0]))))
            r = cv2.resize(im, (rw, self.h)).astype(np.float32)
            r = (r / 255.0 - 0.5) / 0.5                  # 归一化到 [-1,1]
            blob[k, :, :, :rw] = r.transpose(2, 0, 1)
        return self.sess.run(None, {self.input_name: blob})[0]

    def _decode(self, preds: np.ndarray) -> list[tuple[str, float]]:
        """CTC 贪心解码: argmax -> 合并连续重复 -> 去 blank。"""
        idxs = preds.argmax(2)
        probs = preds.max(2)
        out = []
        for idx, prob in zip(idxs, probs):
            keep = np.concatenate(([True], idx[1:] != idx[:-1])) & (idx != 0)
            text = "".join(self.charset[i] for i in idx[keep] if i < len(self.charset))
            conf = float(prob[keep].mean()) if keep.any() else 0.0
            out.append((text, conf))
        return out

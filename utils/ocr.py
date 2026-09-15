import os

system_threads = os.cpu_count() or 1
CPU_THREADS = min(8, max(1, system_threads))
os.environ["OMP_NUM_THREADS"] = str(CPU_THREADS)
os.environ["MKL_NUM_THREADS"] = str(CPU_THREADS)
os.environ["OPENBLAS_NUM_THREADS"] = str(CPU_THREADS)
os.environ["VECLIB_MAXIMUM_THREADS"] = str(CPU_THREADS)
os.environ["NUMEXPR_NUM_THREADS"] = str(CPU_THREADS)

import sys
import math
import random
import argparse
import logging
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Optional, Union
import cv2 as cv
import numpy as np

try:
    import pyvips
    # Suppress pyvips logs
    logging.getLogger("pyvips").setLevel(logging.WARNING)
except ImportError:
    pyvips = None

from rapidocr import RapidOCR, OCRVersion, EngineType, ModelType
from rapidocr.utils.log import logger as rapidocr_logger

# Suppress model downloading/loading logs
rapidocr_logger.setLevel(logging.ERROR)
for handler in rapidocr_logger.handlers:
    handler.setLevel(logging.ERROR)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


# --- CONFIGURATION DEFAULTS ---
CELL_SIZE = 500
DOWNSAMPLE_FACTOR = 16
MAX_TILE_SIZE = 4000
TILE_PADDING = 30

POLY_EDGE_THRESHOLD = 30
POLY_PADDING = 20

RESIZE_LIMIT = 4000


def calculate_iou(box1, box2):
    """Calculates the overlap between 2 bounding boxes, required for filtering duplicate detections."""
    x_left = max(box1[0], box2[0])
    y_top = max(box1[1], box2[1])
    x_right = min(box1[2], box2[2])
    y_bottom = min(box1[3], box2[3])

    if x_right < x_left or y_bottom < y_top:
        return 0.0

    intersection = (x_right - x_left) * (y_bottom - y_top)
    area1 = (box1[2] - box1[0]) * (box1[3] - box1[1])
    area2 = (box2[2] - box2[0]) * (box2[3] - box2[1])

    if area1 == 0 or area2 == 0:
        return 0.0

    return intersection / float(area1 + area2 - intersection)


def clean_text(text: str) -> str:
    """Cleans recognized text by removing special characters and trimming whitespace."""
    if not text:
        return ""
    text = "".join(c for c in text if (32 <= ord(c) <= 126) or (ord(c) == 176)).strip()
    text = text.replace("$", "S").replace("|", "I").replace("=", "")
    text = text.strip(".,:;_-")
    if len(text) == 1 and not (text.isalnum() or text == "°"):
        return ""
    return text.strip()


def load_image(path):
    """Loads image from path and returns it with width and height."""
    img = cv.imread(str(path))
    if img is None:
        raise ValueError(f"Could not read image: {path}")
    h, w = img.shape[:2]
    return img, w, h


def init_ocr_engine(
    engine_type: Optional[EngineType] = None,
    threads: int = CPU_THREADS,
) -> RapidOCR:
    """Initializes and returns an optimized RapidOCR instance."""
    env_engine = (os.getenv("OCR_ENGINE_TYPE") or os.getenv("OCR_ENGINE") or "").lower()
    if env_engine == "openvino":
        engine_type = EngineType.OPENVINO
    elif env_engine == "onnxruntime":
        engine_type = EngineType.ONNXRUNTIME
    elif env_engine != "":
        raise ValueError(f"Invalid OCR engine type: {env_engine}. Please set OCR_ENGINE to 'openvino' or 'onnxruntime'.")
    elif engine_type is None:
        try:
            import openvino  # noqa: F401
            engine_type = EngineType.OPENVINO
        except ImportError:
            engine_type = EngineType.ONNXRUNTIME

    params = {
        "Global.text_score": 0.5,
        "Global.use_cls": True,
        "Det.ocr_version": OCRVersion.PPOCRV6,
        "Det.engine_type": engine_type,
        "Det.model_type": ModelType.SMALL,
        "Det.thresh": 0.3,
        "Det.box_thresh": 0.3,
        "Det.unclip_ratio": 1.2,
        "Cls.engine_type": engine_type,
        "Cls.model_type": ModelType.MOBILE,
        "Rec.ocr_version": OCRVersion.PPOCRV6,
        "Rec.engine_type": engine_type,
        "Rec.model_type": ModelType.SMALL,
    }

    if engine_type == EngineType.OPENVINO:
        params.update(
            {
                "EngineConfig.openvino.performance_hint": "LATENCY",
                "EngineConfig.openvino.num_streams": 1,
                "EngineConfig.openvino.performance_num_requests": 1,
                "EngineConfig.openvino.inference_num_threads": threads,
                "EngineConfig.openvino.enable_cpu_pinning": "YES",
            }
        )

    elif engine_type == EngineType.ONNXRUNTIME:
        params.update(
            {
                "EngineConfig.onnxruntime.use_cuda": False,
                "EngineConfig.onnxruntime.intra_op_num_threads": threads,
                "EngineConfig.onnxruntime.inter_op_num_threads": 1,
            }
        )

    logger.info(
        f"Initializing RapidOCR (PP-OCRv6 Small) with {engine_type.name} engine ({threads} threads)..."
    )

    return RapidOCR(params=params)


class FloorMapOCR:
    def __init__(self, ocr_engine: Optional[RapidOCR] = None):
        self.ocr_engine = ocr_engine if ocr_engine is not None else init_ocr_engine()

    def _generate_grid(
        self, img: Union["pyvips.Image", np.ndarray], cell_size: int = CELL_SIZE
    ) -> tuple[np.ndarray, int, int]:
        """
        Generates an occupancy grid representing active text/geometry cells using
        fast downsampling and Sobel edge detection.
        """
        if pyvips is not None and isinstance(img, pyvips.Image):
            img_w, img_h = img.width, img.height
            lowres = img.shrink(DOWNSAMPLE_FACTOR, DOWNSAMPLE_FACTOR)

            if lowres.hasalpha():
                lowres = lowres.flatten(background=[255, 255, 255])

            gray = lowres.colourspace("grey16") if lowres.bands == 3 else lowres
            mask = gray.sobel() > 12

            mask_np = mask.numpy()
        else:
            if isinstance(img, np.ndarray):
                img_bgr = img
            else:
                img_bgr = np.array(img)
            img_h, img_w = img_bgr.shape[:2]
            small_w = max(1, img_w // DOWNSAMPLE_FACTOR)
            small_h = max(1, img_h // DOWNSAMPLE_FACTOR)
            small = cv.resize(img_bgr, (small_w, small_h), interpolation=cv.INTER_AREA)
            gray = cv.cvtColor(small, cv.COLOR_BGR2GRAY) if small.ndim == 3 and small.shape[2] == 3 else small
            grad_x = cv.Sobel(gray, cv.CV_16S, 1, 0)
            grad_y = cv.Sobel(gray, cv.CV_16S, 0, 1)
            sobel = cv.addWeighted(cv.convertScaleAbs(grad_x), 0.5, cv.convertScaleAbs(grad_y), 0.5, 0)
            mask_np = (sobel > 12).astype(np.uint8)

        cols = math.ceil(img_w / cell_size)
        rows = math.ceil(img_h / cell_size)
        resized_mask = cv.resize(mask_np, (cols, rows), interpolation=cv.INTER_AREA)

        grid = (resized_mask > 0).astype(np.uint8)

        padded = np.pad(grid, pad_width=1, mode="constant", constant_values=0)
        cv.morphologyEx(
            padded, cv.MORPH_CLOSE, np.ones((3, 3), dtype=np.uint8), dst=padded
        )
        grid = padded[1:-1, 1:-1]

        cw = math.ceil(img_w / cols)
        ch = math.ceil(img_h / rows)

        return grid, cw, ch

    def _generate_tiles(
        self,
        grid: np.ndarray,
        cw: int,
        ch: int,
        img_w: int,
        img_h: int,
    ) -> list[tuple[int, int, int, int]]:
        """Decomposes the occupancy grid into padded macro-tiles."""
        tiles = []
        active_rows, active_cols = np.where(grid != 0)
        if len(active_rows) == 0:
            return []

        start_row, end_row = int(active_rows.min()), int(active_rows.max() + 1)
        start_col, end_col = int(active_cols.min()), int(active_cols.max() + 1)

        tile_w = max(1, MAX_TILE_SIZE // cw)
        tile_h = max(1, MAX_TILE_SIZE // ch)

        for r in range(start_row, end_row, tile_h):
            for c in range(start_col, end_col, tile_w):
                r_end = min(r + tile_h, end_row)
                c_end = min(c + tile_w, end_col)

                sub = grid[r:r_end, c:c_end]
                if not np.any(sub):
                    continue

                if np.all(sub):
                    raw_x1 = c * cw
                    raw_y1 = r * ch
                    raw_x2 = min(c_end * cw, img_w)
                    raw_y2 = min(r_end * ch, img_h)

                    x1 = max(0, raw_x1 - TILE_PADDING)
                    y1 = max(0, raw_y1 - TILE_PADDING)
                    x2 = min(img_w, raw_x2 + TILE_PADDING)
                    y2 = min(img_h, raw_y2 + TILE_PADDING)

                    tiles.append((x1, y1, x2, y2))
                else:
                    contours, _ = cv.findContours(
                        sub.astype(np.uint8), cv.RETR_EXTERNAL, cv.CHAIN_APPROX_SIMPLE
                    )
                    for cnt in contours:
                        bx, by, bw, bh = cv.boundingRect(cnt)

                        raw_x1 = (c + bx) * cw
                        raw_y1 = (r + by) * ch
                        raw_x2 = min(raw_x1 + (bw * cw), img_w)
                        raw_y2 = min(raw_y1 + (bh * ch), img_h)

                        x1 = max(0, raw_x1 - TILE_PADDING)
                        y1 = max(0, raw_y1 - TILE_PADDING)
                        x2 = min(img_w, raw_x2 + TILE_PADDING)
                        y2 = min(img_h, raw_y2 + TILE_PADDING)

                        if (x2 - x1) >= 32 and (y2 - y1) >= 32:
                            tiles.append((x1, y1, x2, y2))

        return tiles

    @staticmethod
    def _apply_nms(
        detections: list[dict[str, Any]],
        iou_thresh: float = 0.35,
        iomin_thresh: float = 0.65,
    ) -> list[dict[str, Any]]:
        """
        Applies Non-Maximum Suppression to the list of detections. Prioritizes larger bounding box areas to
        retain complete words/labels over partial fragment detections from overlapping tiles.
        """
        if not detections:
            return []

        boxes_xywh = []
        areas = []
        scores = []
        xyxy_list = []

        for d in detections:
            poly = np.array(d["poly"])
            x_min = int(np.min(poly[:, 0]))
            y_min = int(np.min(poly[:, 1]))
            x_max = int(np.max(poly[:, 0]))
            y_max = int(np.max(poly[:, 1]))
            w = max(1, x_max - x_min)
            h = max(1, y_max - y_min)
            area = w * h

            boxes_xywh.append([x_min, y_min, w, h])
            xyxy_list.append((x_min, y_min, x_max, y_max))
            areas.append(area)
            scores.append(float(d.get("score", 0.0)))

        # Sort score weighting: prioritize area, break ties with confidence score
        nms_scores = [
            float(areas[i] * (1.0 + scores[i])) for i in range(len(detections))
        ]

        indices = cv.dnn.NMSBoxes(
            bboxes=boxes_xywh,
            scores=nms_scores,
            score_threshold=0.0,
            nms_threshold=iou_thresh,
        )

        if len(indices) == 0:
            return []

        kept_indices = np.array(indices).flatten().tolist()

        # Containment filter: If a smaller box is largely inside an already-kept larger box
        kept_indices.sort(key=lambda idx: areas[idx], reverse=True)
        final_kept = []
        kept_boxes = []

        for idx in kept_indices:
            x1, y1, x2, y2 = xyxy_list[idx]
            area = areas[idx]
            suppressed = False

            for kx1, ky1, kx2, ky2, karea in kept_boxes:
                ix1 = max(x1, kx1)
                iy1 = max(y1, ky1)
                ix2 = min(x2, kx2)
                iy2 = min(y2, ky2)

                if ix2 > ix1 and iy2 > iy1:
                    inter = (ix2 - ix1) * (iy2 - iy1)
                    if inter / min(area, karea) > iomin_thresh:
                        suppressed = True
                        break

            if not suppressed:
                final_kept.append(idx)
                kept_boxes.append((x1, y1, x2, y2, area))

        return [detections[i] for i in final_kept]

    def _convert_to_bgr(self, img: Union["pyvips.Image", np.ndarray]) -> np.ndarray:
        """
        Converts a pyvips image or ndarray to a BGR numpy array with proper alpha flattening.
        """
        if pyvips is not None and isinstance(img, pyvips.Image):
            if img.hasalpha():
                img = img.flatten(background=[255, 255, 255])

            if img.bands == 1:
                img = img.colourspace("srgb")
            elif img.bands > 3:
                img = img.extract_band(0, n=3)

            img_np = img.numpy()
            if img_np.ndim == 3 and img_np.shape[2] == 3:
                img_bgr = cv.cvtColor(img_np, cv.COLOR_RGB2BGR)
            else:
                img_bgr = cv.cvtColor(img_np, cv.COLOR_GRAY2BGR)

            return np.ascontiguousarray(img_bgr)
        elif isinstance(img, np.ndarray):
            if img.ndim == 2:
                return cv.cvtColor(img, cv.COLOR_GRAY2BGR)
            elif img.ndim == 3 and img.shape[2] == 4:
                return cv.cvtColor(img, cv.COLOR_BGRA2BGR)
            return img
        return np.array(img)

    def _clean_detection(self, text: str) -> str:
        """
        Cleans detected text by removing special characters, stripping spaces and normalizing the text.
        """
        return clean_text(text)

    def _is_touching_tile_edge(
        self, poly: np.ndarray, tile: tuple[int, int, int, int]
    ) -> bool:
        """Checks if a polygon is touching/near the edge of a tile."""
        tile_x1, tile_y1, tile_x2, tile_y2 = tile
        tile_w = tile_x2 - tile_x1
        tile_h = tile_y2 - tile_y1

        poly_min_x, poly_min_y = np.min(poly, axis=0)
        poly_max_x, poly_max_y = np.max(poly, axis=0)

        return (
            poly_min_x <= POLY_EDGE_THRESHOLD
            or poly_min_y <= POLY_EDGE_THRESHOLD
            or poly_max_x >= tile_w - POLY_EDGE_THRESHOLD
            or poly_max_y >= tile_h - POLY_EDGE_THRESHOLD
        )

    def _merge_polys(
        self, polys: list[tuple[int, int, int, int]], img_w: int, img_h: int
    ) -> list[tuple[int, int, int, int]]:
        """Merges overlapping or adjacent polygons into larger bounding boxes."""
        if not polys:
            return []

        # Pad boxes to ensure adjacent boxes overlap
        padded_polys = []
        for x1, y1, x2, y2 in polys:
            padded_polys.append(
                [
                    max(0, int(x1) - POLY_PADDING),
                    max(0, int(y1) - POLY_PADDING),
                    min(img_w, int(x2) + POLY_PADDING),
                    min(img_h, int(y2) + POLY_PADDING),
                ]
            )

        n = len(padded_polys)
        adj = [[] for _ in range(n)]
        for i in range(n):
            for j in range(i + 1, n):
                a = padded_polys[i]
                b = padded_polys[j]
                if a[0] < b[2] and a[2] > b[0] and a[1] < b[3] and a[3] > b[1]:
                    adj[i].append(j)
                    adj[j].append(i)

        visited = [False] * n
        merged_polys = []

        for i in range(n):
            if visited[i]:
                continue

            cluster = []
            stack = [i]
            visited[i] = True

            while stack:
                curr = stack.pop()
                cluster.append(padded_polys[curr])
                for neighbor in adj[curr]:
                    if not visited[neighbor]:
                        visited[neighbor] = True
                        stack.append(neighbor)

            if cluster:
                x1 = min(c[0] for c in cluster)
                y1 = min(c[1] for c in cluster)
                x2 = max(c[2] for c in cluster)
                y2 = max(c[3] for c in cluster)
                merged_polys.append((x1, y1, x2, y2))

        return merged_polys

    def _run_ocr(
        self,
        img: Union["pyvips.Image", np.ndarray],
        tiles: list[tuple[int, int, int, int]],
    ) -> list[dict[str, Any]]:
        """Runs OCR on all tiles, re-OCRs merged edge-crossing regions, and applies area-weighted NMS."""
        raw_detections = []
        global_edge_polys = []

        is_vips = pyvips is not None and isinstance(img, pyvips.Image)
        img_w = img.width if is_vips else img.shape[1]
        img_h = img.height if is_vips else img.shape[0]

        for x1, y1, x2, y2 in tiles:
            tw = x2 - x1
            th = y2 - y1
            if tw <= 0 or th <= 0:
                continue

            if is_vips:
                tile_vips = img.crop(x1, y1, tw, th)
                tile = self._convert_to_bgr(tile_vips)
            else:
                tile = img[y1:y2, x1:x2]

            result = self.ocr_engine(tile)

            if result and result.boxes is not None and len(result.boxes) > 0:
                for box, text, score in zip(result.boxes, result.txts, result.scores):
                    cleaned_text = self._clean_detection(text)
                    if not cleaned_text:
                        continue

                    poly = np.array(box, dtype=np.int32)
                    is_edge_poly = self._is_touching_tile_edge(poly, (x1, y1, x2, y2))

                    poly[:, 0] += x1
                    poly[:, 1] += y1

                    if is_edge_poly:
                        gx1 = int(np.min(poly[:, 0]))
                        gy1 = int(np.min(poly[:, 1]))
                        gx2 = int(np.max(poly[:, 0]))
                        gy2 = int(np.max(poly[:, 1]))
                        global_edge_polys.append((gx1, gy1, gx2, gy2))

                    font_size = int(
                        max(
                            np.linalg.norm(poly[3] - poly[0]),
                            np.linalg.norm(poly[2] - poly[1]),
                        )
                    )

                    raw_detections.append(
                        {
                            "box": [
                                int(np.min(poly[:, 0])),
                                int(np.min(poly[:, 1])),
                                int(np.max(poly[:, 0])),
                                int(np.max(poly[:, 1])),
                            ],
                            "poly": poly,
                            "text": cleaned_text,
                            "score": float(score),
                            "font_size": font_size,
                        }
                    )

        # Merge edge-touching polygons and re-run OCR on merged bounding boxes
        if global_edge_polys:
            merged_polys = self._merge_polys(global_edge_polys, img_w, img_h)

            for x1, y1, x2, y2 in merged_polys:
                pw = x2 - x1
                ph = y2 - y1
                if pw <= 0 or ph <= 0:
                    continue

                if is_vips:
                    crop_vips = img.crop(x1, y1, pw, ph)
                    crop_bgr = self._convert_to_bgr(crop_vips)
                else:
                    crop_bgr = img[y1:y2, x1:x2]

                result = self.ocr_engine(crop_bgr)
                if result and result.boxes is not None and len(result.boxes) > 0:
                    for box, text, score in zip(
                        result.boxes, result.txts, result.scores
                    ):
                        cleaned_text = self._clean_detection(text)
                        if not cleaned_text:
                            continue

                        poly = np.array(box, dtype=np.int32)
                        poly[:, 0] += x1
                        poly[:, 1] += y1

                        font_size = int(
                            max(
                                np.linalg.norm(poly[3] - poly[0]),
                                np.linalg.norm(poly[2] - poly[1]),
                            )
                        )

                        raw_detections.append(
                            {
                                "box": [
                                    int(np.min(poly[:, 0])),
                                    int(np.min(poly[:, 1])),
                                    int(np.max(poly[:, 0])),
                                    int(np.max(poly[:, 1])),
                                ],
                                "poly": poly,
                                "text": cleaned_text,
                                "score": float(score),
                                "font_size": font_size,
                            }
                        )

        final_detections = self._apply_nms(raw_detections)
        return final_detections

    def detect(
        self,
        image: Union[str, Path, bytes, memoryview, "pyvips.Image", np.ndarray],
        output: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """
        Main detection pipeline: loads the image, generates the occupancy grid, tiles the image,
        runs OCR on each tile, merges edge-touching detections, and applies NMS to produce final results.
        """
        if pyvips is not None and isinstance(image, pyvips.Image):
            img = image
            img_w, img_h = img.width, img.height
        elif isinstance(image, (bytes, memoryview)):
            if pyvips is not None:
                img = pyvips.Image.new_from_buffer(image, "", access="random")
                img_w, img_h = img.width, img.height
            else:
                nparr = np.frombuffer(image, np.uint8)
                img = cv.imdecode(nparr, cv.IMREAD_COLOR)
                img_h, img_w = img.shape[:2]
        elif isinstance(image, np.ndarray):
            img = image
            img_h, img_w = img.shape[:2]
        else:
            img_path = Path(image)
            if not img_path.exists():
                raise FileNotFoundError(f"Image not found: {img_path}")
            if pyvips is not None:
                img = pyvips.Image.new_from_file(img_path, access="random")
                img_w, img_h = img.width, img.height
            else:
                img = cv.imread(str(img_path))
                img_h, img_w = img.shape[:2]

        grid, cw, ch = self._generate_grid(img)
        tiles = self._generate_tiles(grid, cw, ch, img_w, img_h)
        detections = self._run_ocr(img, tiles)

        if output:
            self.visualize(img, detections, output)

        return detections

    @staticmethod
    def visualize(
        img: Union["pyvips.Image", np.ndarray],
        detections: list[dict[str, Any]],
        output: str,
        max_preview_dim: int = 3000,
    ) -> None:
        """Draws detected OCR bounding boxes and text labels on the output preview."""
        if pyvips is not None and isinstance(img, pyvips.Image):
            w, h = img.width, img.height

            if img.hasalpha():
                img = img.flatten(background=[255, 255, 255])

            img_np = img.numpy()
            if img_np.ndim == 3 and img_np.shape[2] == 3:
                preview_img = cv.cvtColor(img_np, cv.COLOR_RGB2BGR)
            else:
                preview_img = cv.cvtColor(img_np, cv.COLOR_GRAY2BGR)
        else:
            h, w = img.shape[:2]
            preview_img = img.copy()

        scale = (
            min(max_preview_dim / w, max_preview_dim / h)
            if max(w, h) > max_preview_dim
            else 1.0
        )

        if scale < 1.0:
            preview = cv.resize(
                preview_img,
                (int(w * scale), int(h * scale)),
                interpolation=cv.INTER_AREA,
            )
        else:
            preview = preview_img.copy()

        # Create overlay array for translucent blending
        overlay = preview.copy()

        # Font configuration
        font = cv.FONT_HERSHEY_SIMPLEX
        font_scale = 1.4 * scale
        thickness = max(1, int(1 * scale))

        for det in detections:
            poly = det.get("poly")
            text = det.get("text", "")

            if poly is None or len(poly) == 0:
                continue

            # Scale polygon coordinates to match preview size
            pts = (np.array(poly, dtype=np.float32) * scale).astype(np.int32)
            pts = pts.reshape((-1, 1, 2))

            color = (
                random.randint(50, 255),
                random.randint(50, 255),
                random.randint(50, 255),
            )

            # Bounding boxes
            cv.fillPoly(overlay, [pts], color)
            cv.polylines(
                preview, [pts], isClosed=True, color=color, thickness=thickness
            )

            if text:
                x, y = pts[0][0]

                (text_w, text_h), baseline = cv.getTextSize(
                    text, font, font_scale, thickness
                )

                text_bg_top = max(0, y - text_h - baseline - 4)
                text_bg_bottom = y
                text_bg_right = min(preview.shape[1], x + text_w + 6)

                cv.rectangle(
                    overlay,
                    (x, text_bg_top),
                    (text_bg_right, text_bg_bottom),
                    color,
                    -1,
                )
                text_y = (
                    y - baseline - 2
                    if (y - text_h - baseline - 4) >= 0
                    else y + text_h + 2
                )
                cv.putText(
                    preview,
                    text,
                    (x + 3, text_y),
                    font,
                    font_scale,
                    (0, 0, 255),
                    thickness,
                    cv.LINE_AA,
                )

        # Blend overlay (0.4 opacity) with base image (0.6 opacity)
        alpha = 0.4
        cv.addWeighted(overlay, alpha, preview, 1 - alpha, 0, preview)

        output_path = Path(output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        cv.imwrite(str(output_path), preview)

    def remove_txt(
        self,
        img: Union["pyvips.Image", np.ndarray],
        detections: list[dict[str, Any]],
        output: Optional[str] = None,
        min_wall_length: int = 80,
    ) -> np.ndarray:
        """Removes text using the existing remove_txt algorithm."""
        if pyvips is not None and isinstance(img, pyvips.Image):
            bgr = self._convert_to_bgr(img)
        else:
            bgr = img
        return remove_txt(bgr, detections, output=output, min_wall_length=min_wall_length)


def recognize_txt(
    image_path: Union[str, Path, bytes, memoryview, "pyvips.Image", np.ndarray],
    ocr: Optional[Union[RapidOCR, FloorMapOCR]] = None,
    engine: Optional[str] = None,
) -> tuple[np.ndarray, list[dict[str, Any]]]:
    """Pre-processes image and runs FloorMapOCR text recognition pipeline."""
    if isinstance(ocr, FloorMapOCR):
        floor_ocr = ocr
    elif isinstance(ocr, RapidOCR):
        floor_ocr = FloorMapOCR(ocr_engine=ocr)
    else:
        floor_ocr = FloorMapOCR()

    if pyvips is not None and isinstance(image_path, pyvips.Image):
        vips_img = image_path
        bgr_img = floor_ocr._convert_to_bgr(vips_img)
        img_w, img_h = vips_img.width, vips_img.height
        target_img = vips_img
    elif isinstance(image_path, (bytes, memoryview)):
        if pyvips is not None:
            vips_img = pyvips.Image.new_from_buffer(image_path, "", access="random")
            bgr_img = floor_ocr._convert_to_bgr(vips_img)
            img_w, img_h = vips_img.width, vips_img.height
            target_img = vips_img
        else:
            nparr = np.frombuffer(image_path, np.uint8)
            bgr_img = cv.imdecode(nparr, cv.IMREAD_COLOR)
            img_h, img_w = bgr_img.shape[:2]
            target_img = bgr_img
    elif isinstance(image_path, np.ndarray):
        bgr_img = image_path
        img_h, img_w = bgr_img.shape[:2]
        target_img = bgr_img
    else:
        path = Path(image_path)
        if not path.exists():
            raise FileNotFoundError(f"Image not found: {path}")
        if pyvips is not None:
            vips_img = pyvips.Image.new_from_file(path, access="random")
            bgr_img = floor_ocr._convert_to_bgr(vips_img)
            img_w, img_h = vips_img.width, vips_img.height
            target_img = vips_img
        else:
            bgr_img = cv.imread(str(path))
            img_h, img_w = bgr_img.shape[:2]
            target_img = bgr_img

    grid, cw, ch = floor_ocr._generate_grid(target_img)
    tiles = floor_ocr._generate_tiles(grid, cw, ch, img_w, img_h)
    detections = floor_ocr._run_ocr(target_img, tiles)

    return bgr_img, detections


def remove_txt(img, detections, output=None, min_wall_length=80):
    """Removes text from the image, using localized inpainting and structural wall restoration."""
    h, w = img.shape[:2]
    gray = cv.cvtColor(img, cv.COLOR_BGR2GRAY)
    
    if len(img.shape) == 3 and img.shape[2] == 3:
        hsv = cv.cvtColor(img, cv.COLOR_BGR2HSV)
        _, s, v = cv.split(hsv)
        gray[(s > 8) & (v > 100)] = 255
        
    _, binary = cv.threshold(gray, 200, 255, cv.THRESH_BINARY_INV)
    h_kernel = cv.getStructuringElement(cv.MORPH_RECT, (min_wall_length, 1))
    v_kernel = cv.getStructuringElement(cv.MORPH_RECT, (1, min_wall_length))
    structural_map = cv.dilate(cv.add(cv.morphologyEx(binary, cv.MORPH_OPEN, h_kernel), 
                                      cv.morphologyEx(binary, cv.MORPH_OPEN, v_kernel)), 
                               np.ones((2, 2), np.uint8), iterations=1)
    del binary
    
    for det in detections:
        poly = np.array(det["poly"], dtype=np.int32)
        x, y, pw, ph = cv.boundingRect(poly)
        
        pad = 5
        x_min, y_min = max(0, x - pad), max(0, y - pad)
        x_max, y_max = min(w, x + pw + pad), min(h, y + ph + pad)
        cw, ch = x_max - x_min, y_max - y_min
        
        if cw <= 0 or ch <= 0:
            continue

        roi_mask = np.zeros((ch, cw), dtype=np.uint8)
        cv.fillPoly(roi_mask, [poly - [x_min, y_min]], 255)
        wall_pixels = cv.bitwise_and(roi_mask, structural_map[y_min:y_max, x_min:x_max])
        
        _, ink_mask = cv.threshold(gray[y_min:y_max, x_min:x_max], 220, 255, cv.THRESH_BINARY_INV)
        ink_to_inpaint = cv.bitwise_and(cv.dilate(ink_mask, np.ones((3, 3), np.uint8), iterations=1), roi_mask)
        
        crop = img[y_min:y_max, x_min:x_max]
        if ink_to_inpaint.any():
            crop = cv.inpaint(crop, ink_to_inpaint, inpaintRadius=5, flags=cv.INPAINT_TELEA)
            
        if wall_pixels.any():
            original_crop = img[y_min:y_max, x_min:x_max]
            crop[wall_pixels > 0] = original_crop[wall_pixels > 0]
            
        img[y_min:y_max, x_min:x_max] = crop
            
    del structural_map, gray 
    if output:
        cv.imwrite(output, img)
    return img


def resize_img_det(img, detections, max_side=RESIZE_LIMIT):
    """Resizes the image and detection coordinates proportionally."""
    h, w = img.shape[:2]
    largest = max(h, w)
    
    if largest <= max_side:
        return img, detections
        
    scale = max_side / float(largest)
    new_w, new_h = int(w * scale), int(h * scale)
    resized_img = cv.resize(img, (new_w, new_h), interpolation=cv.INTER_AREA)
    
    scaled_detections = []
    for d in detections:
        scaled_detections.append({
            "box": [int(v * scale) for v in d.get("box", [0, 0, 0, 0])],
            "poly": [[pt[0] * scale, pt[1] * scale] for pt in d["poly"]],
            "text": d["text"],
            "score": d.get("score", 0.0),
            "font_size": int(d.get("font_size", 25) * scale)
        })
    return resized_img, scaled_detections


def inject_elements(
    svg,
    raw_svg_content,
    detections,
    floor_id,
    floor_name=None,
    floor_locations=None,
    paths=None,
    injector=None,
    geo_data=None,
    scale_x=1.0,
    scale_y=1.0,
    orig_w=2000,
    orig_h=2000,
    engine=None,
):
    """
    Injects text elements into an SVG file based on detected text regions, with scale factors matching py-png2svg-ms.
    """
    engine = (engine or os.getenv("CONVERSION_ENGINE", "potrace")).lower()
    ET.register_namespace('', "http://www.w3.org/2000/svg")
    ET.register_namespace('sclera', 'https://sclera.app/ns')
    root = ET.fromstring(svg)
    
    # inject floor metadata
    if injector:
        injector.write_into_svg_tag(root, floor_id, floor_name, geo_data, orig_w, orig_h)
        injector.write_location_path_tags(root, floor_locations)
        injector.write_path_path_tags(root, paths)
        injector.inject_sclera_data(root, raw_svg_content, floor_locations, paths, geo_data)
    
    # inject text elements for detected text regions
    if detections:
        font_size = (
            np.median([det.get("font_size", 25) for det in detections]) if detections else 25
        )
        scaled_font_size = (font_size * (scale_x + scale_y) / 2.0) - 5
        
        for det in detections:
            poly = np.array(det["poly"], dtype=np.float64)
            cx, cy = np.mean(poly, axis=0)
            
            # Calculate reading direction angle along the baseline (poly[0] -> poly[1])
            dx = poly[1][0] - poly[0][0]
            dy = poly[1][1] - poly[0][1]
            angle_deg = np.degrees(np.arctan2(dy, dx))
        
            cx = cx * scale_x
            cy = cy * scale_y

            attrs = {
                "x": f"{cx:.1f}", 
                "y": f"{cy:.1f}",
                "font-size": f"{scaled_font_size}px", 
                "fill": "black",
                "text-anchor": "middle",
                "dominant-baseline": "central",
                "font-family": "Arial, Helvetica, sans-serif",
            }
            
            if abs(angle_deg) > 10.0:
                attrs["transform"] = f"rotate({angle_deg:.2f}, {cx:.1f}, {cy:.1f})"
            
            text_el = ET.SubElement(root, "text", attrs)
            text_el.text = det["text"]
            
    return ET.ElementTree(root)


def main():
    parser = argparse.ArgumentParser(
        description="Floor Plan OCR Detection and Text Removal Tool"
    )
    
    parser.add_argument(
        "--image",
        "-i",
        type=str,
        nargs="?",
        default="./assets/1.png",
        help="Path to the input image file.",
    )
    parser.add_argument(
        "--vis",
        "-v",
        type=str,
        default=None,
        help="Output Path to save the visualization.",
    )

    args = parser.parse_args()

    ocr = FloorMapOCR()
    _ = ocr.detect(args.image, args.vis)


if __name__ == "__main__":
    main()

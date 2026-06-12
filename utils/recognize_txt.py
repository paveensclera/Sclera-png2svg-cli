import os
import re
import time
import logging

# ----------- Environment Configuration -----------
CPU_THREADS = 4
if CPU_THREADS:
    os.environ["OMP_NUM_THREADS"] = str(CPU_THREADS)
    os.environ["MKL_NUM_THREADS"] = str(CPU_THREADS)
    os.environ["OPENBLAS_NUM_THREADS"] = str(CPU_THREADS)
    os.environ["VECLIB_MAXIMUM_THREADS"] = str(CPU_THREADS)
    os.environ["NUMEXPR_NUM_THREADS"] = str(CPU_THREADS)

import cv2 as cv
import numpy as np
from rapidocr import RapidOCR, OCRVersion, EngineType, ModelType, LangDet, LangCls, LangRec
from rapidocr.utils.log import logger as rapidocr_logger

# Disable RapidOCR verbose logging
rapidocr_logger.setLevel(logging.ERROR)
for handler in rapidocr_logger.handlers:
    handler.setLevel(logging.ERROR)
    
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ----------- OCR & Processing Parameters -----------
TILE_SIZE = 2000          # Default tile window size in pixels
OVERLAP = 300             # Overlap between tiles in pixels
INK_THRESHOLD = 0.005     # Minimum ratio of ink pixels in a tile to run OCR
SCORE_THRESHOLD = 0.7     # Minimum confidence score for a valid detection
IOU_THRESHOLD = 0.3       # Threshold for allowing overlap between two bounding boxes
RESIZE_LIMIT = 4096       # Resize limit for the final SVG output
THIN_LINES_THICKNESS = 4  # Thickness for removing thin lines during pre-processing
DESPECKLE_AREA = 15       # Minimum area for removing small noise speckles
CLUSTER_MAX_SIZE = 2000   # Maximum size for clustered crops to the OCR engine
CLUSTER_MARGIN = 300      # Margin for clustering nearby text regions
CROP_PADDING = 50         # Additional padding around crop regions
# USE_CUDA = False        # GPU toggle (requires onnxruntime-gpu)


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


def clean_text(text):
    """Cleans recognized text by removing special characters and trimming whitespace."""
    text = "".join(c for c in text if (32 <= ord(c) <= 126) or (ord(c) == 176)).strip()
    text = text.replace("$", "S").replace("|", "I").replace("=", "")
    text = re.sub(r'^[\.,:;\-]+', '', text)
    text = re.sub(r'[\.,:;\-]+$', '', text)
    return text.strip()


def filter_detections(detections):
    """Filters out detections below the confidence score threshold or duplicates."""
    detections = sorted(detections, key=lambda x: x["score"], reverse=True)
    n = len(detections)
    keep = [True] * n
    texts = [d["text"].lower() for d in detections]
    lengths = [len(t) for t in texts]
    
    for i in range(n):
        if not keep[i]:
            continue
            
        box1 = detections[i]["box"]
        t1, len1, score1 = texts[i], lengths[i], detections[i]["score"]
        
        for j in range(i + 1, n):
            if not keep[j]:
                continue
                
            box2 = detections[j]["box"]
            t2, len2, score2 = texts[j], lengths[j], detections[j]["score"]
            
            iou = calculate_iou(box1, box2)
            has_overlap = iou > IOU_THRESHOLD
            is_substring = (t1 in t2 or t2 in t1)
            
            # If texts are substrings and overlap, keep the longer one
            if is_substring and iou > 0:
                if len1 >= len2:
                    keep[j] = False
                else:
                    keep[i] = False
                    break
            # If not substrings but significant overlap, keep higher score
            elif has_overlap:
                if score1 >= score2:
                    keep[j] = False
                else:
                    keep[i] = False
                    break

    return [d for i, d in enumerate(detections) if keep[i]]


def load_image(path):
    """Loads image from path and returns it with width and height."""
    img = cv.imread(path)
    if img is None:
        raise ValueError(f"Could not read image: {path}")
    h, w = img.shape[:2]
    return img, w, h


def binarize_image(img):
    """Converts image to binary. Colored darker backgrounds are ignored."""
    gray = cv.cvtColor(img, cv.COLOR_BGR2GRAY)
    if len(img.shape) == 3 and img.shape[2] == 3:
        hsv = cv.cvtColor(img, cv.COLOR_BGR2HSV)
        _, s, v = cv.split(hsv)
        gray[(s > 8) & (v > 100)] = 255
        
    _, binary = cv.threshold(gray, 127, 255, cv.THRESH_BINARY_INV) 
    return binary


def remove_thin_lines(img, thickness=THIN_LINES_THICKNESS):
    """Removes thin lines from floormaps to improve text detections."""
    kernel = np.ones((thickness, thickness), np.uint8)
    return cv.morphologyEx(img, cv.MORPH_OPEN, kernel)


def remove_noise(img, min_area=DESPECKLE_AREA):
    """Removes small speckles of noise left out from binarization."""
    num_labels, labels, stats, _ = cv.connectedComponentsWithStats(img, connectivity=8)
    areas = stats[:, cv.CC_STAT_AREA]
    
    label_map = np.zeros(num_labels, dtype=np.uint8)
    label_map[areas >= min_area] = 255
    label_map[0] = 0 
    return label_map[labels]


def estimate_optimal_tile_params(binary):
    """Performs a fast pre-scan on a downscaled binary to estimate median text height."""
    h, w = binary.shape
    scale = 8
    h_small, w_small = h // scale, w // scale
    
    if h_small > 0 and w_small > 0:
        small_binary = cv.resize(binary, (w_small, h_small), interpolation=cv.INTER_NEAREST)            
        dilated = cv.dilate(small_binary, np.ones((3, 3), np.uint8), iterations=1)
        
        _, _, stats, _ = cv.connectedComponentsWithStats(dilated, connectivity=8)
        
        widths = stats[1:, cv.CC_STAT_WIDTH] * scale
        heights = stats[1:, cv.CC_STAT_HEIGHT] * scale
        
        valid_mask = (heights > 0)
        widths, heights = widths[valid_mask], heights[valid_mask]
        aspect = widths / heights
        
        valid_text_mask = (
            (heights >= 8) & (heights <= 120) &
            (widths >= 8) & (widths <= 2000) &
            (aspect >= 0.2) & (aspect <= 100.0)
        )
        
        valid_heights = heights[valid_text_mask]
        
        if len(valid_heights) > 0:
            median_height = np.median(valid_heights)
            
            if median_height > 70:
                tile_size = 3000
            elif median_height > 50:
                tile_size = 2500
            else:
                tile_size = 2000
            
            overlap = int(median_height * 6)
            overlap = max(150, min(overlap, 500))
            
            return tile_size, overlap
            
    logger.warning("Could not estimate text scale. Falling back to tile_size=3000, overlap=300")
    return 3000, 300


def recognize_txt(image_path, output_thin=None):
    """Pre-processes image and runs RapidOCR in overlapping grids."""
    img, w, h = load_image(image_path)
    process_img = remove_noise(cv.erode(remove_thin_lines(binarize_image(img), thickness=2), np.ones((2, 2), np.uint8), iterations=1))
    
    tile_size, overlap = estimate_optimal_tile_params(process_img)
    process_img = cv.cvtColor(cv.bitwise_not(process_img), cv.COLOR_GRAY2BGR)
    if output_thin:
        cv.imwrite(output_thin, process_img)

    side_len = max(w, h)
    ocr = RapidOCR(params={
        "Global.text_score": SCORE_THRESHOLD,
        "Global.max_side_len": side_len,
        "Det.ocr_version": OCRVersion.PPOCRV5,
        "Det.engine_type": EngineType.ONNXRUNTIME,
        "Det.model_type": ModelType.MOBILE,
        "Det.lang_type": LangDet.CH,
        "Det.limit_side_len": side_len,
        "Det.limit_type": "max",
        "Det.box_thresh": 0.3,
        "Det.unclip_ratio": 1.8,
        "Cls.ocr_version": OCRVersion.PPOCRV5,
        "Cls.engine_type": EngineType.ONNXRUNTIME,
        "Cls.model_type": ModelType.MOBILE,
        "Cls.lang_type": LangCls.CH,
        "Rec.ocr_version": OCRVersion.PPOCRV5,
        "Rec.engine_type": EngineType.ONNXRUNTIME,
        "Rec.model_type": ModelType.MOBILE,
        "Rec.lang_type": LangRec.EN,
        "EngineConfig.onnxruntime.enable_cpu_mem_arena": False,
        "EngineConfig.onnxruntime.intra_op_num_threads": CPU_THREADS,
        "EngineConfig.onnxruntime.inter_op_num_threads": 1
    })
    
    detections = []
    tile_count = skipped_count = total_runs = 0
    start_time = time.time()

    y = 0
    while y < h:
        ymin, ymax = y, min(y + tile_size, h)
        x = 0
        while x < w:
            xmin, xmax = x, min(x + tile_size, w)
            total_runs += 1

            tile_bin_gray = cv.cvtColor(process_img[ymin:ymax, xmin:xmax], cv.COLOR_BGR2GRAY)
            total = tile_bin_gray.size
            ink_pixels = total - cv.countNonZero(tile_bin_gray)
            
            if total == 0 or (ink_pixels / float(total)) <= INK_THRESHOLD:
                x += tile_size - overlap
                skipped_count += 1
                continue

            res = ocr(process_img[ymin:ymax, xmin:xmax])
            tile_count += 1

            if res and res.boxes is not None and len(res.txts) > 0:
                for txt, score, box in zip(res.txts, res.scores, res.boxes):
                    text = clean_text(txt)
                    if not text or len(text) == 1:
                        continue
                        
                    poly = np.array(box)
                    global_poly = poly.copy()
                    global_poly[:, 0] += xmin
                    global_poly[:, 1] += ymin
                    
                    font_size = max(np.linalg.norm(global_poly[3] - global_poly[0]), 
                                    np.linalg.norm(global_poly[2] - global_poly[1]))
                    
                    detections.append({
                        "box": [int(np.min(global_poly[:, 0])), int(np.min(global_poly[:, 1])),
                                int(np.max(global_poly[:, 0])), int(np.max(global_poly[:, 1]))],
                        "poly": global_poly.tolist(),
                        "text": text,
                        "score": float(score),
                        "font_size": int(font_size)
                    })
            x += tile_size - overlap
        y += tile_size - overlap

    del process_img
    merged = filter_detections(detections)

    return img, merged
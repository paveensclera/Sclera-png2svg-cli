import os
CPU_THREADS = 4
if CPU_THREADS:
    os.environ["OMP_NUM_THREADS"] = str(CPU_THREADS)
    os.environ["MKL_NUM_THREADS"] = str(CPU_THREADS)
    os.environ["OPENBLAS_NUM_THREADS"] = str(CPU_THREADS)
    os.environ["VECLIB_MAXIMUM_THREADS"] = str(CPU_THREADS)
    os.environ["NUMEXPR_NUM_THREADS"] = str(CPU_THREADS)

import logging
import re
import cv2 as cv
import numpy as np
import time

from rapidocr import RapidOCR, OCRVersion, EngineType, ModelType, LangDet, LangCls, LangRec
from rapidocr.utils.log import logger as rapidocr_logger

rapidocr_logger.setLevel(logging.ERROR)
for handler in rapidocr_logger.handlers:
    handler.setLevel(logging.ERROR)
    
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


TILE_SIZE         = 2000   # Default tile window size in pixels
OVERLAP           = 300    # Overlap between tiles in pixels
INK_THRESHOLD     = 0.005   # Minimum ratio of ink pixels in a tile to consider it for OCR

# ----------- Parameters -----------

# minimum confidence score to consider a detection valid
SCORE_THRESHOLD = 0.7

# (intersection over union) threshold for allowing how much overlap between two boxes is allowed
IOU_THRESHOLD   = 0.3

# resize scale for the final svg output
RESIZE_LIMIT    = 4096

# defautlt thickness for removing thin lines from the floormaps
THIN_LINES_THICKNESS = 4

# minimum area for removing small speckles of noise
DESPECKLE_AREA = 15

# maximum size for the clustered crops to be passed to the OCR engine
CLUSTER_MAX_SIZE = 2000

# margin for clustering nearby text regions
CLUSTER_MARGIN   = 300

# additional padding added around the crop regions for better recognition
CROP_PADDING     = 50

# GPU toggle (requires onnxruntime-gpu and several cuda dependencies)
#USE_CUDA        = False

# ----------------------------------


def calculate_iou(box1, box2):
    x_left   = max(box1[0], box2[0])
    y_top    = max(box1[1], box2[1])
    x_right  = min(box1[2], box2[2])
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
    text = "".join(c for c in text if (32 <= ord(c) <= 126) or (ord(c) == 176)).strip()
    
    text = text.replace("$", "S")
    text = text.replace("|", "I")
    
    text = re.sub(r'^[\.,:;\-]+', '', text)
    text = re.sub(r'[\.,:;\-]+$', '', text)
    
    return text.strip()


def filter_detections(detections):
    detections = sorted(detections, key=lambda x: x["score"], reverse=True)
    
    n       = len(detections)
    keep    = [True] * n
    texts   = [d["text"].lower() for d in detections]
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
            
            if is_substring and iou > 0:
                if len1 >= len2:
                    keep[j] = False
                else:
                    keep[i] = False
                    break
                
            elif has_overlap:
                if score1 >= score2:
                    keep[j] = False
                else:
                    keep[i] = False
                    break

    return [d for i, d in enumerate(detections) if keep[i]]


def load_image(path):
    img = cv.imread(path)
    if img is None:
        raise ValueError(f"Could not read image: {path}")
    h, w = img.shape[:2]
    return img, w, h


def binarize_image(img):
    gray = cv.cvtColor(img, cv.COLOR_BGR2GRAY)
    if len(img.shape) == 3 and img.shape[2] == 3:
        hsv = cv.cvtColor(img, cv.COLOR_BGR2HSV)
        _, s, v = cv.split(hsv)
        is_colored_bg = (s > 8) & (v > 100)
        gray[is_colored_bg] = 255
        
    _, binary = cv.threshold(gray, 127, 255, cv.THRESH_BINARY_INV) 
    return binary


def remove_thin_lines(img, thickness=THIN_LINES_THICKNESS):
    kernel = np.ones((thickness, thickness), np.uint8)
    return cv.morphologyEx(img, cv.MORPH_OPEN, kernel)


def remove_noise(img, min_area=DESPECKLE_AREA):
    num_labels, labels, stats, _ = cv.connectedComponentsWithStats(img, connectivity=8)
    areas = stats[:, cv.CC_STAT_AREA]
    
    label_map = np.zeros(num_labels, dtype=np.uint8)
    label_map[areas >= min_area] = 255
    label_map[0] = 0 
    cleaned = label_map[labels]
    
    return cleaned


def estimate_optimal_tile_params(binary):
    h, w = binary.shape
    
    scale = 8
    h_small, w_small = h // scale, w // scale
    if h_small > 0 and w_small > 0:
        small_binary = cv.resize(binary, (w_small, h_small), interpolation=cv.INTER_NEAREST)            
        kernel = np.ones((3, 3), np.uint8)
        dilated = cv.dilate(small_binary, kernel, iterations=1)
        
        num_labels, _, stats, _ = cv.connectedComponentsWithStats(dilated, connectivity=8)
        
        widths = stats[1:, cv.CC_STAT_WIDTH] * scale
        heights = stats[1:, cv.CC_STAT_HEIGHT] * scale
        
        valid_mask = (heights > 0)
        widths = widths[valid_mask]
        heights = heights[valid_mask]
        
        aspect = widths / heights
        
        valid_text_mask = (
            (heights >= 8) & (heights <= 120) &
            (widths >= 8) & (widths <= 2000) &
            (aspect >= 0.2) & (aspect <= 100.0)
        )
        
        valid_heights = heights[valid_text_mask]
        
        if len(valid_heights) > 0:
            median_height = np.median(valid_heights)
            print(f"Detected median text height (pre-scan): {median_height:.1f}px")
            
            if median_height > 70:
                tile_size = 2000
                print(f"Large text detected. Choosing tile_size={tile_size}")
            elif median_height > 50:
                tile_size = 1500
                print(f"Medium text detected. Choosing tile_size={tile_size}")
            else:
                tile_size = 1000
                print(f"Small text detected. Choosing tile_size={tile_size}")
            
            return tile_size
            
    print(f"Could not estimate text scale. Falling back to tile_size=2000")
    return 2000

def recognize_txt(image_path, output_thin=None):
    """
    Load the image, pre-process to remove thin lines, then run RapidOCR in
    overlapping tiles with a rigid grid. Returns (original_img, merged_detections).
    """
    img, w, h = load_image(image_path)
    process_img = binarize_image(img)
    process_img = remove_thin_lines(process_img, thickness=2)
    process_img = cv.erode(process_img, np.ones((2, 2), np.uint8), iterations=1)
    process_img = remove_noise(process_img)
    
    tile_size = estimate_optimal_tile_params(process_img)
    
    process_img = cv.bitwise_not(process_img)
    process_img = cv.cvtColor(process_img, cv.COLOR_GRAY2BGR)
    
    if output_thin:
        cv.imwrite(output_thin, process_img)

    # Initialise RapidOCR (ONNX Runtime)
    print(f"Initialising RapidOCR engine (tile_size={tile_size})...")
    side_len = max(w, h)
    ocr = RapidOCR(params={
        "Global.text_score":   SCORE_THRESHOLD,
        "Global.max_side_len": side_len,
        
        "Det.ocr_version":    OCRVersion.PPOCRV5,
        "Det.engine_type":    EngineType.ONNXRUNTIME,
        "Det.model_type":     ModelType.MOBILE,
        "Det.lang_type":      LangDet.CH,
        "Det.limit_side_len": side_len,
        "Det.limit_type":     "max",
        "Det.box_thresh":     0.3,
        "Det.unclip_ratio":   1.8,
        
        "Cls.ocr_version": OCRVersion.PPOCRV5,
        "Cls.engine_type": EngineType.ONNXRUNTIME,
        "Cls.model_type":  ModelType.MOBILE,
        "Cls.lang_type":   LangCls.CH,
        
        "Rec.ocr_version": OCRVersion.PPOCRV5,
        "Rec.engine_type": EngineType.ONNXRUNTIME,
        "Rec.model_type":  ModelType.MOBILE,
        "Rec.lang_type":   LangRec.EN,
        
        "EngineConfig.onnxruntime.enable_cpu_mem_arena": False,
        "EngineConfig.onnxruntime.intra_op_num_threads": CPU_THREADS,
        "EngineConfig.onnxruntime.inter_op_num_threads": 1
    })
    
    
    detections  = []
    tile_count  = 0
    skipped_count = 0
    total_runs = 0

    print(f"Processing tiles ({tile_size}x{tile_size}, {OVERLAP}px overlap)")
    start_time = time.time()

    y = 0
    while y < h:
        ymin, ymax = y, min(y + tile_size, h)
        x = 0
        while x < w:
            xmin, xmax = x, min(x + tile_size, w)
            total_runs += 1

            tile_bin = process_img[ymin:ymax, xmin:xmax]
            tile_bin_gray = cv.cvtColor(tile_bin, cv.COLOR_BGR2GRAY)
            total = tile_bin_gray.shape[0] * tile_bin_gray.shape[1]
            
            ink_pixels = total - cv.countNonZero(tile_bin_gray)
            if total == 0 or (ink_pixels / float(total)) <= INK_THRESHOLD:
                x += tile_size - OVERLAP
                skipped_count += 1
                continue

            tile = process_img[ymin:ymax, xmin:xmax]
            res = ocr(tile)
            tile_count += 1

            if res and res.boxes is not None and len(res.txts) > 0:
                for i in range(len(res.txts)):
                    text  = clean_text(res.txts[i])
                    if not text:
                        continue
                    if len(text) == 1:
                        continue
                    
                    score = float(res.scores[i])
                    poly  = np.array(res.boxes[i])

                    # Shift back to global coordinates
                    global_poly = poly.copy()
                    global_poly[:, 0] += xmin
                    global_poly[:, 1] += ymin
                    
                    h1 = np.linalg.norm(global_poly[3] - global_poly[0])
                    h2 = np.linalg.norm(global_poly[2] - global_poly[1])
                    detected_font_size = max(h1, h2)
                    
                    detections.append({
                        "box":  [int(np.min(global_poly[:, 0])), int(np.min(global_poly[:, 1])),
                                 int(np.max(global_poly[:, 0])), int(np.max(global_poly[:, 1]))],
                        "poly": global_poly.tolist(),
                        "text": text,
                        "score": score,
                        "font_size": int(detected_font_size)
                    })

            x += tile_size - OVERLAP
            
        y += tile_size - OVERLAP

    print(f"Traversed {total_runs} tile positions. Ran OCR on {tile_count} tiles. Jumped past {skipped_count} empty positions.")
    
    print("Merging overlapping detections...")
    merged = filter_detections(detections)
    print(f"Detections after merge: {len(merged)}")
    print(f"Tiled OCR completed in {time.time() - start_time:.2f}s")

    return img, merged
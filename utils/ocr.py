import os
import re
import logging
import xml.etree.ElementTree as ET

# ----------- Environment Configuration -----------
system_threads = os.cpu_count() or 4
CPU_THREADS = max(1, (system_threads // 2) - 2)
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
RESIZE_LIMIT = 4000       # Resize limit for the final SVG output
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
            
            overlap = int(median_height * 8)
            overlap = max(300, min(overlap, 500))
            
            return tile_size, overlap
            
    logger.warning("Could not estimate text scale. Falling back to tile_size=3000, overlap=300")
    return 3000, 300


def init_ocr_engine():
    """Initializes the RapidOCR engine with specified parameters."""
    ocr = RapidOCR(params={
        "Global.text_score": SCORE_THRESHOLD,
        #"Global.max_side_len": 4096,
        "Det.ocr_version": OCRVersion.PPOCRV5,
        "Det.engine_type": EngineType.ONNXRUNTIME,
        "Det.model_type": ModelType.MOBILE,
        "Det.lang_type": LangDet.CH,
        #"Det.limit_side_len": 4096,
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
    return ocr

# def update_side_len(ocr, side_len):
#     ocr.update_params(
#         max_side_len=side_len,
#         limit_side_len=side_len
#     )

def recognize_txt(image_path, ocr):
    """Pre-processes image and runs RapidOCR in overlapping grids."""
    img, w, h = load_image(image_path)
    process_img = remove_noise(cv.erode(remove_thin_lines(binarize_image(img), thickness=2), np.ones((2, 2), np.uint8), iterations=1))
    
    tile_size, overlap = estimate_optimal_tile_params(process_img)
    process_img = cv.cvtColor(cv.bitwise_not(process_img), cv.COLOR_GRAY2BGR)

    #side_len = max(w, h)
    #update_side_len(ocr, side_len)
    
    detections = []
    tile_count = skipped_count = total_runs = 0

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
            "box": [int(v * scale) for v in d["box"]],
            "poly": [[pt[0] * scale, pt[1] * scale] for pt in d["poly"]],
            "text": d["text"],
            "score": d["score"],
            "font_size": int(d["font_size"] * scale)
        })
    return resized_img, scaled_detections


def inject_elements(svg, raw_svg_content, detections, floor_id, floor_name = None, floor_locations = None, paths = None, injector = None, geo_data = None):
    """
    Injects text elements into an SVG file based on detected text regions.
    """
    ET.register_namespace('', "http://www.w3.org/2000/svg")
    ET.register_namespace('sclera', 'https://sclera.app/ns')
    root = ET.fromstring(svg)
    
    # remove extra vtracer path
    for child in list(root):
        tag_name = child.tag.split('}')[-1] if '}' in child.tag else child.tag
        if tag_name == 'path' and 'd' in child.attrib:
            d = child.attrib['d']
            if d.startswith('M0,0') or d.startswith('M 0 0'):
                root.remove(child)
                break
    
    # inject floor metadata
    if injector:
        injector.write_into_svg_tag(root, floor_id, floor_name, geo_data)
        injector.write_location_path_tags(root, floor_locations)
        injector.write_path_path_tags(root, paths)
        injector.inject_sclera_data(root, raw_svg_content, floor_locations, paths, geo_data)
    
    # inject text elements for detected text regions
    if detections:
        font_size = (np.median([det["font_size"] for det in detections]) if detections else 25) - 5
        
        for det in detections:
            poly = np.array(det["poly"], dtype=np.float64)
            cx, cy = np.mean(poly, axis=0)
            
            avg_horiz = (np.linalg.norm(poly[1] - poly[0]) + np.linalg.norm(poly[3] - poly[2])) / 2.0
            avg_vert = (np.linalg.norm(poly[0] - poly[3]) + np.linalg.norm(poly[2] - poly[1])) / 2.0
            
            if avg_horiz >= avg_vert:
                dx, dy = poly[1][0] - poly[0][0], poly[1][1] - poly[0][1]
            else:
                dx, dy = poly[0][0] - poly[3][0], poly[0][1] - poly[3][1]
            
            angle_deg = np.degrees(np.arctan2(dy, dx))
        
            attrs = {
                "x": f"{cx:.1f}", 
                "y": f"{cy:.1f}",
                "font-size": f"{font_size}px", 
                "fill": "red",
                "text-anchor": "middle", "dominant-baseline": "central",
                "font-family": "Arial, Helvetica, sans-serif",
            }
            
            if abs(angle_deg) > 10.0:
                attrs["transform"] = f"rotate({angle_deg:.2f}, {cx:.1f}, {cy:.1f})"
            
            text_el = ET.SubElement(root, "text", attrs)
            text_el.text = det["text"]
            
    return ET.ElementTree(root)


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

from rapidocr import RapidOCR, OCRVersion, EngineType, ModelType, LangDet, LangCls, LangRec
from rapidocr.utils.log import logger as rapidocr_logger


rapidocr_logger.setLevel(logging.ERROR)
for handler in rapidocr_logger.handlers:
    handler.setLevel(logging.ERROR)
    
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger(__name__)


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
USE_CUDA        = False

# ----------------------------------


def calculate_iou(box1, box2):
    """
    Calculates the overlap between 2 bounding boxes of the text, required for filtering out duplicate detections.
    """
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
    """
    Cleans the recognized text by removing special characters and trimming whitespace.
    """
    text = "".join(c for c in text if (32 <= ord(c) <= 126) or (ord(c) == 176)).strip()
    
    text = text.replace("$", "S")
    text = text.replace("|", "I")
    
    text = re.sub(r'^[\.,:;\-]+', '', text)
    text = re.sub(r'[\.,:;\-]+$', '', text)
    
    return text.strip()


def filter_detections(detections):
    """
    Filters out detections that are below the confidence score threshold or are duplicates.
    """
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
            
            # if the texts are substrings of each other and they have some overlap, the shorter one is not kept
            if is_substring and iou > 0:
                if len1 >= len2:
                    keep[j] = False
                else:
                    keep[i] = False
                    break
                
            # if the texts are not substrings but they have significant overlap, the one with the lower score is not kept
            elif has_overlap:
                if score1 >= score2:
                    keep[j] = False
                else:
                    keep[i] = False
                    break

    return [d for i, d in enumerate(detections) if keep[i]]
         

def load_image(path):
    """
    Loads the image from the given path and returns it along with its width and height.
    """
    img = cv.imread(path)
    if img is None:
        raise ValueError(f"Could not read image: {path}")
    h, w = img.shape[:2]
    return img, w, h


def binarize_image(img):
    """
    Converts the image to a binary image to improve detections.
    The coloured darker background is detected and set to white to get a better reading of the text. 
    """
    gray = cv.cvtColor(img, cv.COLOR_BGR2GRAY)
    if len(img.shape) == 3 and img.shape[2] == 3:
        hsv = cv.cvtColor(img, cv.COLOR_BGR2HSV)
        _, s, v = cv.split(hsv)
        is_colored_bg = (s > 8) & (v > 100)
        gray[is_colored_bg] = 255
        
    # results vary and using THRESH_OTSU gives better results in some floormaps
    # this could be tweaked for better detections
    _, binary = cv.threshold(gray, 127, 255, cv.THRESH_BINARY_INV) 
    return binary


def remove_thin_lines(img, thickness=THIN_LINES_THICKNESS):
    """
    Removes thin lines from the floormaps to improve detections.
    Higher thickness values will remove the thin lines better, but may also remove parts of the text.
    """
    kernel = np.ones((thickness, thickness), np.uint8)
    return cv.morphologyEx(img, cv.MORPH_OPEN, kernel)


def remove_noise(img, min_area=DESPECKLE_AREA):
    """
    Remove small speckles of noise left out from binarization and line removal.
    """
    num_labels, labels, stats, _ = cv.connectedComponentsWithStats(img, connectivity=8)
    areas = stats[:, cv.CC_STAT_AREA]
    
    label_map = np.zeros(num_labels, dtype=np.uint8)
    label_map[areas >= min_area] = 255
    label_map[0] = 0 
    cleaned = label_map[labels]
    
    return cleaned


def toggle_ocr_recognition(ocr, det=True, cls=True, rec=True):
    """
    Toggles the detection, classification, and recognition components of the OCR engine.
    """
    ocr.update_params(
        use_rec=rec, 
        use_cls=cls, 
        use_det=det
    )


def cluster_regions(boxes, max_size=CLUSTER_MAX_SIZE, margin=CLUSTER_MARGIN):
    """
    Clusters nearby text regions into larger crops to be passed to the OCR engine for better recognition.
    """
    if len(boxes) == 0:
        return []
        
    boxes = np.array(boxes)
    
    # precompute the axis-aligned bounding boxes for the text regions
    x_mins = np.min(boxes[:, :, 0], axis=1)
    y_mins = np.min(boxes[:, :, 1], axis=1)
    x_maxs = np.max(boxes[:, :, 0], axis=1)
    y_maxs = np.max(boxes[:, :, 1], axis=1)
    
    aabbs = np.column_stack((x_mins, y_mins, x_maxs, y_maxs))
    order = np.lexsort((x_mins, y_mins))
    aabbs = aabbs[order]

    clusters = []
    
    for aabb in aabbs:
        b_xmin, b_ymin, b_xmax, b_ymax = aabb
        added = False
        
        for c in clusters:
            c_xmin, c_ymin, c_xmax, c_ymax = c
            
            # calculate the distance between the boxes
            dx = max(0, max(b_xmin, c_xmin) - min(b_xmax, c_xmax))
            dy = max(0, max(b_ymin, c_ymin) - min(b_ymax, c_ymax))
            
            # check if the boxes are within the margin distance to be clustered together
            if max(dx, dy) <= margin:
                new_xmin = min(b_xmin, c_xmin)
                new_ymin = min(b_ymin, c_ymin)
                new_xmax = max(b_xmax, c_xmax)
                new_ymax = max(b_ymax, c_ymax)
                
                # check if the cluster is within the cluster size limit
                if (new_xmax - new_xmin) <= max_size and (new_ymax - new_ymin) <= max_size:
                    c[0], c[1], c[2], c[3] = new_xmin, new_ymin, new_xmax, new_ymax
                    added = True
                    break
                    
        if not added:
            clusters.append([b_xmin, b_ymin, b_xmax, b_ymax])
            
    # logger.info(f"Clustered {len(boxes)} text regions into {len(clusters)} large crops.")
    return clusters


def process_ocr(img, w, h):
    """
    Processes the image using the OCR engine using 2 passes.
    1st pass: detects text regions and clusters them into larger crops.
    2nd pass: runs the OCR engine on the clustered crops to get the final text detections.
    """
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
        "EngineConfig.onnxruntime.use_cuda":             USE_CUDA,
    })
    
    toggle_ocr_recognition(ocr, rec=False)
    
    coarse_res = ocr(img)
    if coarse_res is None or not hasattr(coarse_res, "boxes") or coarse_res.boxes is None:
        logger.info("No text regions detected.")
        return img, []
    
    c_boxes = cluster_regions(coarse_res.boxes)
    
    toggle_ocr_recognition(ocr, rec=True)
    ocr.update_params(unclip_ratio=1.4)
    
    final_detections = []
    for cbox in c_boxes:
        xmin = max(0, int(cbox[0]) - CROP_PADDING)
        ymin = max(0, int(cbox[1]) - CROP_PADDING)
        xmax = min(w, int(cbox[2]) + CROP_PADDING)
        ymax = min(h, int(cbox[3]) + CROP_PADDING)
        
        crop = img[ymin:ymax, xmin:xmax]
        if crop.shape[0] < 10 or crop.shape[1] < 10:
            continue
        
        res = ocr(crop)
        if res is None or res.boxes is None or len(res.txts) == 0:
            continue
        
        for box, text, score in zip(res.boxes, res.txts, res.scores):
            text = clean_text(text)
            if text == "":
                continue
            
            score = float(score)
            
            local_poly = np.array(box, dtype=np.int32)
            global_poly = local_poly + np.array([xmin, ymin])
            
            h1 = np.linalg.norm(global_poly[3] - global_poly[0])
            h2 = np.linalg.norm(global_poly[2] - global_poly[1])
            text_h = max(h1, h2)
            
            final_detections.append({
                "box": [
                    int(np.min(global_poly[:, 0])),
                    int(np.min(global_poly[:, 1])),
                    int(np.max(global_poly[:, 0])),
                    int(np.max(global_poly[:, 1]))
                ],
                "poly": global_poly.tolist(),
                "text": text,
                "score": score,
                "font_size": int(text_h)
            })
            
    final_detections = filter_detections(final_detections)
    # logger.info(f"Total text detections: {len(final_detections)}")
    
    return final_detections

def recognize_txt(img):
    """
    Recognizes text from the given PNG image and returns a list of detections with their bounding boxes, recognized text, confidence scores, and font sizes.
    """
    h, w = img.shape[:2]
    process_img = binarize_image(img)
    process_img = remove_thin_lines(process_img)
    process_img = cv.erode(process_img, np.ones((2, 2), np.uint8), iterations=1)
    process_img = remove_noise(process_img)
    process_img = cv.bitwise_not(process_img)
    process_img = cv.cvtColor(process_img, cv.COLOR_GRAY2BGR)
    
    detections = process_ocr(process_img, w, h)
    
    return detections
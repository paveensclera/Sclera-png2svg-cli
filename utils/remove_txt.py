import cv2 as cv
import numpy as np

def build_exact_mask(polys, h, w, thresh, kernel):
    mask = np.zeros((h, w), dtype=np.uint8)
    if not polys: 
        return mask
    cv.fillPoly(mask, polys, 255)
    mask = cv.bitwise_and(mask, thresh)
    return cv.dilate(mask, kernel, iterations=1)


def remove_txt(img, detections, output=None, min_wall_length=80):
    h, w = img.shape[:2]
    
    gray = cv.cvtColor(img, cv.COLOR_BGR2GRAY)
    if len(img.shape) == 3 and img.shape[2] == 3:
        hsv = cv.cvtColor(img, cv.COLOR_BGR2HSV)
        _, s, v = cv.split(hsv)
        gray[(s > 8) & (v > 100)] = 255
        
    _, binary = cv.threshold(gray, 200, 255, cv.THRESH_BINARY_INV)
    h_kernel = cv.getStructuringElement(cv.MORPH_RECT, (min_wall_length, 10))
    v_kernel = cv.getStructuringElement(cv.MORPH_RECT, (10, min_wall_length))
    
    structural_map = cv.bitwise_or(
        cv.morphologyEx(binary, cv.MORPH_OPEN, h_kernel),
        cv.morphologyEx(binary, cv.MORPH_OPEN, v_kernel)
    )
    
    ns_polys, telea_polys = [], []
    for det in detections:
        poly = np.array(det["poly"], dtype=np.int32)
        x, y, pw, ph = cv.boundingRect(poly)
        
        x, y = max(0, x), max(0, y)
        pw, ph = min(w - x, pw), min(h - y, ph)
        if pw <= 0 or ph <= 0:
            continue

        roi_struct = structural_map[y:y+ph, x:x+pw]
        roi_mask = np.zeros((ph, pw), dtype=np.uint8)
        cv.fillPoly(roi_mask, [poly - [x, y]], 255)
        
        if cv.bitwise_and(roi_mask, roi_struct).any():
            ns_polys.append(poly)
        else:
            telea_polys.append(poly)
            
    thresh = cv.adaptiveThreshold(gray, 255, cv.ADAPTIVE_THRESH_GAUSSIAN_C, cv.THRESH_BINARY_INV, 15, 5)
    kernel = np.ones((10, 10), np.uint8)

    ns_mask = build_exact_mask(ns_polys, h, w, thresh, kernel)
    telea_mask = build_exact_mask(telea_polys, h, w, thresh, kernel)

    img = img.copy()
    if telea_polys:
        img = cv.inpaint(img, telea_mask, inpaintRadius=1, flags=cv.INPAINT_TELEA)
    if ns_polys:
        img = cv.inpaint(img, ns_mask, inpaintRadius=1, flags=cv.INPAINT_NS)
        
    if output:
        cv.imwrite(str(output), img)

    return img
import cv2 as cv
import numpy as np

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
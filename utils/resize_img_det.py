import cv2 as cv

RESIZE_LIMIT = 4096

def resize_img_det(img, detections, max_side=RESIZE_LIMIT):
    h, w = img.shape[:2]
    largest = max(h, w)
    
    if largest <= max_side:
        return img, detections
        
    scale = max_side / float(largest)
    new_w = int(w * scale)
    new_h = int(h * scale)
    
    resized_img = cv.resize(img, (new_w, new_h), interpolation=cv.INTER_AREA)
    
    scaled_detections = []
    for d in detections:
        box = d["box"]
        poly = d["poly"]
        scaled_box = [int(val * scale) for val in box]
        scaled_poly = [[pt[0] * scale, pt[1] * scale] for pt in poly]
        
        scaled_detections.append({
            "box": scaled_box,
            "poly": scaled_poly,
            "text": d["text"],
            "score": d["score"],
            "font_size": int(d["font_size"] * scale)
        })
        
    return resized_img, scaled_detections
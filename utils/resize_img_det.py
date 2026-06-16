import cv2 as cv

RESIZE_LIMIT = 4096

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
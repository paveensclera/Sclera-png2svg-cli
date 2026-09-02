import cv2
import numpy as np


def is_rgb_image(
    img: np.ndarray, tolerance: int = 5, min_color_pixels: int = 50
) -> bool:
    """
    Check if an image contains meaningful RGB color.
    Returns True if the image has color content (recommended for VTracer),
    or False if it is grayscale/monochrome (recommended for Potrace).
    """
    if img.ndim == 2 or img.shape[2] == 1:
        return False

    # Fast strided sample check
    stride = 16
    sample_threshold = max(1, min_color_pixels // (stride * stride))
    sample = img[::stride, ::stride]

    diff_bg_sample = cv2.absdiff(sample[:, :, 0], sample[:, :, 1])
    if np.count_nonzero(diff_bg_sample > tolerance) >= sample_threshold:
        return True

    diff_gr_sample = cv2.absdiff(sample[:, :, 1], sample[:, :, 2])
    if np.count_nonzero(diff_gr_sample > tolerance) >= sample_threshold:
        return True

    # Full image check
    diff_bg = cv2.absdiff(img[:, :, 0], img[:, :, 1])
    if np.count_nonzero(diff_bg > tolerance) > min_color_pixels:
        return True

    diff_gr = cv2.absdiff(img[:, :, 1], img[:, :, 2])
    return np.count_nonzero(diff_gr > tolerance) > min_color_pixels

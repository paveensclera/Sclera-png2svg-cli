import cv2
import numpy as np

def is_rgb_image(
    img: np.ndarray,
    chroma_threshold: int = 6,
    min_color_percentage: float = 0.01,
    min_lightness: int = 10,
    max_lightness: int = 245,
) -> bool:
    """
    Fast and perceptually accurate color detection using CIELAB Chroma distance.
    Convert the image to CIELAB, measure how far each pixel's color is from neutral gray using 
    the a*/b* channels, ignore extreme light/dark pixels, calculate what percentage of pixels have meaningful chroma, and return True 
    if that percentage exceeds the threshold—with a fast sampled pass before doing the expensive full-image check.
    Args:
        img: Input image (BGR or BGRA).
        chroma_threshold: Minimum distance in a*b* plane from neutral gray (128, 128). Default: 6.
        min_color_percentage: Minimum percentage of colored pixels required. Default: 0.01.
        min_lightness: Minimum lightness (L channel) to consider (filters extreme black noise).
        max_lightness: Maximum lightness (L channel) to consider (filters extreme white noise).

    Returns:
        (is_color)
        - is_color: True (VTracer recommended) if color content exceeds threshold, False (Potrace) otherwise.

    """
    if img is None:
        raise ValueError("Image array is None")

    if img.ndim == 2 or img.shape[2] == 1:
        return False

    bgr = img[:, :, :3]
    thresh_sq = chroma_threshold * chroma_threshold

    # --------------------------------------------------------------------------
    # Step 1: Fast Strided Early Exit Check
    # --------------------------------------------------------------------------
    stride = 16
    sample = bgr[::stride, ::stride]
    sample_lab = cv2.cvtColor(sample, cv2.COLOR_BGR2Lab)

    a_samp = sample_lab[:, :, 1].astype(np.int16) - 128
    b_samp = sample_lab[:, :, 2].astype(np.int16) - 128
    L_samp = sample_lab[:, :, 0]

    sample_chroma_sq = a_samp * a_samp + b_samp * b_samp
    sample_valid = (L_samp >= min_lightness) & (L_samp <= max_lightness)
    sample_colored = (sample_chroma_sq > thresh_sq) & sample_valid

    sample_pct = (np.count_nonzero(sample_colored) / sample_colored.size) * 100
    if sample_pct >= min_color_percentage:
        return True

    # --------------------------------------------------------------------------
    # Step 2: Full Image Verification (Only runs if strided check misses)
    # --------------------------------------------------------------------------
    lab = cv2.cvtColor(bgr, cv2.COLOR_BGR2Lab)
    a = lab[:, :, 1].astype(np.int16) - 128
    b = lab[:, :, 2].astype(np.int16) - 128
    L = lab[:, :, 0]

    chroma_sq = a * a + b * b
    valid = (L >= min_lightness) & (L <= max_lightness)
    colored = (chroma_sq > thresh_sq) & valid

    color_pct = (np.count_nonzero(colored) / colored.size) * 100
    return color_pct >= min_color_percentage

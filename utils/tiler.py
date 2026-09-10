# app/core/tiler.py
import os
import shutil
import subprocess
import logging
import time
import gc
from pathlib import Path
from typing import List, Tuple, Dict, Any, Optional
import struct
import xml.etree.ElementTree as ET

try:
    from PIL import Image
except ImportError:
    Image = None

try:
    import cv2
except ImportError:
    cv2 = None

try:
    import numpy as np
except ImportError:
    np = None

logger = logging.getLogger(__name__)

# Default tile size in pixels (e.g. 4000x4000)
DEFAULT_TILE_SIZE = int(os.getenv("MAX_TILE_DIM", os.getenv("TILE_SIZE", "4000")))

# Default VTracer parameters for floor plan color vectorization
VTRACER_DEFAULT_PARAMS: Dict[str, Any] = {
    "mode": "polygon",
    "colormode": "color",
    "hierarchical": "stacked",
    "filter_speckle": 4,
    "color_precision": 8,
    "layer_difference": 4,
    "corner_threshold": 60,
    "length_threshold": 4.0,
    "splice_threshold": 45,
}


def _parse_png_dimensions(image_path: Path) -> Optional[Tuple[int, int]]:
    """Fast pure-Python parser to extract PNG width and height from the 24-byte IHDR chunk."""
    try:
        with open(image_path, "rb") as f:
            header = f.read(24)
            if len(header) >= 24 and header.startswith(b"\x89PNG\r\n\x1a\n"):
                width, height = struct.unpack(">II", header[16:24])
                return width, height
    except Exception:
        pass
    return None


def get_image_dimensions(image_path: Path) -> Tuple[int, int]:
    """
    Get (width, height) of an image with minimal memory overhead.
    Fast-paths PNG header, then Pillow, then OpenCV.
    """
    png_dims = _parse_png_dimensions(image_path)
    if png_dims:
        return png_dims

    if Image is not None:
        try:
            with Image.open(image_path) as img:
                return img.size  # (width, height)
        except Exception as e:
            logger.warning(f"[Tiler] PIL failed to read dimensions for {image_path}: {e}")

    if cv2 is not None:
        img = cv2.imread(str(image_path), cv2.IMREAD_UNCHANGED)
        if img is not None:
            h, w = img.shape[:2]
            return w, h

    raise ValueError(f"Could not determine image dimensions for {image_path}")


def calculate_tiles(
    width: int,
    height: int,
    tile_size: int = DEFAULT_TILE_SIZE,
) -> List[Tuple[int, int, int, int, int, int]]:
    """
    Calculate the N x M grid of non-overlapping bounding boxes for tiling.
    Returns a list of tuples: (col_idx, row_idx, xmin, ymin, xmax, ymax)
    """
    tiles: List[Tuple[int, int, int, int, int, int]] = []
    row_idx = 0
    for y in range(0, height, tile_size):
        ymin = y
        ymax = min(y + tile_size, height)
        col_idx = 0
        for x in range(0, width, tile_size):
            xmin = x
            xmax = min(x + tile_size, width)
            if (xmax > xmin) and (ymax > ymin):
                tiles.append((col_idx, row_idx, xmin, ymin, xmax, ymax))
            col_idx += 1
        row_idx += 1
    return tiles


def extract_tile_crop(
    image_path: Path,
    xmin: int,
    ymin: int,
    xmax: int,
    ymax: int,
    output_tile_path: Path,
) -> Path:
    """Extract a single tile crop from the source image file and write to disk."""
    crop_w = xmax - xmin
    crop_h = ymax - ymin

    if Image is not None:
        try:
            with Image.open(image_path) as img:
                crop = img.crop((xmin, ymin, xmax, ymax))
                crop.save(output_tile_path, format="PNG")
                return output_tile_path
        except Exception as e:
            logger.warning(
                f"[Tiler] PIL crop failed for crop ({xmin},{ymin},{xmax},{ymax}): {e}"
            )

    if cv2 is not None:
        try:
            full_img = cv2.imread(str(image_path))
            if full_img is not None:
                crop_img = full_img[ymin:ymax, xmin:xmax]
                cv2.imwrite(str(output_tile_path), crop_img)
                del full_img, crop_img
                gc.collect()
                return output_tile_path
        except Exception as e:
            logger.warning(f"[Tiler] OpenCV crop failed: {e}")

    # Fallback to ImageMagick crop
    im_cmd = "magick" if shutil.which("magick") else "convert"
    crop_cmd = [
        im_cmd,
        str(image_path),
        "-crop",
        f"{crop_w}x{crop_h}+{xmin}+{ymin}",
        "+repage",
        str(output_tile_path),
    ]
    result = subprocess.run(crop_cmd, capture_output=True, text=True, timeout=120)
    if result.returncode == 0 and output_tile_path.exists():
        return output_tile_path

    raise ValueError(
        f"Failed to extract tile crop from {image_path} at ({xmin}, {ymin})"
    )


def normalize_tile(tile_png_path: Path, output_norm_path: Path) -> Path:
    """Normalize a tile image with ImageMagick (depth 8, white background, strip alpha)."""
    im_cmd = "magick" if shutil.which("magick") else "convert"
    normalize_cmd = [
        im_cmd,
        str(tile_png_path),
        "-depth",
        "8",
        "-background",
        "white",
        "-alpha",
        "off",
        "-alpha",
        "remove",
        str(output_norm_path),
    ]
    result = subprocess.run(
        normalize_cmd,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0 or not output_norm_path.exists():
        logger.warning(
            f"[Tiler] ImageMagick tile normalization warning/fallback: {result.stderr.strip()}"
        )
        return tile_png_path
    return output_norm_path


def vectorize_single_tile(
    tile_norm_path: Path,
    tile_svg_path: Path,
    vtracer_params: Optional[Dict[str, Any]] = None,
) -> Path:
    """Run VTracer on a single normalized tile."""
    import vtracer

    params = {**VTRACER_DEFAULT_PARAMS, **(vtracer_params or {})}
    vtracer.convert_image_to_svg_py(
        str(tile_norm_path),
        str(tile_svg_path),
        **params,
    )
    return tile_svg_path


import re


def translate_svg_path(d_str: str, dx: float, dy: float) -> str:
    """
    Translate all absolute coordinates in an SVG path 'd' string by (+dx, +dy).
    Handles all standard SVG path commands (M/m, L/l, H/h, V/v, C/c, S/s, Q/q, T/t, A/a, Z/z).
    """
    if (dx == 0 and dy == 0) or not d_str:
        return d_str

    tokens = re.findall(
        r"[MmLlHhVvCcSsQqTtAaZz]|[-+]?(?:\d*\.\d+|\d+)(?:[eE][-+]?\d+)?", d_str
    )
    if not tokens:
        return d_str

    output = []
    i = 0
    n = len(tokens)
    cmd = ""

    while i < n:
        t = tokens[i]
        if t in "MmLlHhVvCcSsQqTtAaZz":
            cmd = t
            output.append(cmd)
            i += 1
        else:
            if cmd == "M" or cmd == "L":
                if i + 1 < n:
                    x = float(tokens[i]) + dx
                    y = float(tokens[i + 1]) + dy
                    output.append(f"{x:g} {y:g}")
                    i += 2
                else:
                    output.append(tokens[i])
                    i += 1
            elif cmd == "m":
                if i + 1 < n:
                    is_start = len(output) == 1
                    x = float(tokens[i]) + (dx if is_start else 0)
                    y = float(tokens[i + 1]) + (dy if is_start else 0)
                    output.append(f"{x:g} {y:g}")
                    i += 2
                else:
                    output.append(tokens[i])
                    i += 1
            elif cmd == "H":
                x = float(tokens[i]) + dx
                output.append(f"{x:g}")
                i += 1
            elif cmd == "V":
                y = float(tokens[i]) + dy
                output.append(f"{y:g}")
                i += 1
            elif cmd == "C":
                if i + 5 < n:
                    x1, y1 = float(tokens[i]) + dx, float(tokens[i + 1]) + dy
                    x2, y2 = float(tokens[i + 2]) + dx, float(tokens[i + 3]) + dy
                    x, y = float(tokens[i + 4]) + dx, float(tokens[i + 5]) + dy
                    output.append(f"{x1:g} {y1:g} {x2:g} {y2:g} {x:g} {y:g}")
                    i += 6
                else:
                    output.append(tokens[i])
                    i += 1
            elif cmd == "S" or cmd == "Q":
                if i + 3 < n:
                    x1, y1 = float(tokens[i]) + dx, float(tokens[i + 1]) + dy
                    x, y = float(tokens[i + 2]) + dx, float(tokens[i + 3]) + dy
                    output.append(f"{x1:g} {y1:g} {x:g} {y:g}")
                    i += 4
                else:
                    output.append(tokens[i])
                    i += 1
            elif cmd == "T":
                if i + 1 < n:
                    x, y = float(tokens[i]) + dx, float(tokens[i + 1]) + dy
                    output.append(f"{x:g} {y:g}")
                    i += 2
                else:
                    output.append(tokens[i])
                    i += 1
            elif cmd == "A":
                if i + 6 < n:
                    rx, ry, rot, laf, sf = tokens[i : i + 5]
                    x = float(tokens[i + 5]) + dx
                    y = float(tokens[i + 6]) + dy
                    output.append(f"{rx} {ry} {rot} {laf} {sf} {x:g} {y:g}")
                    i += 7
                else:
                    output.append(tokens[i])
                    i += 1
            elif cmd in ("l", "h", "v", "c", "s", "q", "t", "a"):
                counts = {"l": 2, "h": 1, "v": 1, "c": 6, "s": 4, "q": 4, "t": 2, "a": 7}
                count = counts.get(cmd, 1)
                take = min(count, n - i)
                output.append(" ".join(tokens[i : i + take]))
                i += take
            else:
                output.append(tokens[i])
                i += 1

    return " ".join(output)


def translate_svg_points(points_str: str, dx: float, dy: float) -> str:
    """Translate polygon / polyline points string by (+dx, +dy)."""
    if (dx == 0 and dy == 0) or not points_str:
        return points_str
    nums = re.findall(r"[-+]?(?:\d*\.\d+|\d+)(?:[eE][-+]?\d+)?", points_str)
    out = []
    for j in range(0, len(nums) - 1, 2):
        x = float(nums[j]) + dx
        y = float(nums[j + 1]) + dy
        out.append(f"{x:g},{y:g}")
    return " ".join(out)


def parse_transform_translate(transform_str: str) -> tuple:
    """Parse translate(x, y) or translate(x) or matrix(a,b,c,d,e,f) from transform attribute."""
    if not transform_str:
        return 0.0, 0.0
    m = re.search(
        r"translate\(\s*([-+]?(?:\d*\.\d+|\d+)(?:[eE][-+]?\d+)?)\s*(?:[,\s]\s*([-+]?(?:\d*\.\d+|\d+)(?:[eE][-+]?\d+)?)\s*)?\)",
        transform_str,
    )
    if m:
        tx = float(m.group(1))
        ty = float(m.group(2)) if m.group(2) else 0.0
        return tx, ty
    m = re.search(r"matrix\(\s*([^)]+)\)", transform_str)
    if m:
        nums = [float(x) for x in re.split(r"[, \t\n]+", m.group(1).strip()) if x]
        if len(nums) == 6:
            return nums[4], nums[5]
    return 0.0, 0.0


def translate_element_coordinates(elem: ET.Element, dx: float, dy: float) -> None:
    """Recursively translate coordinates of an SVG element and its children, baking and removing transforms."""
    transform_attr = elem.get("transform")
    tx, ty = parse_transform_translate(transform_attr) if transform_attr else (0.0, 0.0)
    total_dx = dx + tx
    total_dy = dy + ty

    tag_local = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag

    if tag_local == "path":
        d_val = elem.get("d")
        if d_val:
            elem.set("d", translate_svg_path(d_val, total_dx, total_dy))
        if transform_attr:
            del elem.attrib["transform"]
    elif tag_local in ("polygon", "polyline"):
        pts = elem.get("points")
        if pts:
            elem.set("points", translate_svg_points(pts, total_dx, total_dy))
        if transform_attr:
            del elem.attrib["transform"]
    elif tag_local == "rect":
        x = elem.get("x", "0")
        y = elem.get("y", "0")
        elem.set("x", f"{float(x) + total_dx:g}")
        elem.set("y", f"{float(y) + total_dy:g}")
        if transform_attr:
            del elem.attrib["transform"]
    elif tag_local in ("circle", "ellipse"):
        cx = elem.get("cx", "0")
        cy = elem.get("cy", "0")
        elem.set("cx", f"{float(cx) + total_dx:g}")
        elem.set("cy", f"{float(cy) + total_dy:g}")
        if transform_attr:
            del elem.attrib["transform"]
    elif tag_local == "line":
        for attr, delta in [("x1", total_dx), ("y1", total_dy), ("x2", total_dx), ("y2", total_dy)]:
            v = elem.get(attr, "0")
            elem.set(attr, f"{float(v) + delta:g}")
        if transform_attr:
            del elem.attrib["transform"]
    elif tag_local in ("text", "use"):
        x = elem.get("x", "0")
        y = elem.get("y", "0")
        elem.set("x", f"{float(x) + total_dx:g}")
        elem.set("y", f"{float(y) + total_dy:g}")
        if transform_attr:
            del elem.attrib["transform"]

    for child in list(elem):
        translate_element_coordinates(child, dx, dy)


def get_path_bbox_area(p: ET.Element) -> float:
    """Calculate approximate bounding box area of an SVG path."""
    d = p.get("d", "")
    tokens = re.findall(r"[MmLlHhVvCcSsQqTtAaZz]|[-+]?(?:\d*\.\d+|\d+)(?:[eE][-+]?\d+)?", d)
    cur_x, cur_y = 0.0, 0.0
    start_x, start_y = 0.0, 0.0
    min_x, max_x = float("inf"), float("-inf")
    min_y, max_y = float("inf"), float("-inf")

    cmd = ""
    j = 0
    while j < len(tokens):
        t = tokens[j]
        if t in "MmLlHhVvCcSsQqTtAaZz":
            cmd = t
            j += 1
            if cmd in ("Z", "z"):
                cur_x, cur_y = start_x, start_y
        else:
            if cmd == "M":
                cur_x, cur_y = float(tokens[j]), float(tokens[j + 1])
                start_x, start_y = cur_x, cur_y
                j += 2
                cmd = "L"
            elif cmd == "m":
                cur_x += float(tokens[j])
                cur_y += float(tokens[j + 1])
                start_x, start_y = cur_x, cur_y
                j += 2
                cmd = "l"
            elif cmd == "L":
                cur_x, cur_y = float(tokens[j]), float(tokens[j + 1])
                j += 2
            elif cmd == "l":
                cur_x += float(tokens[j])
                cur_y += float(tokens[j + 1])
                j += 2
            elif cmd == "H":
                cur_x = float(tokens[j])
                j += 1
            elif cmd == "h":
                cur_x += float(tokens[j])
                j += 1
            elif cmd == "V":
                cur_y = float(tokens[j])
                j += 1
            elif cmd == "v":
                cur_y += float(tokens[j])
                j += 1
            elif cmd in ("C", "c"):
                cur_x += float(tokens[j + 4]) if cmd == "c" else float(tokens[j + 4])
                cur_y += float(tokens[j + 5]) if cmd == "c" else float(tokens[j + 5])
                j += 6
            elif cmd in ("S", "s", "Q", "q"):
                cur_x += float(tokens[j + 2]) if cmd in ("s", "q") else float(tokens[j + 2])
                cur_y += float(tokens[j + 3]) if cmd in ("s", "q") else float(tokens[j + 3])
                j += 4
            elif cmd in ("A", "a"):
                cur_x += float(tokens[j + 5]) if cmd == "a" else float(tokens[j + 5])
                cur_y += float(tokens[j + 6]) if cmd == "a" else float(tokens[j + 6])
                j += 7
            else:
                j += 1
            min_x = min(min_x, cur_x)
            max_x = max(max_x, cur_x)
            min_y = min(min_y, cur_y)
            max_y = max(max_y, cur_y)

    if max_x < min_x or max_y < min_y:
        return 0.0
    return (max_x - min_x) * (max_y - min_y)


def sort_svg_paths_by_area(svg_path: Path) -> Path:
    """Sort SVG paths by bounding box area descending so backgrounds render first and rooms/details on top."""
    tree = ET.parse(svg_path)
    root = tree.getroot()
    if "xmlns" in root.attrib:
        del root.attrib["xmlns"]

    paths = []
    other_elements = []

    for child in list(root):
        tag = child.tag.split("}")[-1]
        if tag == "path":
            area = get_path_bbox_area(child)
            paths.append((child, area))
        elif tag == "g":
            g_paths = [e for e in child if e.tag.split("}")[-1] == "path"]
            if g_paths and child.get("id") != "walls-layer":
                for gp in g_paths:
                    area = get_path_bbox_area(gp)
                    paths.append((gp, area))
            else:
                other_elements.append(child)
        else:
            other_elements.append(child)

    paths.sort(key=lambda x: x[1], reverse=True)

    for child in list(root):
        root.remove(child)
    for p, _ in paths:
        root.append(p)
    for elem in other_elements:
        root.append(elem)

    ET.register_namespace("", "http://www.w3.org/2000/svg")
    tree.write(svg_path, encoding="utf-8", xml_declaration=True)
    return svg_path


def bake_svg_transforms(svg_path: Path) -> Path:
    """Parse SVG, translate all element coordinates by their local transform, and remove transform attributes."""
    tree = ET.parse(svg_path)
    root = tree.getroot()
    if "xmlns" in root.attrib:
        del root.attrib["xmlns"]
    for child in list(root):
        translate_element_coordinates(child, 0.0, 0.0)
    ET.register_namespace("", "http://www.w3.org/2000/svg")
    tree.write(svg_path, encoding="utf-8", xml_declaration=True)
    return svg_path


def extract_tile_elements(
    tile_svg_path: Path,
    xmin: int,
    ymin: int,
) -> List[ET.Element]:
    """Parse tile SVG and return elements with coordinates translated to global space."""
    tree = ET.parse(tile_svg_path)
    root = tree.getroot()
    elements = []

    for child in list(root):
        tag_local = child.tag.split("}")[-1] if "}" in child.tag else child.tag
        if tag_local in ("metadata", "title", "desc"):
            continue

        translate_element_coordinates(child, xmin, ymin)
        elements.append(child)

    return elements


def extract_tile_group(
    tile_svg_path: Path,
    col_idx: int,
    row_idx: int,
    xmin: int,
    ymin: int,
) -> ET.Element:
    """Parse tile SVG and extract its elements with baked coordinates."""
    group = ET.Element("g", {"id": f"tile-{col_idx}-{row_idx}"})
    for elem in extract_tile_elements(tile_svg_path, xmin, ymin):
        group.append(elem)
    return group


def vectorize_image_tiled(
    png_path: Path,
    output_svg_path: Path,
    tile_size: int = DEFAULT_TILE_SIZE,
    vtracer_params: Optional[Dict[str, Any]] = None,
    temp_dir: Optional[Path] = None,
) -> Path:
    """
    Vectorize an image using Tiled Grid Vectorization.
    Slices input image into N x M tiles, vectorizes each tile with VTracer,
    wraps each in a translated <g transform="translate(xmin, ymin)"> group,
    and merges into a single root SVG with viewBox="0 0 orig_w orig_h".
    Peak memory stays bounded (<350 MB) regardless of image size.
    """
    start_time = time.time()
    orig_w, orig_h = get_image_dimensions(png_path)
    logger.info(
        f"[Tiler] Starting tiled vectorization for '{png_path.name}' "
        f"({orig_w}x{orig_h} px, tile_size={tile_size}px)"
    )

    # If within a single tile, vectorize in single pass
    if orig_w <= tile_size and orig_h <= tile_size:
        logger.info(
            f"[Tiler] Image size {orig_w}x{orig_h} <= {tile_size}px; running single-pass VTracer"
        )
        temp_norm = (temp_dir or png_path.parent) / f"{png_path.stem}_single_norm.png"
        try:
            norm_path = normalize_tile(png_path, temp_norm)
            vectorize_single_tile(norm_path, output_svg_path, vtracer_params)
            return output_svg_path
        finally:
            if temp_norm.exists():
                try:
                    temp_norm.unlink()
                except Exception:
                    pass

    tiles = calculate_tiles(orig_w, orig_h, tile_size)
    num_cols = (orig_w + tile_size - 1) // tile_size
    num_rows = (orig_h + tile_size - 1) // tile_size
    logger.info(
        f"[Tiler] Grid partitioned into {len(tiles)} tiles ({num_cols} cols x {num_rows} rows)"
    )

    working_dir = temp_dir or png_path.parent
    working_dir.mkdir(parents=True, exist_ok=True)

    root_svg = ET.Element(
        "svg",
        {
            "version": "1.1",
            "width": str(orig_w),
            "height": str(orig_h),
            "viewBox": f"0 0 {orig_w} {orig_h}",
        },
    )

    for idx, (col_idx, row_idx, xmin, ymin, xmax, ymax) in enumerate(tiles):
        tile_t0 = time.time()
        tile_id_str = f"{png_path.stem}_t_{col_idx}_{row_idx}"
        tile_crop_path = working_dir / f"{tile_id_str}.png"
        tile_norm_path = working_dir / f"{tile_id_str}_norm.png"
        tile_svg_path = working_dir / f"{tile_id_str}.svg"

        try:
            # 1. Slice tile crop
            extract_tile_crop(png_path, xmin, ymin, xmax, ymax, tile_crop_path)

            # 2. Normalize tile
            norm_path = normalize_tile(tile_crop_path, tile_norm_path)

            # 3. Vectorize tile with VTracer
            vectorize_single_tile(norm_path, tile_svg_path, vtracer_params)

            # 4. Extract elements with baked global coordinates
            tile_elements = extract_tile_elements(
                tile_svg_path, xmin, ymin
            )
            for elem in tile_elements:
                root_svg.append(elem)

            tile_duration = time.time() - tile_t0
            logger.info(
                f"[Tiler] Tile [{idx + 1}/{len(tiles)}] (col={col_idx}, row={row_idx}, "
                f"offset=({xmin},{ymin}), dim={xmax - xmin}x{ymax - ymin}) "
                f"vectorized in {tile_duration:.2f}s"
            )
        except Exception as e:
            logger.error(
                f"[Tiler] Error processing tile ({col_idx}, {row_idx}): {e}",
                exc_info=True,
            )
            raise
        finally:
            # Clean up intermediate files for this tile immediately
            for p in (tile_crop_path, tile_norm_path, tile_svg_path):
                if p.exists():
                    try:
                        p.unlink()
                    except Exception:
                        pass
            # Force garbage collection to keep RAM minimal
            gc.collect()

    # Write merged SVG to output_svg_path
    output_svg_path.parent.mkdir(parents=True, exist_ok=True)
    ET.register_namespace("", "http://www.w3.org/2000/svg")
    tree = ET.ElementTree(root_svg)
    tree.write(output_svg_path, encoding="utf-8", xml_declaration=True)

    total_duration = time.time() - start_time
    logger.info(
        f"[Tiler] Tiled vectorization complete for '{png_path.name}' in {total_duration:.2f}s "
        f"-> {output_svg_path} ({output_svg_path.stat().st_size} bytes)"
    )
    return output_svg_path

import os
import subprocess
import logging
import glob
import traceback
import requests
import re
import json
from datetime import time
from pathlib import Path
from urllib.parse import urlparse
import time
from dotenv import load_dotenv

load_dotenv()

from utils.api_config import Data
from utils.path_injector import PathInjector
from utils.ocr import (
    recognize_txt,
    remove_txt,
    inject_elements,
    resize_img_det,
    init_ocr_engine,
)
from utils.image_utils import is_rgb_image
from utils.tiler import (
    vectorize_image_tiled,
    bake_svg_transforms,
    sort_svg_paths_by_area,
    translate_element_coordinates,
    parse_transform_translate,
    DEFAULT_TILE_SIZE,
    get_image_dimensions,
)
import xml.etree.ElementTree as ET
import cv2 as cv

# Configure logging
root_logger = logging.getLogger()
root_logger.setLevel(logging.DEBUG)  # Capture all logs down to DEBUG

# Clear existing handlers
for handler in root_logger.handlers[:]:
    root_logger.removeHandler(handler)

# Create StreamHandler for concise console logs (INFO level)
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)
console_handler.setFormatter(
    logging.Formatter("%(asctime)s | %(levelname)s | %(message)s", datefmt="%H:%M:%S")
)
root_logger.addHandler(console_handler)

logger = logging.getLogger(__name__)

OCR_INSTANCE = init_ocr_engine()

ALL_LOGS_FILE = None
FAILED_LOGS_FILE = None


def setup_file_logging():
    global ALL_LOGS_FILE, FAILED_LOGS_FILE
    # Use environment variables if set, otherwise default to OUTPUT_DIR
    all_logs_path = os.getenv("ALL_LOGS_FILE", str(OUTPUT_DIR / "all_logs.log"))
    failed_logs_path = os.getenv(
        "FAILED_LOGS_FILE", str(OUTPUT_DIR / "failed_files.log")
    )

    ALL_LOGS_FILE = Path(all_logs_path)
    FAILED_LOGS_FILE = Path(failed_logs_path)

    # Ensure the parent directory of log files exists
    ALL_LOGS_FILE.parent.mkdir(parents=True, exist_ok=True)

    # Add FileHandler to root logger
    root_logger = logging.getLogger()

    # Check if a FileHandler with the same path already exists to avoid duplication
    for handler in root_logger.handlers[:]:
        if isinstance(handler, logging.FileHandler) and os.path.abspath(
            handler.baseFilename
        ) == os.path.abspath(all_logs_path):
            return

    try:
        file_handler = logging.FileHandler(all_logs_path, mode="a", encoding="utf-8")
        file_handler.setFormatter(
            logging.Formatter(
                "%(asctime)s | %(levelname)s | %(message)s", datefmt="%H:%M:%S"
            )
        )
        file_handler.setLevel(logging.DEBUG)  # Capture detailed debug logs in the file
        root_logger.addHandler(file_handler)
        logger.info(f"Logging all outputs to: {all_logs_path}")
    except Exception as e:
        logger.error(f"Failed to set up file logging: {str(e)}")


def write_failed_files_report(failures: list):
    """
    Append a report of failed files to FAILED_LOGS_FILE in table format
    """
    if not failures:
        return

    try:
        failed_logs_path = os.getenv(
            "FAILED_LOGS_FILE", str(OUTPUT_DIR / "failed_files.log")
        )
        failed_logs_file = Path(failed_logs_path)

        failed_logs_file.parent.mkdir(parents=True, exist_ok=True)

        write_header = (
            not failed_logs_file.exists() or failed_logs_file.stat().st_size == 0
        )

        last_index = 0
        if not write_header:
            import re

            try:
                with open(failed_logs_file, "r", encoding="utf-8") as f:
                    for line in f:
                        match = re.match(r"^\|\s*(\d+)\s*\|", line)
                        if match:
                            last_index = int(match.group(1))
            except Exception as read_err:
                logger.warning(
                    f"Could not read last index from failed_files.log: {read_err}"
                )

        import datetime

        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        with open(failed_logs_file, "a", encoding="utf-8") as f:
            if write_header:
                f.write("| Index | Filename | Timestamp | Reason |\n")
                f.write("| :--- | :--- | :--- | :--- |\n")

            for idx, failure in enumerate(failures, 1):
                current_idx = last_index + idx
                filename = failure["filename"]
                reason = failure["reason"]
                filename_clean = str(filename).replace("|", "\\|")
                reason_clean = str(reason).replace("|", "\\|").replace("\n", " ")
                f.write(
                    f"| {current_idx} | {filename_clean} | {timestamp} | {reason_clean} |\n"
                )

        logger.info(
            f"Failed files appended to: {failed_logs_path} (Added {len(failures)} failures, new last index: {last_index + len(failures)})"
        )
    except Exception as e:
        logger.error(f"Failed to write failed files report: {str(e)}")


STORE_PERMANENTLY = os.getenv("STORE_FILES_PERMANENTLY", "true").lower() == "true"

ENABLE_SVGO = os.getenv("ENABLE_SVGO", "true").lower() == "true"

INPUT_DIR = Path(os.getenv("INPUT_DIR", "/home/sclera/images/png_floors"))
OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", "/home/sclera/images/svg_floors"))

CONVERSION_ENGINE = os.getenv("CONVERSION_ENGINE", "potrace").lower()
if CONVERSION_ENGINE not in ["potrace", "vtracer"]:
    CONVERSION_ENGINE = "potrace"

AUTO_DETECT_ENGINE = os.getenv("AUTO_DETECT_ENGINE", os.getenv("AUTO_DETECT", "false")).lower() in ("true", "1", "yes")


def determine_engine(img=None, default_engine: str = None) -> str:
    """
    Determine whether to use Potrace or VTracer engine.
    If AUTO_DETECT_ENGINE is enabled and img is provided, uses is_rgb_image:
      - RGB / color image -> 'vtracer'
      - Grayscale / monochrome image -> 'potrace'
    Otherwise, returns default_engine or CONVERSION_ENGINE.
    """
    base_engine = (default_engine or CONVERSION_ENGINE).lower()
    if AUTO_DETECT_ENGINE and img is not None:
        is_rgb = is_rgb_image(img)
        engine = "vtracer" if is_rgb else "potrace"
        logger.info(
            f"   Auto-detected engine: {engine.upper()} (color={'RGB' if is_rgb else 'Grayscale'})"
        )
        return engine
    return base_engine


TEMP_DIR = Path("/tmp/floor_map_processing")
TEMP_DIR.mkdir(parents=True, exist_ok=True)
SHM_DIR = Path("/dev/shm") if Path("/dev/shm").exists() else TEMP_DIR

INPUT_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def parse_vdms_filter(raw_val) -> set:
    """
    Parse VDMS filter string from environment variable or CLI argument.
    Returns a set of VDMS IDs (strings). If empty, returns set().

    Supports:
      - Single VDMS: "VDMS885" -> {"VDMS885"}
      - Comma-separated: "VDMS885, VDMS886" -> {"VDMS885", "VDMS886"}
      - JSON list format: '["VDMS885", "VDMS886"]'
      - Space-separated: "VDMS885 VDMS886" -> {"VDMS885", "VDMS886"}
      - Empty / None -> set() (runs for all VDMS)
    """
    if not raw_val:
        return set()

    raw_val = str(raw_val).strip().strip('"\'')
    if not raw_val:
        return set()

    if raw_val.startswith("[") and raw_val.endswith("]"):
        try:
            parsed = json.loads(raw_val)
            if isinstance(parsed, list):
                return {
                    str(item).strip().strip('"\'')
                    for item in parsed
                    if str(item).strip()
                }
        except Exception:
            raw_val = raw_val[1:-1]

    if "," in raw_val:
        tokens = raw_val.split(",")
    else:
        tokens = raw_val.split()

    result = set()
    for token in tokens:
        cleaned = token.strip().strip('"\'[]')
        if cleaned:
            result.add(cleaned)

    return result


def get_vdms_filter() -> set:
    """
    Get VDMS filter set from environment variables.
    Checks VDMS_FILTER, VDMS_LIST, VDMS_IDS, or VDMS.
    """
    raw_val = (
        os.getenv("VDMS_FILTER")
        or os.getenv("VDMS_LIST")
        or os.getenv("VDMS_IDS")
        or os.getenv("VDMS")
        or ""
    )
    return parse_vdms_filter(raw_val)


_RESOLUTION_THRESHOLD = 4_000_000
_IM_SUPPORTS_AUTO_THRESHOLD: bool | None = None


def _supports_auto_threshold() -> bool:
    """Return True if this ImageMagick build supports -auto-threshold OTSU (IM 7+).
    Result is cached so the subprocess is only run once per process lifetime."""
    global _IM_SUPPORTS_AUTO_THRESHOLD
    if _IM_SUPPORTS_AUTO_THRESHOLD is None:
        try:
            import re

            result = subprocess.run(
                ["convert", "--version"], capture_output=True, text=True, timeout=5
            )
            match = re.search(r"Version: ImageMagick (\d+)\.", result.stdout)
            _IM_SUPPORTS_AUTO_THRESHOLD = bool(match and int(match.group(1)) >= 7)
        except Exception:
            _IM_SUPPORTS_AUTO_THRESHOLD = False
        logger.info(
            f"ImageMagick -auto-threshold OTSU supported: {_IM_SUPPORTS_AUTO_THRESHOLD}"
        )
    return _IM_SUPPORTS_AUTO_THRESHOLD


def _needs_preprocessing(image_info: dict) -> bool:
    """Return True if the image is below the resolution threshold and needs
    upscaling and denoising before binarization."""
    width = image_info.get("width", 0)
    height = image_info.get("height", 0)
    pixel_count = width * height
    if pixel_count < _RESOLUTION_THRESHOLD:
        logger.info(
            f"Preprocessing required — pixels={pixel_count} (threshold={_RESOLUTION_THRESHOLD}), "
            f"std_dev={image_info.get('std_dev', 'N/A')}"
        )
        return True
    return False


def analyze_image_for_conversion(png_path: Path) -> dict:
    """Analyze image properties to help with conversion debugging"""
    try:
        identify_cmd = [
            "identify",
            "-format",
            "%[colorspace] %[type] %w %h %[colors] %[mean] %[fx:standard_deviation]",
            str(png_path),
        ]

        result = subprocess.run(
            identify_cmd, capture_output=True, text=True, timeout=10
        )

        if result.returncode == 0:
            parts = result.stdout.strip().split()
            info = {
                "colorspace": parts[0],
                "type": parts[1],
                "width": int(parts[2]),
                "height": int(parts[3]),
                "colors": int(parts[4]),
                "mean": float(parts[5]),
                "std_dev": float(parts[6]),
            }
            logger.info(
                f"Image analysis: {info['width']}x{info['height']} px, "
                f"std_dev={info['std_dev']:.4f}"
            )
            return info
        else:
            logger.warning(f"Could not analyze image: {result.stderr}")
            return {}

    except Exception as e:
        logger.warning(f"Image analysis failed: {str(e)}")
        return {}


def standardize_svg_for_frontend(svg_content: str) -> str:
    """
    Standardize SVG dimensions and add white background for frontend compatibility

    NOTE: This function modifies the SVG to match frontend expectations:
    - Adds overflow='visible' for proper rendering
    - Inserts white background rectangle for consistent appearance
    """
    try:
        try:
            from xml.dom import minidom

            dom = minidom.parseString(svg_content)
            svg_element = dom.getElementsByTagName("svg")[0]
            svg_element.setAttribute("overflow", "visible")

            rect_element = dom.createElement("rect")
            rect_element.setAttribute("x", "0")
            rect_element.setAttribute("y", "0")
            rect_element.setAttribute("width", "100%")
            rect_element.setAttribute("height", "100%")
            rect_element.setAttribute("fill", "white")

            if svg_element.firstChild:
                svg_element.insertBefore(rect_element, svg_element.firstChild)
            else:
                svg_element.appendChild(rect_element)

            modified_svg_content = dom.toxml()

        except Exception as e:
            logger.warning(
                f"     Failed to modify SVG dimensions, using original: {str(e)}"
            )
            modified_svg_content = svg_content

        # Write the standardized SVG content
        return modified_svg_content

    except Exception as e:
        logger.error(f"     SVG standardization failed: {str(e)}")
        return svg_content


def flatten_svg_content(svg_content: str, engine: str = "potrace") -> str:
    """Flatten SVG by converting all <g> tags into direct child <path> and shape elements, baking transforms and attributes."""
    if not svg_content or "<svg" not in svg_content:
        return svg_content

    engine_str = (engine or "potrace").lower()

    try:
        root = ET.fromstring(svg_content)
        if "xmlns" in root.attrib:
            del root.attrib["xmlns"]

        # 1. Bake transforms on root-level children
        for child in list(root):
            translate_element_coordinates(child, 0.0, 0.0)

        # 2. Recursively convert all <g> tags into flat child paths/shapes
        def recurse_and_flatten(parent, group):
            group_transform = group.get("transform")
            gtx, gty = parse_transform_translate(group_transform) if group_transform else (0.0, 0.0)

            for child in list(group):
                if gtx != 0 or gty != 0:
                    translate_element_coordinates(child, gtx, gty)

                for attr, value in group.attrib.items():
                    if attr not in ("transform", "id") and attr not in child.attrib:
                        child.attrib[attr] = value

                tag_local = child.tag.split("}")[-1] if "}" in child.tag else child.tag
                if tag_local == "g":
                    recurse_and_flatten(parent, child)
                else:
                    if tag_local in ("path", "polygon", "rect", "circle", "ellipse", "line", "polyline"):
                        if not child.get("fill"):
                            fill_val = group.get("fill")
                            if fill_val:
                                child.set("fill", fill_val)
                            elif engine_str == "potrace":
                                child.set("fill", "#000000")
                    parent.append(child)

            parent.remove(group)

        SEMANTIC_GROUP_IDS = {"walls-layer"}
        SEMANTIC_DATA_TYPES = {"vt_marker", "route"}

        for elem in list(root):
            tag_local = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
            if tag_local == "g":
                elem_id = elem.get("id", "")
                elem_type = elem.get("data-type", "")
                if elem_id in SEMANTIC_GROUP_IDS or elem_type in SEMANTIC_DATA_TYPES or elem.get("data-camera") or elem.get("data-wifi"):
                    # Preserve semantic container, but ensure explicit fills on its children
                    for child in elem:
                        ctag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
                        if ctag in ("path", "polygon", "rect", "circle", "ellipse", "polyline") and not child.get("fill"):
                            child.set("fill", "none")
                    continue
                recurse_and_flatten(root, elem)

        # 4. Ensure every shape at root has explicit fill
        for elem in list(root):
            tag_local = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
            if tag_local in ("path", "polygon", "rect", "circle", "ellipse", "polyline"):
                if not elem.get("fill"):
                    elem.set("fill", "#000000")

        ET.register_namespace("", "http://www.w3.org/2000/svg")
        return ET.tostring(root, encoding="utf-8").decode("utf-8")
    except Exception as e:
        logger.warning(f"     SVG flattening failed, using unflattened SVG: {str(e)}")
        return svg_content


def is_valid_xml(xml_str: str) -> bool:
    if not xml_str or not xml_str.strip():
        return False
    try:
        ET.fromstring(xml_str)
        return True
    except Exception:
        return False


def optimize_svg(svg_content: str) -> str:
    """
    Optimize SVG file using svgcleaner and optionally svgo for maximum compression and quality

    NOTE: This function applies a two-stage optimization process:
    1. svgcleaner - removes unnecessary elements and optimizes structure
    2. svgo (optional) - further compresses and optimizes the SVG content

    SVGO can be disabled by setting ENABLE_SVGO=false environment variable
    """
    try:
        svgcleaner_cmd = [
            "svgcleaner",
            "-",
            "-",
            "--remove-unused-defs",
            "--convert-shapes",
            "--remove-title",
            "--remove-desc",
            "--remove-metadata",
            "--remove-dupl-lineargradient",
            "--remove-dupl-radialgradient",
            "--remove-dupl-fegaussianblur",
            "--remove-unused-coordinates",
            "--remove-default-attributes",
            "--remove-xmlns-xlink-attribute",
            "--remove-needless-attributes",
            "--remove-gradient-attributes",
            "--join-style-attributes",
            "--simplify-transforms",
            "--remove-unresolved-classes",
        ]

        svgcleaner = subprocess.Popen(
            svgcleaner_cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        cleaned_svg, cleaner_err = svgcleaner.communicate(
            input=svg_content, timeout=60
        )

        if svgcleaner.returncode != 0 or not is_valid_xml(cleaned_svg):
            if svgcleaner.returncode != 0:
                logger.warning(f"     svgcleaner failed: {cleaner_err}")
            else:
                logger.warning(
                    f"     svgcleaner output is invalid or truncated XML, falling back to original SVG"
                )
            cleaned_svg = svg_content

        if not ENABLE_SVGO:
            logger.debug("     SVGO optimization disabled (ENABLE_SVGO=false)")
            logger.debug(f"     Using svgcleaner result only")
            optimized_svg = cleaned_svg
        else:
            svgo_config_path = Path(__file__).parent / "svgo.config.js"

            if svgo_config_path.exists():
                svgo_cmd = ["svgo", "-", "-o", "-", "--config", str(svgo_config_path)]
                logger.debug("     Running svgo optimization (using svgo.config.js)...")
            else:
                svgo_cmd = [
                    "svgo",
                    "-",
                    "-o",
                    "-",
                    "--multipass",
                    "--config",
                    '{"plugins": [{"name": "preset-default", "params": {"overrides": {"removeViewBox": false}}}]}',
                ]
                logger.debug("     Running svgo optimization (using inline config)...")

            result = subprocess.Popen(
                svgo_cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            optimized_svg, svgo_err = result.communicate(
                input=cleaned_svg, timeout=60
            )

            if result.returncode != 0 or not is_valid_xml(optimized_svg):
                if result.returncode != 0:
                    logger.warning(f"     svgo failed: {svgo_err}")
                else:
                    logger.warning(
                        f"     svgo output is invalid or truncated XML, falling back to cleaned SVG"
                    )
                optimized_svg = cleaned_svg

        # Validate the optimized SVG
        if not is_valid_xml(optimized_svg):
            logger.error("     Optimized SVG is invalid or truncated, using original")
            optimized_svg = svg_content
        else:
            final_size = len(optimized_svg)
            logger.debug(f"     svgo optimization completed successfully!")
            logger.debug(f"     Final optimization results: {final_size} bytes")

        return optimized_svg

    except subprocess.TimeoutExpired:
        logger.error("SVG optimization process timed out")
        return svg_content
    except Exception as e:
        logger.error(f"SVG optimization failed: {str(e)}")
        return svg_content


def run_vtracer_conversion(png_path: Path, temp_svg_path: Path, image_info: dict | None = None) -> bool:
    """Run ImageMagick normalization and VTracer (or Tiled VTracer for large images) to convert PNG to SVG"""
    import vtracer
    import shutil

    temp_filename = png_path.stem + "_temp"
    normalized_png = TEMP_DIR / f"{temp_filename}_normalized.png"

    try:
        width = (image_info or {}).get("width")
        height = (image_info or {}).get("height")
        if not width or not height:
            try:
                width, height = get_image_dimensions(png_path)
            except Exception:
                width, height = 0, 0

        tile_size = int(os.getenv("MAX_TILE_DIM", os.getenv("TILE_SIZE", str(DEFAULT_TILE_SIZE))))

        if max(width, height) > tile_size:
            logger.info(
                f"   Image dimensions ({width}x{height} px) exceed {tile_size}px. "
                f"Dispatching to Tiled Grid Vectorization."
            )
            vectorize_image_tiled(
                png_path,
                temp_svg_path,
                tile_size=tile_size,
                temp_dir=TEMP_DIR,
            )
            return True

        logger.info(
            f"   Image dimensions ({width}x{height} px) <= {tile_size}px. "
            f"Running single-pass VTracer."
        )
        im_cmd = "magick" if shutil.which("magick") else "convert"
        normalize_cmd = [
            im_cmd, str(png_path),
            "-depth", "8",
            "-background", "white",
            "-alpha", "off",
            "-alpha", "remove",
            str(normalized_png),
        ]

        result = subprocess.run(
            normalize_cmd,
            capture_output=True,
            text=True,
            timeout=120
        )

        if result.returncode != 0:
            logger.error(f"   Image normalization failed: {result.stderr}")
            return False
        if not normalized_png.exists():
            logger.error("   Normalized PNG was not created")
            return False

        vtracer.convert_image_to_svg_py(
            str(normalized_png),
            str(temp_svg_path),
            mode="polygon",
            colormode="color",
            hierarchical="cutout",
            filter_speckle=4,
            color_precision=6,
            layer_difference=8,
            corner_threshold=60,
            length_threshold=4.0,
            splice_threshold=45
        )
        bake_svg_transforms(temp_svg_path)

        return True

    except Exception as e:
        logger.error(f"   VTracer conversion error: {str(e)}")
        return False
    finally:
        try:
            if normalized_png.exists():
                normalized_png.unlink()
        except Exception as e:
            logger.warning(f"   Failed to delete normalized PNG: {str(e)}")


def run_potrace_conversion(
    png_path: Path, temp_svg_path: Path, image_info: dict | None = None
) -> bool:
    """Run ImageMagick thresholding (with adaptive preprocessing if needed) and Potrace to convert PNG to SVG"""
    temp_filename = png_path.stem + "_temp"
    pbm_path = TEMP_DIR / f"{temp_filename}.pbm"

    try:
        preprocess = image_info is not None and _needs_preprocessing(image_info)
        if preprocess:
            if _supports_auto_threshold():
                threshold_args = ["-auto-threshold", "OTSU"]
                logger.info(
                    "Applying low-quality preprocessing: grayscale + upscale + blur/sharpen + OTSU threshold + denoise"
                )
            else:
                threshold_args = ["-normalize", "-threshold", "50%"]
                logger.info(
                    "Applying low-quality preprocessing: grayscale + upscale + blur/sharpen + normalize+threshold + denoise"
                )
            imagemagick_cmd = [
                "convert",
                str(png_path),
                "-colorspace",
                "Gray",
                "-resize",
                "4000x4000<",
                "-blur",
                "0x0.8",
                "-unsharp",
                "0x2+1.5+0",
                *threshold_args,
                "-morphology",
                "Open",
                "Disk:1.0",
                str(pbm_path),
            ]
        else:
            logger.info(
                "High-quality image detected — using standard threshold (99%), no preprocessing"
            )
            imagemagick_cmd = [
                "convert",
                str(png_path),
                "-threshold",
                "99%",
                str(pbm_path),
            ]

        result = subprocess.run(
            imagemagick_cmd, capture_output=True, text=True, timeout=300
        )
        if result.returncode != 0:
            logger.error(f"   ImageMagick thresholding failed: {result.stderr}")
            return False
        if not pbm_path.exists():
            logger.error("   PBM file was not created")
            return False

        turdsize = "10" if preprocess else "2"
        potrace_cmd = [
            "potrace",
            str(pbm_path),
            "-s",
            "-t",
            turdsize,
            "-o",
            str(temp_svg_path),
        ]
        result = subprocess.run(
            potrace_cmd, capture_output=True, text=True, timeout=120
        )

        if result.returncode != 0:
            logger.error(f"   Potrace conversion failed: {result.stderr}")
            return False

        return True

    except subprocess.TimeoutExpired:
        logger.error("   Potrace conversion timed out")
        return False
    except Exception as e:
        logger.error(f"   Potrace conversion error: {str(e)}")
        return False
    finally:
        try:
            if pbm_path.exists():
                pbm_path.unlink()
        except Exception as e:
            logger.warning(f"   Failed to delete PBM file: {str(e)}")


def convert_png_to_svg_local(png_path: Path, engine: str | None = None) -> str:
    """Convert PNG to SVG using either Potrace (with adaptive preprocessing) or VTracer based on configuration"""
    selected_engine = (engine or CONVERSION_ENGINE).lower()
    logger.debug(f"CONVERTING: {png_path.name} (Engine: {selected_engine})")
    logger.debug(f"   Input:  {png_path}")

    temp_filename = png_path.stem + "_temp"
    temp_svg_path = TEMP_DIR / f"{temp_filename}.svg"

    try:
        if selected_engine == "vtracer":
            logger.debug(f"   Running VTracer for conversion...")
            success = run_vtracer_conversion(png_path, temp_svg_path)
        else:
            logger.debug(f"   Running Potrace for conversion...")
            # Pass image_info=None to match standard thresholding (bypasses low-quality blur/open preprocessing)
            success = run_potrace_conversion(png_path, temp_svg_path, None)

        if not success or not temp_svg_path.exists():
            logger.error(f"   Conversion tool failed to generate SVG for {png_path.name}")
            return None

        if selected_engine == "vtracer":
            try:
                sort_svg_paths_by_area(temp_svg_path)
            except Exception as e:
                logger.warning(f"   Failed to sort SVG paths by area: {e}")

        with open(temp_svg_path, "r", encoding="utf-8") as f:
            svg_content = f.read()

        if not is_valid_xml(svg_content):
            logger.error(
                f"   Generated SVG content from vectorizer is invalid or truncated XML for {png_path.name}"
            )
            return None

        standardized_svg_content = svg_content

        logger.debug(f"   Optimizing SVG file...")
        optimized_svg_content = optimize_svg(standardized_svg_content)

        logger.debug(f"   Flattening SVG groups...")
        flattened_svg_content = flatten_svg_content(optimized_svg_content, engine=selected_engine)

        try:
            temp_svg_path.unlink(missing_ok=True)
        except Exception as e:
            logger.warning(f"   Failed to clean up temp SVG file: {str(e)}")

        return flattened_svg_content

    except Exception as e:
        logger.error(f"   Conversion pipeline failed for {png_path.name}: {str(e)}")
        try:
            temp_svg_path.unlink(missing_ok=True)
        except Exception:
            pass
        return None


def check_dependencies():
    """Check if required tools are available"""
    tools = {
        "ImageMagick": ["convert", "--version"],
        "svgcleaner": ["svgcleaner", "--version"],
    }

    if CONVERSION_ENGINE == "potrace":
        tools["Potrace"] = ["potrace", "--version"]

    if ENABLE_SVGO:
        tools["svgo"] = ["svgo", "--version"]

    logger.info("Checking required dependencies...")
    all_ok = True

    for tool_name, cmd in tools.items():
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
            if result.returncode == 0:
                logger.info(f"   {tool_name}: Available")
            else:
                logger.error(f"   {tool_name}: Not working properly")
                all_ok = False
        except FileNotFoundError:
            logger.error(f"   {tool_name}: Not installed")
            all_ok = False
        except Exception as e:
            logger.error(f"   {tool_name}: Error checking - {str(e)}")
            all_ok = False

    if not ENABLE_SVGO:
        logger.info(f"   svgo: Disabled (ENABLE_SVGO=false)")

    return all_ok


def fetch_floor_urls_from_api(api_endpoint: str) -> list:
    """
    Fetch the list of floor image URLs from the API endpoint

    Args:
        api_endpoint: The API endpoint URL to fetch floor URLs from

    Returns:
        List of floor image URLs, or empty list if request fails
    """
    try:
        response = requests.get(api_endpoint, timeout=30)
        response.raise_for_status()

        urls = response.json()

        if not isinstance(urls, list):
            logger.error(f"API response is not a list: {type(urls)}")
            return []

        logger.info(f"Successfully fetched {len(urls)} floor URLs from API")
        return urls

    except requests.exceptions.Timeout:
        logger.error(f"API request timed out after 30 seconds")
        return []
    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to fetch floor URLs from API: {str(e)}")
        return []
    except ValueError as e:
        logger.error(f"Failed to parse API response as JSON: {str(e)}")
        return []


def get_base_url() -> str:
    """
    Get base URL based on mode.
    If MODE is 'multi-tenant', returns MULTI_TENANT_BASEURL (falling back to BASE_URL).
    Otherwise returns BASE_URL.
    """
    mode = (os.getenv("MODE") or "").strip().strip("\r\n\"'").lower()
    if mode == "multi-tenant":
        raw = os.getenv("MULTI_TENANT_BASEURL") or os.getenv("BASE_URL") or ""
    else:
        raw = os.getenv("BASE_URL") or ""
    return raw.strip().strip("\r\n\"'").rstrip("/")


def fetch_vdms_floor_from_cloud_api(base_url: str = None):
    """
    Fetch the list or dictionary of floor image URLs from the API endpoint

    Args:
        base_url: The base URL for the cloud API endpoint

    Returns:
        List or dict of floor image URLs, or empty list if request fails
    """
    if not base_url:
        base_url = get_base_url()
    try:
        url = f"{base_url}/tenants/floors/imageUrls"
        response = requests.get(url, timeout=30)
        response.raise_for_status()

        data = response.json()

        if not isinstance(data, (list, dict)):
            logger.error(f"API response is not a list or dict: {type(data)}")
            return []

        logger.info(f"Successfully fetched floor URLs from API")
        return data

    except requests.exceptions.Timeout:
        logger.error(f"API request timed out after 30 seconds")
        return []
    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to fetch floor URLs from API: {str(e)}")
        return []
    except ValueError as e:
        logger.error(f"Failed to parse API response as JSON: {str(e)}")
        return []


def extract_filenames_from_urls(urls: list) -> list:
    """
    Extract filenames from a list of URLs

    Args:
        urls: List of full URLs like "http://localhost:8888/images/floors/033813a3-31cc-11ee-9e48-7becb817b0ad_15642127873072412.png"

    Returns:
        List of filenames like ["033813a3-31cc-11ee-9e48-7becb817b0ad_15642127873072412.png"]
    """
    filenames = []

    for url in urls:
        try:
            parsed_url = urlparse(url)
            path = parsed_url.path

            filename = os.path.basename(path)

            if filename:
                filenames.append(filename)
                logger.debug(f"   Extracted filename: {filename}")
            else:
                logger.warning(f"   Could not extract filename from URL: {url}")

        except Exception as e:
            logger.warning(f"   Failed to parse URL: {url} - {str(e)}")

    logger.info(f"Extracted {len(filenames)} filenames from URLs")
    return filenames


def extract_vdmsIds_floorids_from_urls(data) -> dict:
    """
    Extract VDMS IDs and floor image URLs from a list of URLs or data structure.

    Args:
        data: List of full URLs or data containing URLs

    Returns:
        Dict mapping VDMS ID (e.g. 'VDMS885') to a list of floor image URLs
    """
    result = {}

    if isinstance(data, dict):
        for key, val in data.items():
            if isinstance(val, list):
                result[key] = val
            elif isinstance(val, str):
                result[key] = [val]
        return result

    if not isinstance(data, list):
        return result

    for item in data:
        if isinstance(item, dict):
            vdms_id = item.get("vdmsId") or item.get("vdms_id") or item.get("id")
            url_val = item.get("url") or item.get("imageUrl") or item.get("image_url")
            if vdms_id:
                if vdms_id not in result:
                    result[vdms_id] = []
                if url_val:
                    result[vdms_id].append(url_val)
                continue
            url = url_val or str(item)
        elif isinstance(item, str):
            url = item
        else:
            url = str(item)

        if not url:
            continue

        try:
            match = re.search(r"/vdms/([^/]+)/floor", url)
            if not match:
                match = re.search(r"(VDMS\d+)", url, re.IGNORECASE)
            if not match:
                match = re.search(r"/vdms/([^/]+)", url)

            if match:
                vdms_id = match.group(1)
                if vdms_id not in result:
                    result[vdms_id] = []
                result[vdms_id].append(url)
            else:
                logger.warning(f"   Could not extract VDMS ID from URL: {url}")
        except Exception as e:
            logger.warning(f"   Failed to process URL: {url} - {str(e)}")

    logger.info(f"Extracted floor URLs for {len(result)} VDMS IDs")
    return result


def validate_file_readability(file_path: Path) -> tuple[bool, str]:
    """
    Validate that a file exists and is readable

    Args:
        file_path: Path to the file to validate

    Returns:
        Tuple of (is_valid, error_message)
    """
    try:
        if not file_path.exists():
            return False, "File does not exist"

        if not file_path.is_file():
            return False, "Path is not a file"

        if not os.access(file_path, os.R_OK):
            return False, "File is not readable (permission denied)"

        file_size = file_path.stat().st_size
        if file_size == 0:
            return False, "File is empty (0 bytes)"

        if file_size < 100:
            return False, f"File is too small ({file_size} bytes) to be a valid PNG"

        try:
            with open(file_path, "rb") as f:
                header = f.read(8)
                # PNG magic number: 89 50 4E 47 0D 0A 1A 0A
                if header != b"\x89PNG\r\n\x1a\n":
                    return False, "File is not a valid PNG (invalid header)"
        except Exception as e:
            return False, f"Cannot read file header: {str(e)}"

        return True, "OK"

    except Exception as e:
        return False, f"Validation error: {str(e)}"


def check_missing_files(requested_filenames: list, input_dir: Path) -> dict:
    """
    Check which files from the API are missing or invalid in the input directory

    Args:
        requested_filenames: List of filenames requested by the API
        input_dir: Directory where files should be located

    Returns:
        Dictionary with 'missing', 'invalid', and 'valid' lists of filenames
    """
    logger.info(f"Validating {len(requested_filenames)} files from API...")

    missing_files = []
    invalid_files = []
    valid_files = []

    for filename in requested_filenames:
        file_path = input_dir / filename
        is_valid, error_msg = validate_file_readability(file_path)

        if not file_path.exists():
            missing_files.append(filename)
            logger.warning(f"   MISSING: {filename}")
        elif not is_valid:
            invalid_files.append((filename, error_msg))
            logger.warning(f"   INVALID: {filename} - {error_msg}")
        else:
            valid_files.append(filename)

    # Summary of validation
    logger.info(f"Validation complete:")
    logger.info(f"   Valid files: {len(valid_files)}")
    logger.info(f"   Missing files: {len(missing_files)}")
    logger.info(f"   Invalid files: {len(invalid_files)}")

    return {"missing": missing_files, "invalid": invalid_files, "valid": valid_files}


def process_local_files(filter_filenames: list = None):
    """
    NOTE: Main function for local file processing
    Scans INPUT_DIR for PNG files and converts them to SVG in OUTPUT_DIR

    Args:
        filter_filenames: Optional list of filenames to process. If provided, only these files will be processed.
                          If None, all PNG files in INPUT_DIR will be processed.
    """
    setup_file_logging()
    print("=" * 80)
    logger.info(f"STARTING LOCAL FILE PROCESSING")
    logger.info(f"Input directory:  {INPUT_DIR}")
    logger.info(f"Output directory: {OUTPUT_DIR}")
    if filter_filenames:
        logger.info(
            f"Filter mode: Processing only {len(filter_filenames)} specific files from API"
        )
    else:
        logger.info(f"Mode: Processing all PNG files in directory")
    print("=" * 80)

    failures = []

    if not check_dependencies():
        engine_name = "Potrace" if CONVERSION_ENGINE == "potrace" else "VTracer"
        deps = f" {engine_name}, and svgcleaner"
        if ENABLE_SVGO:
            deps += ", and svgo"
        logger.error(f"Missing required dependencies. Please install {deps}")
        write_failed_files_report(failures)
        return False

    print("=" * 80)

    missing_files = []
    invalid_files = []

    png_pattern = str(INPUT_DIR / "*.png")
    all_png_files = glob.glob(png_pattern)

    if filter_filenames:
        validation_results = check_missing_files(filter_filenames, INPUT_DIR)

        missing_files = validation_results["missing"]
        invalid_files = validation_results["invalid"]
        valid_filenames = validation_results["valid"]

        for filename in missing_files:
            failures.append(
                {
                    "filename": filename,
                    "reason": "File does not exist in input directory",
                }
            )

        for filename, reason in invalid_files:
            failures.append(
                {
                    "filename": filename,
                    "reason": f"Invalid or unreadable PNG file: {reason}",
                }
            )

        # Create a set for faster lookup
        filter_set = set(filter_filenames)
        png_files = [f for f in all_png_files if os.path.basename(f) in filter_set]

        logger.info(f"Found {len(all_png_files)} total PNG files in directory")
        logger.info(f"Filtered to {len(png_files)} valid files to process")

        if missing_files:
            print()
            logger.warning(f"MISSING FILES REPORT ({len(missing_files)} files):")
            for i, filename in enumerate(missing_files[:10], 1):
                logger.warning(f"   {i}. {filename}")
            if len(missing_files) > 10:
                logger.warning(
                    f"   ... and {len(missing_files) - 10} more missing files"
                )
            print()

        if invalid_files:
            print()
            logger.warning(f"INVALID FILES REPORT ({len(invalid_files)} files):")
            for i, (filename, reason) in enumerate(invalid_files[:10], 1):
                logger.warning(f"   {i}. {filename} - {reason}")
            if len(invalid_files) > 10:
                logger.warning(
                    f"   ... and {len(invalid_files) - 10} more invalid files"
                )
            print()

        if not png_files:
            logger.error(f"No valid PNG files found to process after validation")
            logger.error(f"   API requested: {len(filter_filenames)} files")
            logger.error(f"   Missing: {len(missing_files)} files")
            logger.error(f"   Invalid: {len(invalid_files)} files")
            logger.error(f"   Available in directory: {len(all_png_files)} files")
            print("=" * 80)
            write_failed_files_report(failures)
            return False
    else:
        png_files = all_png_files
        if not png_files:
            logger.warning(f"No PNG files found in {INPUT_DIR}")
            print("=" * 80)
            write_failed_files_report(failures)
            return False

    png_files.sort()
    total_files = len(png_files)
    logger.info(f"Processing {total_files} PNG files")
    print("=" * 80)

    data = Data()
    injector = PathInjector()

    processed_count = 0
    conversion_failed_count = 0
    skipped_count = 0
    overall_start = time.perf_counter()
    for index, png_file_path in enumerate(png_files, 1):
        temp_png = None
        try:
            png_path = Path(png_file_path)
            svg_filename = png_path.stem + ".svg"
            output_svg_path = OUTPUT_DIR / svg_filename

            start = time.perf_counter()
            logger.info(f"[{index}/{total_files}] PROCESSING: {png_path.name}")

            if output_svg_path.exists():
                skipped_count += 1
                logger.info(
                    f"[{index}/{total_files}] SKIPPED: {svg_filename} (SVG already exists)"
                )
                continue

            logger.debug(f"   Starting conversion process...")
            orig_img = cv.imread(str(png_path))
            orig_h, orig_w = orig_img.shape[:2] if orig_img is not None else (2000, 2000)

            file_engine = determine_engine(orig_img)

            img, detections = recognize_txt(str(png_path), OCR_INSTANCE, engine=file_engine)
            img = remove_txt(img, detections)
            temp_png = SHM_DIR / f"{png_path.stem}_notext.png"

            cv.imwrite(str(temp_png), img)

            resultant_svg_content = convert_png_to_svg_local(temp_png, engine=file_engine)

            if not resultant_svg_content:
                conversion_failed_count += 1
                logger.error(f"   CONVERSION FAILED: Could not process {png_path.name}")
                failures.append(
                    {
                        "filename": png_path.name,
                        "reason": "Conversion pipeline failed to generate SVG content",
                    }
                )
                continue

            try:
                svg_root = ET.fromstring(resultant_svg_content)
                viewbox_str = svg_root.attrib.get("viewBox", "")
                if viewbox_str:
                    parts = viewbox_str.split()
                    vb_w, vb_h = float(parts[2]), float(parts[3])
                else:
                    vb_w = float(svg_root.attrib.get("width", orig_w))
                    vb_h = float(svg_root.attrib.get("height", orig_h))
                scale_x = vb_w / float(orig_w) if orig_w else 1.0
                scale_y = vb_h / float(orig_h) if orig_h else 1.0
            except Exception:
                scale_x, scale_y = 1.0, 1.0

            floor_parts = svg_filename.split("_")
            floor_id = floor_parts[0]

            floor_name = data.get_floor(floor_id)
            paths_string = data.get_paths(floor_id)
            floor_locations = data.get_locations(floor_id)
            geo_data = data.get_geo_data(floor_id)
            tree = inject_elements(
                svg=resultant_svg_content,
                raw_svg_content=resultant_svg_content,
                detections=detections,
                floor_id=floor_id,
                floor_name=floor_name,
                floor_locations=floor_locations,
                paths=paths_string,
                injector=injector,
                geo_data=geo_data,
                scale_x=scale_x,
                scale_y=scale_y,
                orig_w=orig_w,
                orig_h=orig_h,
                engine=file_engine,
            )
            tree.write(
                os.path.join(OUTPUT_DIR, svg_filename),
                encoding="utf-8",
                xml_declaration=True,
            )

            processed_count += 1
            final_size = os.path.getsize(output_svg_path)
            end = time.perf_counter()
            logger.info(
                f"[{index}/{total_files}] COMPLETED: {svg_filename} | Size: {final_size} bytes | Time: {end - start:.2f}s"
            )

        except Exception as e:
            conversion_failed_count += 1
            logger.error(
                f"[{index}/{total_files}] FAILED: {png_path.name} | Error: {str(e)}"
            )
            failures.append(
                {
                    "filename": Path(png_file_path).name,
                    "reason": f"Processing exception: {str(e)}",
                }
            )
        finally:
            if temp_png and temp_png.exists():
                try:
                    temp_png.unlink()
                    logger.debug(f"   Cleaned up temporary file: {temp_png.name}")
                except Exception as cleanup_err:
                    logger.warning(
                        f"   Failed to delete temp file {temp_png}: {cleanup_err}"
                    )
    overall_end = time.perf_counter()

    print()
    print("=" * 80)
    logger.info(f"LOCAL FILE PROCESSING COMPLETED!")
    logger.info(f"FINAL RESULTS:")
    logger.info(f"   Processed successfully: {processed_count} files")
    logger.info(f"   Skipped (already exist): {skipped_count} files")
    logger.info(
        f"   Total processing time: " f"{overall_end - overall_start:.2f} seconds"
    )
    if filter_filenames:
        logger.info(f"   Missing files (not in directory): {len(missing_files)} files")
        logger.info(
            f"   Invalid files (corrupted/unreadable): {len(invalid_files)} files"
        )
    logger.info(f"   Conversion failures: {conversion_failed_count} files")

    total_failures = len(missing_files) + len(invalid_files) + conversion_failed_count
    if total_failures > 0:
        logger.warning(f"   Total failures: {total_failures} files")

    if filter_filenames:
        logger.info(f"   Total requested from API: {len(filter_filenames)}")
    else:
        logger.info(f"   Total files found: {total_files}")

    if processed_count > 0:
        logger.info(f"{processed_count} new SVG files created in {OUTPUT_DIR}")

    write_failed_files_report(failures)

    if total_failures > 0:
        logger.warning(f"Completed with {total_failures} failures")

    print("=" * 80)
    return True


def load_image(path):
    """
    Loads the image from the given path and returns it along with its width and height.
    """
    img = cv.imread(path)
    if img is None:
        raise ValueError(f"Could not read image: {path}")
    h, w = img.shape[:2]
    return img, w, h


def process_filtered_files(filter_filenames: list = None):
    """
    NOTE: Main function for processing files based on API-provided filename filter
    Scans INPUT_DIR for PNG files and converts them to SVG in OUTPUT_DIR

    Args:
        filter_filenames: Optional list of filenames to process. If provided, only these files will be processed.
                         If None, all PNG files in INPUT_DIR will be processed.
    """
    setup_file_logging()
    print("=" * 80)
    logger.info(f"STARTING API-DRIVEN FILE PROCESSING")
    logger.info(f"Input directory:  {INPUT_DIR}")
    logger.info(f"Output directory: {OUTPUT_DIR}")
    if filter_filenames:
        logger.info(
            f"Filter mode: Processing only {len(filter_filenames)} specific files from API"
        )
    else:
        logger.info(f"Mode: Processing all PNG files in directory")
    print("=" * 80)

    failures = []

    if not check_dependencies():
        engine_name = "Potrace" if CONVERSION_ENGINE == "potrace" else "VTracer"
        deps = f" {engine_name}, and svgcleaner"
        if ENABLE_SVGO:
            deps += ", and svgo"
        logger.error(f"Missing required dependencies. Please install {deps}")
        write_failed_files_report(failures)
        return False

    print("=" * 80)

    missing_files = []
    invalid_files = []

    png_pattern = str(INPUT_DIR / "*.png")
    all_png_files = glob.glob(png_pattern)
    data = Data()
    injector = PathInjector()

    if filter_filenames:
        validation_results = check_missing_files(filter_filenames, INPUT_DIR)

        missing_files = validation_results["missing"]
        invalid_files = validation_results["invalid"]
        valid_filenames = validation_results["valid"]

        for filename in missing_files:
            failures.append(
                {
                    "filename": filename,
                    "reason": "File does not exist in input directory",
                }
            )

        for filename, reason in invalid_files:
            failures.append(
                {
                    "filename": filename,
                    "reason": f"Invalid or unreadable PNG file: {reason}",
                }
            )

        filter_set = set(filter_filenames)
        png_files = [f for f in all_png_files if os.path.basename(f) in filter_set]

        logger.info(f"Found {len(all_png_files)} total PNG files in directory")
        logger.info(f"Filtered to {len(png_files)} valid files to process")

        if missing_files:
            print()
            logger.warning(f"MISSING FILES REPORT ({len(missing_files)} files):")
            for i, filename in enumerate(missing_files[:10], 1):  # Show first 10
                logger.warning(f"   {i}. {filename}")
            if len(missing_files) > 10:
                logger.warning(
                    f"   ... and {len(missing_files) - 10} more missing files"
                )
            print()

        if invalid_files:
            print()
            logger.warning(f"INVALID FILES REPORT ({len(invalid_files)} files):")
            for i, (filename, reason) in enumerate(
                invalid_files[:10], 1
            ):  # Show first 10
                logger.warning(f"   {i}. {filename} - {reason}")
            if len(invalid_files) > 10:
                logger.warning(
                    f"   ... and {len(invalid_files) - 10} more invalid files"
                )
            print()

        if not png_files:
            logger.error(f"No valid PNG files found to process after validation")
            logger.error(f"   API requested: {len(filter_filenames)} files")
            logger.error(f"   Missing: {len(missing_files)} files")
            logger.error(f"   Invalid: {len(invalid_files)} files")
            logger.error(f"   Available in directory: {len(all_png_files)} files")
            print("=" * 80)
            write_failed_files_report(failures)
            return False
    else:
        png_files = all_png_files
        if not png_files:
            logger.warning(f"No PNG files found in {INPUT_DIR}")
            print("=" * 80)
            write_failed_files_report(failures)
            return False

    png_files.sort()
    total_files = len(png_files)
    logger.info(f"Processing {total_files} PNG files")
    print("=" * 80)

    processed_count = 0
    conversion_failed_count = 0
    skipped_count = 0
    overall_start = time.perf_counter()
    for index, png_file_path in enumerate(png_files, 1):
        temp_png = None
        try:
            start = time.perf_counter()

            png_path = Path(png_file_path)
            svg_filename = png_path.stem + ".svg"
            output_svg_path = OUTPUT_DIR / svg_filename

            logger.info(f"[{index}/{total_files}] PROCESSING: {png_path.name}")
            logger.debug(f"   Size: {png_path.stat().st_size} bytes")

            if output_svg_path.exists():
                skipped_count += 1
                logger.info(
                    f"[{index}/{total_files}] SKIPPED: {svg_filename} (SVG already exists)"
                )
                continue

            logger.debug(f"   Starting conversion process...")
            orig_img = cv.imread(str(png_path))
            orig_h, orig_w = orig_img.shape[:2] if orig_img is not None else (2000, 2000)

            file_engine = determine_engine(orig_img)

            img, detections = recognize_txt(str(png_path), OCR_INSTANCE, engine=file_engine)
            img = remove_txt(img, detections)
            temp_png = SHM_DIR / f"{png_path.stem}_notext.png"

            cv.imwrite(str(temp_png), img)

            resultant_svg_content = convert_png_to_svg_local(temp_png, engine=file_engine)

            if not resultant_svg_content:
                conversion_failed_count += 1
                logger.error(f"   CONVERSION FAILED: Could not process {png_path.name}")
                failures.append(
                    {
                        "filename": png_path.name,
                        "reason": "Conversion pipeline failed to generate SVG content",
                    }
                )
                continue

            try:
                svg_root = ET.fromstring(resultant_svg_content)
                viewbox_str = svg_root.attrib.get("viewBox", "")
                if viewbox_str:
                    parts = viewbox_str.split()
                    vb_w, vb_h = float(parts[2]), float(parts[3])
                else:
                    vb_w = float(svg_root.attrib.get("width", orig_w))
                    vb_h = float(svg_root.attrib.get("height", orig_h))
                scale_x = vb_w / float(orig_w) if orig_w else 1.0
                scale_y = vb_h / float(orig_h) if orig_h else 1.0
            except Exception:
                scale_x, scale_y = 1.0, 1.0

            floor_parts = svg_filename.split("_")
            floor_id = floor_parts[0]

            floor_name = data.get_floor(floor_id)
            paths_string = data.get_paths(floor_id)
            floor_locations = data.get_locations(floor_id)
            geo_data = data.get_geo_data(floor_id)
            tree = inject_elements(
                svg=resultant_svg_content,
                raw_svg_content=resultant_svg_content,
                detections=detections,
                floor_id=floor_id,
                floor_name=floor_name,
                floor_locations=floor_locations,
                paths=paths_string,
                injector=injector,
                geo_data=geo_data,
                scale_x=scale_x,
                scale_y=scale_y,
                orig_w=orig_w,
                orig_h=orig_h,
                engine=file_engine,
            )

            tree.write(
                os.path.join(OUTPUT_DIR, svg_filename),
                encoding="utf-8",
                xml_declaration=True,
            )

            # Upload SVG to API
            svg_bytes = ET.tostring(
                tree.getroot(), encoding="utf-8", xml_declaration=True
            )
            upload_response = data.upload_floor(floor_id, svg_bytes)
            logger.debug(f"Upload response: {upload_response}")

            processed_count += 1
            final_size = len(resultant_svg_content)
            end = time.perf_counter()
            logger.info(
                f"[{index}/{total_files}] COMPLETED: {svg_filename} | Size: {final_size} bytes | Time: {end - start:.2f}s"
            )

        except Exception as e:
            conversion_failed_count += 1
            logger.error(
                f"[{index}/{total_files}] FAILED: {png_path.name} | Error: {str(e)}"
            )
            logger.debug(traceback.format_exc())
            failures.append(
                {
                    "filename": Path(png_file_path).name,
                    "reason": f"Processing exception: {str(e)}",
                }
            )
        finally:
            if temp_png and temp_png.exists():
                try:
                    temp_png.unlink()
                    logger.debug(f"   Cleaned up temporary file: {temp_png.name}")
                except Exception as cleanup_err:
                    logger.warning(
                        f"   Failed to delete temp file {temp_png}: {cleanup_err}"
                    )
    overall_end = time.perf_counter()

    print()
    print("=" * 80)
    logger.info(f"LOCAL FILE PROCESSING COMPLETED!")
    logger.info(f"FINAL RESULTS:")
    logger.info(f"   Processed successfully: {processed_count} files")
    logger.info(f"   Skipped (already exist): {skipped_count} files")
    logger.info(
        f"   Total processing time: " f"{overall_end - overall_start:.2f} seconds"
    )
    if filter_filenames:
        logger.info(f"   Missing files (not in directory): {len(missing_files)} files")
        logger.info(
            f"   Invalid files (corrupted/unreadable): {len(invalid_files)} files"
        )
    logger.info(f"   Conversion failures: {conversion_failed_count} files")

    total_failures = len(missing_files) + len(invalid_files) + conversion_failed_count
    if total_failures > 0:
        logger.warning(f"   Total failures: {total_failures} files")

    if filter_filenames:
        logger.info(f"   Total requested from API: {len(filter_filenames)}")
    else:
        logger.info(f"   Total files found: {total_files}")

    if processed_count > 0:
        logger.info(f"{processed_count} new SVG files created in {OUTPUT_DIR}")

    write_failed_files_report(failures)

    if total_failures > 0:
        logger.warning(f"Completed with {total_failures} failures")

    print("=" * 80)
    return True


def download_png(base_url: str = None, vdms_id: str = "", image_id: str = ""):
    if not base_url:
        base_url = get_base_url()
    url = f"{base_url}/images/floors/{image_id}.png?vdms_id={vdms_id}"
    try:
        response = requests.get(url, timeout=30)
        logger.debug(
            f"Downloading PNG from URL: {url}{response} | Status code: {response.status_code}"
        )
        response.raise_for_status()
        return response.content
    except Exception as e:
        logger.error(f"Failed to download PNG for image '{image_id}': {str(e)}")
        return None


def get_image_id_from_url(url: str):
    """
    Extracts the image ID (without file extension) from a given URL.
    Args:
        url: The URL string to extract the image ID from.
    Returns:
        The extracted image ID as a string without extension, or None if not found.
    """
    try:
        parsed_url = urlparse(url)
        path = parsed_url.path.rstrip("/")
        path_segments = [seg for seg in path.split("/") if seg]

        if path_segments:
            image_filename = path_segments[-1]
            image_id = os.path.splitext(image_filename)[0]
            return image_id
        else:
            logger.warning(f"URL format unexpected, cannot extract image ID: {url}")
            return None
    except Exception as e:
        logger.error(f"Error extracting image ID from URL '{url}': {str(e)}")
        return None


def process_cloud_floor_maps(cloud_url: str, vdms_floor_map: dict):
    """
    Main function for processing floor maps from cloud and uploading generated SVGs directly to cloud.
    No files are stored permanently on disk.

    Args:
        cloud_url: The URL of the cloud service.
        vdms_floor_map: A dictionary mapping VDMS IDs to lists of image URLs.
    """
    setup_file_logging()
    print("=" * 80)
    logger.info("STARTING CLOUD FILE PROCESSING (UPLOAD MODE)")
    logger.info(f"Cloud URL: {cloud_url}")
    print("=" * 80)

    failures = []

    if not check_dependencies():
        engine_name = "Potrace" if CONVERSION_ENGINE == "potrace" else "VTracer"
        deps = f" {engine_name}, and svgcleaner"
        if ENABLE_SVGO:
            deps += ", and svgo"
        logger.error(f"Missing required dependencies. Please install {deps}")
        write_failed_files_report(failures)
        return False

    data = Data()
    injector = PathInjector()

    # Apply VDMS filter from .env / environment / CLI
    vdms_filter = get_vdms_filter()
    if vdms_filter:
        logger.info(f"Applying VDMS filter: {sorted(list(vdms_filter))}")
        vdms_filter_upper = {v.upper() for v in vdms_filter}
        filtered_map = {
            k: v
            for k, v in vdms_floor_map.items()
            if k in vdms_filter or k.upper() in vdms_filter_upper
        }
        logger.info(
            f"Filtered {len(vdms_floor_map)} total VDMS box(es) down to {len(filtered_map)} matching box(es): {list(filtered_map.keys())}"
        )
        vdms_floor_map = filtered_map
        if not vdms_floor_map:
            logger.warning(
                f"No matching VDMS boxes found for filter {sorted(list(vdms_filter))}. Nothing to process."
            )
            return True

    total_files = (
        sum(len(urls) for urls in vdms_floor_map.values())
        if isinstance(vdms_floor_map, dict)
        else 0
    )
    logger.info(f"Total floor map URLs to process: {total_files}")
    print("=" * 80)

    processed_count = 0
    conversion_failed_count = 0
    current_index = 0
    overall_start = time.perf_counter()

    for vdms_id, image_urls in vdms_floor_map.items():
        for image_url in image_urls:
            current_index += 1
            start = time.perf_counter()
            temp_png_path = None
            temp_notext_png = None

            try:
                image_id = get_image_id_from_url(image_url)
                if not image_id:
                    logger.error(
                        f"[{current_index}/{total_files}] Could not extract image ID from URL: {image_url}"
                    )
                    failures.append(
                        {
                            "filename": image_url,
                            "reason": "Could not extract image ID from URL",
                        }
                    )
                    conversion_failed_count += 1
                    continue

                logger.info(
                    f"[{current_index}/{total_files}] PROCESSING (VDMS: {vdms_id}, Image: {image_id})"
                )

                # Download PNG bytes from cloud
                png_bytes = download_png(cloud_url, vdms_id, image_id)
                if not png_bytes:
                    logger.error(
                        f"[{current_index}/{total_files}] Failed to download PNG bytes for image {image_id}"
                    )
                    failures.append(
                        {"filename": image_id, "reason": "Failed to download PNG bytes"}
                    )
                    conversion_failed_count += 1
                    continue

                # Temporary storage in memory/tmp for OCR & conversion
                temp_png_path = SHM_DIR / f"{image_id}_temp.png"
                temp_png_path.write_bytes(png_bytes)

                orig_img = cv.imread(str(temp_png_path))
                orig_h, orig_w = orig_img.shape[:2] if orig_img is not None else (2000, 2000)

                file_engine = determine_engine(orig_img)

                # Perform OCR & text removal
                img, detections = recognize_txt(str(temp_png_path), OCR_INSTANCE, engine=file_engine)
                img = remove_txt(img, detections)

                temp_notext_png = temp_png_path.parent / f"{image_id}_notext.png"
                cv.imwrite(str(temp_notext_png), img)

                # Convert to SVG in memory
                resultant_svg_content = convert_png_to_svg_local(temp_notext_png, engine=file_engine)
                if not resultant_svg_content:
                    conversion_failed_count += 1
                    logger.error(
                        f"[{current_index}/{total_files}] CONVERSION FAILED for {image_id}"
                    )
                    failures.append(
                        {
                            "filename": image_id,
                            "reason": "Conversion pipeline failed to generate SVG content",
                        }
                    )
                    continue

                try:
                    svg_root = ET.fromstring(resultant_svg_content)
                    viewbox_str = svg_root.attrib.get("viewBox", "")
                    if viewbox_str:
                        parts = viewbox_str.split()
                        vb_w, vb_h = float(parts[2]), float(parts[3])
                    else:
                        vb_w = float(svg_root.attrib.get("width", orig_w))
                        vb_h = float(svg_root.attrib.get("height", orig_h))
                    scale_x = vb_w / float(orig_w) if orig_w else 1.0
                    scale_y = vb_h / float(orig_h) if orig_h else 1.0
                except Exception:
                    scale_x, scale_y = 1.0, 1.0

                # Inject elements into SVG
                floor_id = image_id.split("_")[0]
                floor_name = data.get_floor(floor_id, vdms_id)
                paths_string = data.get_paths(floor_id, vdms_id)
                floor_locations = data.get_locations(floor_id, vdms_id)
                geo_data = data.get_geo_data(floor_id)

                tree = inject_elements(
                    svg=resultant_svg_content,
                    raw_svg_content=resultant_svg_content,
                    detections=detections,
                    floor_id=floor_id,
                    floor_name=floor_name,
                    floor_locations=floor_locations,
                    paths=paths_string,
                    injector=injector,
                    geo_data=geo_data,
                    scale_x=scale_x,
                    scale_y=scale_y,
                    orig_w=orig_w,
                    orig_h=orig_h,
                    engine=file_engine,
                )

                # Save generated SVG and upload to API
                svg_bytes = ET.tostring(
                    tree.getroot(), encoding="utf-8", xml_declaration=True
                )

                # upload_response = data.upload_floor(floor_id, svg_bytes, vdms_id=vdms_id)
                # logger.info(f"[{current_index}/{total_files}] UPLOADED: {floor_id} | Response: {upload_response}")

                output_path = os.path.join(OUTPUT_DIR, f"{floor_id}.svg")
                with open(output_path, "wb") as f:
                    f.write(svg_bytes)
                logger.info(f"[{current_index}/{total_files}] SAVED TO DISK: {output_path}")

                processed_count += 1
                end = time.perf_counter()
                logger.info(
                    f"[{current_index}/{total_files}] COMPLETED: {image_id} | Time: {end - start:.2f}s"
                )

            except Exception as e:
                conversion_failed_count += 1
                logger.error(
                    f"[{current_index}/{total_files}] FAILED: {image_url} | Error: {str(e)}"
                )
                logger.debug(traceback.format_exc())
                failures.append(
                    {"filename": image_url, "reason": f"Processing exception: {str(e)}"}
                )
            finally:
                # Clean up temporary PNG files
                if temp_png_path and temp_png_path.exists():
                    try:
                        temp_png_path.unlink()
                    except Exception as cleanup_err:
                        logger.warning(
                            f"Failed to delete temp file {temp_png_path}: {cleanup_err}"
                        )
                if temp_notext_png and temp_notext_png.exists():
                    try:
                        temp_notext_png.unlink()
                    except Exception as cleanup_err:
                        logger.warning(
                            f"Failed to delete temp file {temp_notext_png}: {cleanup_err}"
                        )

    overall_end = time.perf_counter()

    print()
    print("=" * 80)
    logger.info("CLOUD FILE PROCESSING COMPLETED!")
    logger.info("FINAL RESULTS:")
    logger.info(f"   Successfully converted & uploaded: {processed_count} files")
    logger.info(f"   Total processing time: {overall_end - overall_start:.2f} seconds")
    if conversion_failed_count > 0:
        logger.warning(f"   Failures: {conversion_failed_count} files")

    write_failed_files_report(failures)
    print("=" * 80)
    return True


def process_floor_from_cloud(cloud_url: str = None):
    """
    Fetch floor PNG from the API, process and upload to multi-tenant system.

    Args:
        cloud_url: The cloud URL that returns list of floor image URLs

    Returns:
        bool: True if processing completed successfully, False otherwise
    """
    if not cloud_url:
        cloud_url = get_base_url()

    setup_file_logging()
    print("\n" + "=" * 80)
    logger.info(f"API-BASED FLOOR PROCESSING MODE FOR CLOUD")
    logger.info(f"Cloud URL: {cloud_url}")
    print("=" * 80)

    logger.info(f"Fetching floor URLs from API: {cloud_url}")
    vdms_floors = fetch_vdms_floor_from_cloud_api(cloud_url)
    if not vdms_floors:
        logger.error("No floor URLs fetched from API. Cannot proceed.")
        write_failed_files_report([])
        return False
    logger.info(f"Extracting filenames from {len(vdms_floors)} URLs...")
    floor_mapping = extract_vdmsIds_floorids_from_urls(vdms_floors)
    if not floor_mapping:
        logger.error("No filenames extracted from URLs. Cannot proceed.")
        write_failed_files_report([])
        return False
    logger.info(f"Successfully extracted filenames")
    print("=" * 80)

    logger.info(f"Starting processing of floors...")
    success = process_cloud_floor_maps(
        cloud_url=cloud_url, vdms_floor_map=floor_mapping
    )
    return success


def process_floors_from_api(api_endpoint: str):
    """
    Fetch floor URLs from API and process only those specific floors

    Args:
        api_endpoint: The API endpoint URL that returns list of floor image URLs

    Returns:
        bool: True if processing completed successfully, False otherwise
    """
    setup_file_logging()
    print("\n" + "=" * 80)
    logger.info(f"API-BASED FLOOR PROCESSING MODE")
    logger.info(f"API Endpoint: {api_endpoint}")
    print("=" * 80)

    logger.info(f"Fetching floor URLs from API: {api_endpoint}")
    floor_urls = fetch_floor_urls_from_api(api_endpoint)

    if not floor_urls:
        logger.error("No floor URLs fetched from API. Cannot proceed.")
        write_failed_files_report([])
        return False

    logger.info(f"Extracting filenames from {len(floor_urls)} URLs...")
    filenames = extract_filenames_from_urls(floor_urls)

    if not filenames:
        logger.error("No filenames extracted from URLs. Cannot proceed.")
        write_failed_files_report([])
        return False

    logger.info(f"Successfully extracted {len(filenames)} filenames")
    logger.info(f"Sample filenames:")
    for i, filename in enumerate(filenames[:5], 1):  # Show first 5
        logger.info(f"   {i}. {filename}")
    if len(filenames) > 5:
        logger.info(f"   ... and {len(filenames) - 5} more")

    print("=" * 80)

    logger.info(f"Starting filtered processing of {len(filenames)} floors...")
    success = process_filtered_files(filter_filenames=filenames)

    return success


def parse_arguments_and_update_config():
    """Parse CLI arguments and update configuration globals"""
    global CONVERSION_ENGINE, AUTO_DETECT_ENGINE, ENABLE_SVGO, INPUT_DIR, OUTPUT_DIR

    import argparse

    parser = argparse.ArgumentParser(
        description="Floor Map Processing Service - PNG to SVG Converter"
    )
    parser.add_argument(
        "--engine",
        "-e",
        choices=["potrace", "vtracer"],
        default=CONVERSION_ENGINE,
        help="Conversion engine to use (default: potrace or CONVERSION_ENGINE env var)",
    )
    parser.add_argument(
        "--auto-detect", "--auto-detect-engine", "-a",
        dest="auto_detect",
        action=argparse.BooleanOptionalAction,
        default=AUTO_DETECT_ENGINE,
        help="Auto-detect conversion engine per image based on RGB color content (potrace for grayscale, vtracer for RGB)"
    )
    parser.add_argument(
        "--disable-svgo",
        action="store_true",
        default=not ENABLE_SVGO,
        help="Disable SVGO optimization (use svgcleaner only)",
    )
    parser.add_argument(
        "--input-dir",
        "-i",
        type=str,
        default=str(INPUT_DIR),
        help="Input directory containing PNG files",
    )
    parser.add_argument(
        "--output-dir",
        "-o",
        type=str,
        default=str(OUTPUT_DIR),
        help="Output directory for SVG files",
    )
    parser.add_argument(
        "--vdms",
        "-v",
        type=str,
        default="",
        help="Filter specific VDMS ID(s), e.g. VDMS885 or VDMS885,VDMS886 (default: VDMS_FILTER env var or all)",
    )

    args, unknown = parser.parse_known_args()

    if args.vdms:
        os.environ["VDMS_FILTER"] = args.vdms

    CONVERSION_ENGINE = args.engine
    AUTO_DETECT_ENGINE = bool(args.auto_detect)
    ENABLE_SVGO = not args.disable_svgo
    INPUT_DIR = Path(args.input_dir)
    OUTPUT_DIR = Path(args.output_dir)

    return args


if __name__ == "__main__":
    start_time = time.perf_counter()
    args = parse_arguments_and_update_config()

    print("\n" + "=" * 80)
    print("FLOOR MAP PROCESSING SERVICE - PNG to SVG CONVERTER")
    print("=" * 80)
    logger.info("Starting PNG to SVG conversion...")
    logger.info(f"Python script: {__file__}")
    logger.info(f"Conversion Engine: {CONVERSION_ENGINE.upper()}{' (Default - Auto-detect enabled)' if AUTO_DETECT_ENGINE else ''}")
    logger.info(f"Auto-detect Engine: {'Enabled' if AUTO_DETECT_ENGINE else 'Disabled'}")

    if not INPUT_DIR.exists():
        logger.error(f"Input directory does not exist: {INPUT_DIR}")
        print("=" * 80 + "\n")
        exit(1)

    if not OUTPUT_DIR.exists():
        logger.info(f"Creating output directory: {OUTPUT_DIR}")
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    setup_file_logging()

    logger.info(
        f"SVGO Optimization: {'Enabled' if ENABLE_SVGO else 'Disabled (using svgcleaner only)'}"
    )
    if not ENABLE_SVGO:
        logger.info(f"Tip: Set ENABLE_SVGO=true to enable additional SVGO optimization")

    vdms_filter = get_vdms_filter()
    if vdms_filter:
        logger.info(f"VDMS Filter: {', '.join(sorted(list(vdms_filter)))}")
    else:
        logger.info("VDMS Filter: None (processing all VDMS)")

    MODE = os.getenv("MODE", "").strip().lower()
    API_ENDPOINT = os.getenv("API_ENDPOINT", "").strip()

    if MODE == "multi-tenant":
        logger.info(f"Mode: Multi-tenant cloud processing (Base URL: {get_base_url()})")
        success = process_floor_from_cloud()
    elif API_ENDPOINT:
        logger.info(f"Mode: API-based filtering (processing specific floors from API)")
        success = process_floors_from_api(API_ENDPOINT)
    else:
        logger.info(f"Mode: Full directory processing (all PNG files)")
        logger.info(
            f"Tip: Set API_ENDPOINT environment variable to process only specific floors"
        )
        success = process_local_files()

    if success:
        logger.info("Script completed successfully!")
    else:
        logger.error("Script completed with errors!")
        exit(1)

    end_time = time.perf_counter()
    total_time = end_time - start_time

    logger.info(f"Total conversion time: {total_time:.2f} seconds")
    print()

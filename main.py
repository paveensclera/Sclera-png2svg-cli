import os
import subprocess
import logging
import glob
import traceback
from cv2 import data
import requests
from datetime import time
from pathlib import Path
from urllib.parse import urlparse
import time
from utils.inject_elements import inject_elements
from utils.resize_img_det import resize_img_det
from utils.api_config import Data
from utils.path_injector import PathInjector
import xml.etree.ElementTree as ET
from io import BytesIO
from utils.remove_txt import remove_txt
from utils.recognize_txt import recognize_txt
import cv2 as cv
import gc 
# Configure logging
# NOTE: Enhanced logging configuration for clear terminal output and progress tracking
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger(__name__)

# NOTE: File Storage Configuration
STORE_PERMANENTLY = os.getenv('STORE_FILES_PERMANENTLY', 'true').lower() == 'true'

# NOTE: SVG Optimization Configuration
# Set to 'false' to disable SVGO optimization (will still use svgcleaner)
ENABLE_SVGO = os.getenv('ENABLE_SVGO', 'true').lower() == 'true'

# NOTE: Local file processing directories
INPUT_DIR = Path(os.getenv("INPUT_DIR", "/home/sclera/images/png_floors"))
OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", "/home/sclera/images/svg_floors"))

# NOTE: Conversion Engine Configuration (default to potrace)
CONVERSION_ENGINE = os.getenv('CONVERSION_ENGINE', 'potrace').lower()
if CONVERSION_ENGINE not in ['potrace', 'vtracer']:
    CONVERSION_ENGINE = 'potrace'

# Create directories for processing
TEMP_DIR = Path("/tmp/floor_map_processing")
TEMP_DIR.mkdir(parents=True, exist_ok=True)

# NOTE: Ensure local processing directories exist
INPUT_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

def analyze_image_for_conversion(png_path: Path) -> dict:
    """Analyze image properties to help with conversion debugging"""
    try:
        identify_cmd = [
            "identify", "-format", 
            "%[colorspace] %[type] %w %h %[colors] %[mean] %[standard-deviation]", 
            str(png_path)
        ]
        
        result = subprocess.run(identify_cmd, capture_output=True, text=True, timeout=10)
        
        if result.returncode == 0:
            parts = result.stdout.strip().split()
            return {
                "colorspace": parts[0],
                "type": parts[1], 
                "width": int(parts[2]),
                "height": int(parts[3]),
                "colors": int(parts[4]),
                "mean": float(parts[5]),
                "std_dev": float(parts[6])
            }
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
    - Sets consistent dimensions (width='500', height='100%') 
    - Adds overflow='visible' for proper rendering
    - Inserts white background rectangle for consistent appearance
    """
    try:
        
        logger.info(f"     🎨 Standardizing SVG for frontend compatibility...")
        logger.info(f"     Using direct svg content")
        try:
            # NOTE: Parse SVG using xml.dom.minidom for manipulation
            from xml.dom import minidom
            dom = minidom.parseString(svg_content)
            svg_element = dom.getElementsByTagName('svg')[0]
            
            # NOTE: Set the same dimensions as the frontend code expects    
            # Removed forced Dimension
            # Was working for potrace but vtracer has its own dimension handling that can conflict with hardcoded values
            # svg_element.setAttribute('width', '500')
            # svg_element.setAttribute('height', '100%')
            svg_element.setAttribute('overflow', 'visible')
            
            
            # NOTE: Add white background rectangle to ensure consistent background
            # This ensures the SVG has an explicit white background regardless of viewer
            rect_element = dom.createElement('rect')
            rect_element.setAttribute('x', '0')
            rect_element.setAttribute('y', '0')
            rect_element.setAttribute('width', '100%')
            rect_element.setAttribute('height', '100%')
            rect_element.setAttribute('fill', 'white')
            
            # NOTE: Insert the background rectangle as the first child of SVG
            if svg_element.firstChild:
                svg_element.insertBefore(rect_element, svg_element.firstChild)
            else:
                svg_element.appendChild(rect_element)
            
            # Serialize back to string
            modified_svg_content = dom.toxml()
            
        except Exception as e:
            # FIXME: Better error handling for XML parsing failures
            logger.warning(f"     ⚠️  Failed to modify SVG dimensions, using original: {str(e)}")
            modified_svg_content = svg_content
        
        # Write the standardized SVG content
        return modified_svg_content
            
    except Exception as e:
        logger.error(f"     ❌ SVG standardization failed: {str(e)}")
        # HACK: Return original path if standardization fails completely
        return svg_content

def optimize_svg(svg_content: str) -> str:
    """
    Optimize SVG file using svgcleaner and optionally svgo for maximum compression and quality
    
    NOTE: This function applies a two-stage optimization process:
    1. svgcleaner - removes unnecessary elements and optimizes structure
    2. svgo (optional) - further compresses and optimizes the SVG content
    
    SVGO can be disabled by setting ENABLE_SVGO=false environment variable
    """
    try:
        # original_size = svg_path.stat().st_size
        # logger.info(f"     📈 Starting SVG optimization (original size: {original_size} bytes)")
        
        # Stage 1: Clean with svgcleaner
        # NOTE: svgcleaner is excellent at removing unused elements and cleaning SVG structure
        # cleaned_svg_path = TEMP_DIR / f"{output_filename}_cleaned.svg"
        
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
            "--remove-unresolved-classes"
        ]
        
        logger.info("     🛠️  Running svgcleaner optimization...")
        svgcleaner = subprocess.Popen(svgcleaner_cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        cleaned_svg, cleaner_err = svgcleaner.communicate(
            input=svg_content,
            timeout=60
        )

        if svgcleaner.returncode != 0:
            logger.warning(
                f"     ⚠️ svgcleaner failed: {cleaner_err}"
            )
            cleaned_svg = svg_content
        
        # Stage 2: Optimize with svgo (if enabled)
        # NOTE: SVGO can be disabled via ENABLE_SVGO environment variable
        # optimized_svg_path = TEMP_DIR / f"{output_filename}_optimized.svg"
        
        if not ENABLE_SVGO:
            # SVGO optimization is disabled - use svgcleaner result only
            logger.info("     ⏭️  SVGO optimization disabled (ENABLE_SVGO=false)")
            logger.info(f"     ℹ️  Using svgcleaner result only")
            optimized_svg = cleaned_svg
        else:
            # SVGO optimization is enabled
            # NOTE: SVGO v3+ configuration approach
            # Check if custom config file exists, otherwise use inline config
            svgo_config_path = Path(__file__).parent / "svgo.config.js"
            
            if svgo_config_path.exists():
                # Use external config file for comprehensive control
                svgo_cmd = [
                    "svgo",
                    "-",
                    "-o", "-",
                    "--config", str(svgo_config_path)
                ]
                logger.info("     🚀 Running svgo optimization (using svgo.config.js)...")
            else:
                # Fallback to inline config
                svgo_cmd = [
                    "svgo",
                    "-",
                    "-o", "-",
                    "--multipass",
                    "--config", '{"plugins": [{"name": "preset-default", "params": {"overrides": {"removeViewBox": false}}}]}'
                ]
                logger.info("     🚀 Running svgo optimization (using inline config)...")
            
            result = subprocess.Popen(svgo_cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            optimized_svg, svgo_err = result.communicate(
                input=cleaned_svg,
                timeout=60
            )

            if result.returncode != 0:
                logger.warning(f"     ⚠️  svgo failed: {svgo_err}")
                # HACK: If svgo fails, use the svgcleaner result as fallback
                optimized_svg = cleaned_svg
        
        # Validate the optimized SVG
        if not optimized_svg:
            logger.error("     ❌ Optimized SVG is invalid or SVGO set to false, using original")
            # FIXME: Better validation needed - check if SVG is actually parseable
            optimized_svg = svg_content
        else:
            final_size = len(optimized_svg)
            logger.info(f"     ✅ svgo optimization completed successfully!")
            logger.info(f"     📊 Final optimization results: {final_size} bytes")
        
        # Clean up intermediate files
       
        return optimized_svg
        
    except subprocess.TimeoutExpired:
        logger.error("SVG optimization process timed out")
        # HACK: Return original file if optimization times out
        return svg_content
    except Exception as e:
        logger.error(f"SVG optimization failed: {str(e)}")
        # HACK: Return original file if optimization fails completely
        return svg_content

def run_vtracer_conversion(png_path: Path, temp_svg_path: Path) -> bool:
    """Run ImageMagick normalization and VTracer to convert PNG to SVG"""
    
    
    temp_filename = png_path.stem + "_temp"
    normalized_png = Path("/dev/shm") / f"{temp_filename}_normalized.png"
    
    try:
        logger.info(f"   🖼️  Step 1/2: Normalizing PNG using ImageMagick...")
        normalize_cmd = [
            "convert",
            str(png_path),
            "-alpha", "remove",
            "-background", "white",
            "PNG24:" + str(normalized_png)
        ]

        result = subprocess.run(
            normalize_cmd,
            capture_output=True,
            text=True,
            timeout=120
        )

        if result.returncode != 0:
            logger.error(f"   ❌ Image normalization failed: {result.stderr}")
            return False
        if not normalized_png.exists():
            logger.error("   ❌ Normalized PNG was not created")
            return False
        
        logger.info(f"   📏 Normalized PNG size: {normalized_png.stat().st_size} bytes")
        logger.info(f"   🖼️  Step 2/2: Converting PNG to SVG using VTracer...")
        
        vtracer_cmd = [
            "vtracer",
            "--input", str(normalized_png),
            "--output", str(temp_svg_path),
            "--mode", "polygon",
            "--colormode", "color",
            "--hierarchical", "stacked",
            "--filter_speckle", "4",
            "--color_precision", "6",
            "--gradient_step", "8",
            "--corner_threshold", "60",
            "--segment_length", "4",
            "--splice_threshold", "45"
        ]
        start_time = time.perf_counter()
        result = subprocess.run(
            vtracer_cmd,
            capture_output=True,
            text=True,
            timeout=300
        )
        end_time = time.perf_counter()
        logger.info(f"   ⏱️  VTracer execution time: {end_time - start_time:.2f} seconds")
        
        if result.returncode != 0:
            logger.error(
                f"   ❌ VTracer conversion failed:\n"
                f"STDOUT:\n{result.stdout}\n"
                f"STDERR:\n{result.stderr}"
            )
            return False
        
        return True

    except subprocess.TimeoutExpired:
        logger.error("   ❌ VTracer conversion timed out")
        return False
    except Exception as e:
        logger.error(f"   ❌ VTracer conversion error: {str(e)}")
        return False
    finally:
        # Clean up normalized png
        try:
            if normalized_png.exists():
                normalized_png.unlink()
        except Exception as e:
            logger.warning(f"   ⚠️  Failed to delete normalized PNG: {str(e)}")


def run_potrace_conversion(png_path: Path, temp_svg_path: Path) -> bool:
    """Run ImageMagick thresholding and Potrace to convert PNG to SVG"""
    temp_filename = png_path.stem + "_temp"
    pbm_path = Path("/dev/shm") / f"{temp_filename}.pbm"
    
    try:
        logger.info(f"   🖼️  Step 1/2: Converting PNG to PBM using ImageMagick...")
        imagemagick_cmd = [
            "convert", str(png_path),
            "-threshold", "98%",
            str(pbm_path)
        ]
        result = subprocess.run(
            imagemagick_cmd,
            capture_output=True,
            text=True,
            timeout=300
        )
        if result.returncode != 0:
            logger.error(f"   ❌ ImageMagick thresholding failed: {result.stderr}")
            return False
        if not pbm_path.exists():
            logger.error("   ❌ PBM file was not created")
            return False
        
        logger.info(f"   📏 PBM file size: {pbm_path.stat().st_size} bytes")
        logger.info(f"   🖊️  Step 2/2: Converting PBM to SVG using Potrace...")
        
        potrace_cmd = [
            "potrace", str(pbm_path),
            "-s",
            "-o", str(temp_svg_path)
        ]
        start_time = time.perf_counter()
        result = subprocess.run(
            potrace_cmd,
            capture_output=True,
            text=True,
            timeout=120
        )
        end_time = time.perf_counter()
        logger.info(f"   ⏱️  Potrace execution time: {end_time - start_time:.2f} seconds")
        
        if result.returncode != 0:
            logger.error(f"   ❌ Potrace conversion failed: {result.stderr}")
            return False
        
        return True

    except subprocess.TimeoutExpired:
        logger.error("   ❌ Potrace conversion timed out")
        return False
    except Exception as e:
        logger.error(f"   ❌ Potrace conversion error: {str(e)}")
        return False
    finally:
        # Clean up pbm file
        try:
            if pbm_path.exists():
                pbm_path.unlink()
        except Exception as e:
            logger.warning(f"   ⚠️  Failed to delete PBM file: {str(e)}")


def convert_png_to_svg_local(png_path: Path) -> str:
    """Convert PNG to SVG using either Potrace or VTracer based on configuration"""
    logger.info(f"📄 CONVERTING: {png_path.name} (Engine: {CONVERSION_ENGINE})")
    logger.info(f"   📂 Input:  {png_path}")

    # Temporary file output path
    temp_filename = png_path.stem + "_temp"
    temp_svg_path = Path("/dev/shm") / f"{temp_filename}.svg"

    try:
        # Step 1: Run the actual conversion tool to generate the temp SVG
        if CONVERSION_ENGINE == "vtracer":
            success = run_vtracer_conversion(png_path, temp_svg_path)
        else:
            success = run_potrace_conversion(png_path, temp_svg_path)

        if not success or not temp_svg_path.exists():
            logger.error(f"   ❌ Conversion tool failed to generate SVG for {png_path.name}")
            return None

        # Step 2: Standardize SVG for frontend compatibility
        logger.info(f"   🎨 Standardizing SVG for frontend compatibility...")
        with open(temp_svg_path, "r", encoding="utf-8") as f:
            svg_content = f.read()

        standardized_svg_content = standardize_svg_for_frontend(svg_content)

        # Step 3: Optimize the SVG using svgcleaner and svgo
        logger.info(f"   ⚡ Optimizing SVG file...")
        optimized_svg_content = optimize_svg(standardized_svg_content)

        # Clean up temp SVG file
        try:
            temp_svg_path.unlink(missing_ok=True)
        except Exception as e:
            logger.warning(f"   ⚠️  Failed to clean up temp SVG file: {str(e)}")

        return optimized_svg_content

    except Exception as e:
        logger.error(f"   ❌ Conversion pipeline failed for {png_path.name}: {str(e)}")
        # Cleanup in case of failure
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
    
    if CONVERSION_ENGINE == "vtracer":
        tools["VTracer"] = ["vtracer", "--help"]
    else:
        tools["Potrace"] = ["potrace", "--version"]
    
    # Add SVGO to checks only if it's enabled
    if ENABLE_SVGO:
        tools["svgo"] = ["svgo", "--version"]
    
    logger.info("🔍 Checking required dependencies...")
    all_ok = True
    
    for tool_name, cmd in tools.items():
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
            if result.returncode == 0:
                logger.info(f"   ✅ {tool_name}: Available")
            else:
                logger.error(f"   ❌ {tool_name}: Not working properly")
                all_ok = False
        except FileNotFoundError:
            logger.error(f"   ❌ {tool_name}: Not installed")
            all_ok = False
        except Exception as e:
            logger.error(f"   ❌ {tool_name}: Error checking - {str(e)}")
            all_ok = False
    
    # Show optional tools status
    if not ENABLE_SVGO:
        logger.info(f"   ⏭️  svgo: Disabled (ENABLE_SVGO=false)")
    
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
        logger.info(f"🌐 Fetching floor URLs from API: {api_endpoint}")
        
        response = requests.get(api_endpoint, timeout=30)
        response.raise_for_status()  # Raise exception for bad status codes
        
        urls = response.json()
        
        if not isinstance(urls, list):
            logger.error(f"❌ API response is not a list: {type(urls)}")
            return []
        
        logger.info(f"✅ Successfully fetched {len(urls)} floor URLs from API")
        return urls
        
    except requests.exceptions.Timeout:
        logger.error(f"❌ API request timed out after 30 seconds")
        return []
    except requests.exceptions.RequestException as e:
        logger.error(f"❌ Failed to fetch floor URLs from API: {str(e)}")
        return []
    except ValueError as e:
        logger.error(f"❌ Failed to parse API response as JSON: {str(e)}")
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
            # Parse URL and extract the filename from the path
            parsed_url = urlparse(url)
            path = parsed_url.path
            
            # Extract filename from the path
            filename = os.path.basename(path)
            
            if filename:
                filenames.append(filename)
                logger.debug(f"   📄 Extracted filename: {filename}")
            else:
                logger.warning(f"   ⚠️  Could not extract filename from URL: {url}")
                
        except Exception as e:
            logger.warning(f"   ⚠️  Failed to parse URL: {url} - {str(e)}")
    
    logger.info(f"📋 Extracted {len(filenames)} filenames from URLs")
    return filenames

def validate_file_readability(file_path: Path) -> tuple[bool, str]:
    """
    Validate that a file exists and is readable
    
    Args:
        file_path: Path to the file to validate
    
    Returns:
        Tuple of (is_valid, error_message)
    """
    try:
        # Check if file exists
        if not file_path.exists():
            return False, "File does not exist"
        
        # Check if it's actually a file (not a directory)
        if not file_path.is_file():
            return False, "Path is not a file"
        
        # Check if file is readable
        if not os.access(file_path, os.R_OK):
            return False, "File is not readable (permission denied)"
        
        # Check if file has content
        file_size = file_path.stat().st_size
        if file_size == 0:
            return False, "File is empty (0 bytes)"
        
        # Check if file is too small to be a valid PNG
        if file_size < 100:
            return False, f"File is too small ({file_size} bytes) to be a valid PNG"
        
        # Try to read the PNG header to verify it's a valid PNG file
        try:
            with open(file_path, 'rb') as f:
                header = f.read(8)
                # PNG magic number: 89 50 4E 47 0D 0A 1A 0A
                if header != b'\x89PNG\r\n\x1a\n':
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
    logger.info(f"🔍 Validating {len(requested_filenames)} files from API...")
    
    missing_files = []
    invalid_files = []
    valid_files = []
    
    for filename in requested_filenames:
        file_path = input_dir / filename
        is_valid, error_msg = validate_file_readability(file_path)
        
        if not file_path.exists():
            missing_files.append(filename)
            logger.warning(f"   ❌ MISSING: {filename}")
        elif not is_valid:
            invalid_files.append((filename, error_msg))
            logger.warning(f"   ⚠️  INVALID: {filename} - {error_msg}")
        else:
            valid_files.append(filename)
    
    # Summary of validation
    logger.info(f"✅ Validation complete:")
    logger.info(f"   ✅ Valid files: {len(valid_files)}")
    logger.info(f"   ❌ Missing files: {len(missing_files)}")
    logger.info(f"   ⚠️  Invalid files: {len(invalid_files)}")
    
    return {
        'missing': missing_files,
        'invalid': invalid_files,
        'valid': valid_files
    }

def process_local_files(filter_filenames: list = None): 
    """
    NOTE: Main function for local file processing
    Scans INPUT_DIR for PNG files and converts them to SVG in OUTPUT_DIR
    
    Args:
        filter_filenames: Optional list of filenames to process. If provided, only these files will be processed.
                         If None, all PNG files in INPUT_DIR will be processed.
    """
    print("="*80)  # NOTE: Visual separator for terminal output
    logger.info(f"🚀 STARTING LOCAL FILE PROCESSING")
    logger.info(f"📁 Input directory:  {INPUT_DIR}")
    logger.info(f"📁 Output directory: {OUTPUT_DIR}")
    if filter_filenames:
        logger.info(f"🔍 Filter mode: Processing only {len(filter_filenames)} specific files from API")
    else:
        logger.info(f"🔍 Mode: Processing all PNG files in directory")
    print("="*80)
    
    # NOTE: Check dependencies first
    if not check_dependencies():
        engine_name = "Potrace" if CONVERSION_ENGINE == "potrace" else "VTracer"
        deps = f" {engine_name}, and svgcleaner"
        if ENABLE_SVGO:
            deps += ", and svgo"
        logger.error(f"❌ Missing required dependencies. Please install {deps}")
        return False
    
    print("="*80)
    
    # Track missing files separately
    missing_files = []
    invalid_files = []
    
    # NOTE: Find all PNG files in input directory
    png_pattern = str(INPUT_DIR / "*.png")
    all_png_files = glob.glob(png_pattern)
    
    # NOTE: Apply filename filter if provided with validation
    if filter_filenames:
        # Validate files from API before processing
        validation_results = check_missing_files(filter_filenames, INPUT_DIR)
        
        missing_files = validation_results['missing']
        invalid_files = validation_results['invalid']
        valid_filenames = validation_results['valid']
        
        # Create a set for faster lookup
        filter_set = set(filter_filenames)
        png_files = [f for f in all_png_files if os.path.basename(f) in filter_set]
        
        logger.info(f"📊 Found {len(all_png_files)} total PNG files in directory")
        logger.info(f"🎯 Filtered to {len(png_files)} valid files to process")
        
        # Report missing files in detail
        if missing_files:
            print()
            logger.warning(f"⚠️  MISSING FILES REPORT ({len(missing_files)} files):")
            for i, filename in enumerate(missing_files[:10], 1):  # Show first 10
                logger.warning(f"   {i}. {filename}")
            if len(missing_files) > 10:
                logger.warning(f"   ... and {len(missing_files) - 10} more missing files")
            print()
        
        # Report invalid files in detail
        if invalid_files:
            print()
            logger.warning(f"⚠️  INVALID FILES REPORT ({len(invalid_files)} files):")
            for i, (filename, reason) in enumerate(invalid_files[:10], 1):  # Show first 10
                logger.warning(f"   {i}. {filename} - {reason}")
            if len(invalid_files) > 10:
                logger.warning(f"   ... and {len(invalid_files) - 10} more invalid files")
            print()
        
        if not png_files:
            logger.error(f"❌ No valid PNG files found to process after validation")
            logger.error(f"   API requested: {len(filter_filenames)} files")
            logger.error(f"   Missing: {len(missing_files)} files")
            logger.error(f"   Invalid: {len(invalid_files)} files")
            logger.error(f"   Available in directory: {len(all_png_files)} files")
            print("="*80)
            return False
    else:
        png_files = all_png_files
        if not png_files:
            logger.warning(f"❌ No PNG files found in {INPUT_DIR}")
            print("="*80)
            return False
    
    # NOTE: Sort files for consistent processing order
    png_files.sort()
    total_files = len(png_files)
    logger.info(f"📊 Processing {total_files} PNG files")
    print("="*80)
    
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
            # NOTE: Generate output SVG path with same filename but .svg extension
            svg_filename = png_path.stem + ".svg"
            output_svg_path = OUTPUT_DIR / svg_filename
            
            print()  # NOTE: Add spacing between files for readability
            logger.info(f"📋 PROCESSING FILE {index}/{total_files}")
            logger.info(f"   📄 File: {png_path.name}")
            logger.info(f"   📏 Size: {png_path.stat().st_size} bytes")
            
            # NOTE: Skip if SVG already exists unless we want to overwrite
            if output_svg_path.exists():
                skipped_count += 1
                logger.info(f"   ⭐️  SKIPPED: SVG already exists ({svg_filename})")
                logger.info(f"   📊 Progress: {index}/{total_files} files checked")
                continue
            
            # NOTE: Convert PNG to SVG using local processing function
            logger.info(f"   🎯 Starting conversion process...")
            img, w, h = load_image(png_path)
            detections = recognize_txt(img)  
            img, detections = resize_img_det(img, detections)         
            img = remove_txt(img, detections)
            temp_png = Path("/dev/shm") / f"{png_path.stem}_notext.png"

            cv.imwrite(str(temp_png), img)

            resultant_svg_content = convert_png_to_svg_local(temp_png)
            
            if not resultant_svg_content:
                conversion_failed_count += 1
                logger.error(f"   ❌ CONVERSION FAILED: Could not process {png_path.name}")
                continue
                
            floor_parts = svg_filename.split("_")
            floor_id = floor_parts[0]

            floor_name = data.get_floor(floor_id)
            paths_string = data.get_paths(floor_id)
            floor_locations = data.get_locations(floor_id)
            tree = inject_elements(
                svg=resultant_svg_content,
                detections=detections,
                floor_id=floor_id,
                floor_name=floor_name,
                floor_locations=floor_locations,
                paths=paths_string,
                injector=injector
            )
            tree.write(os.path.join(OUTPUT_DIR, svg_filename), encoding="utf-8", xml_declaration=True)
            processed_count += 1
            final_size = os.path.getsize(output_svg_path)
            logger.info(f"   ✅ COMPLETED: {png_path.name} -> {svg_filename} ({final_size} bytes)")
            logger.info(f"   📊 Progress: {index}/{total_files} files processed ({processed_count} successful)")
                
        except Exception as e:
            conversion_failed_count += 1
            logger.error(f"   ❌ PROCESSING ERROR: {png_file_path}: {str(e)}")
            logger.info(f"   📊 Progress: {index}/{total_files} files processed ({conversion_failed_count} conversion failures)")
        finally:
            if temp_png and temp_png.exists():
                try:
                    temp_png.unlink()
                    logger.info(f"   🧹 Cleaned up temporary file: {temp_png.name}")
                except Exception as cleanup_err:
                    logger.warning(f"   ⚠️  Failed to delete temp file {temp_png}: {cleanup_err}")
    overall_end = time.perf_counter()

    # NOTE: Final summary with clear visual separation
    print()
    print("="*80)
    logger.info(f"🏁 LOCAL FILE PROCESSING COMPLETED!")
    logger.info(f"📊 FINAL RESULTS:")
    logger.info(f"   ✅ Processed successfully: {processed_count} files")
    logger.info(f"   ⭐️  Skipped (already exist): {skipped_count} files")
    logger.info(
            f"   🕒 Total processing time: "
            f"{overall_end - overall_start:.2f} seconds"
        )
    # Show separate counts for different types of failures
    if filter_filenames:
        logger.info(f"   ❌ Missing files (not in directory): {len(missing_files)} files")
        logger.info(f"   ⚠️  Invalid files (corrupted/unreadable): {len(invalid_files)} files")
    logger.info(f"   ❌ Conversion failures: {conversion_failed_count} files")
    
    total_failures = len(missing_files) + len(invalid_files) + conversion_failed_count
    if total_failures > 0:
        logger.warning(f"   📊 Total failures: {total_failures} files")
    
    if filter_filenames:
        logger.info(f"   📁 Total requested from API: {len(filter_filenames)}")
    else:
        logger.info(f"   📁 Total files found: {total_files}")
    
    if processed_count > 0:
        logger.info(f"🎉 {processed_count} new SVG files created in {OUTPUT_DIR}")
    
    # Final status assessment
    if total_failures > 0:
        logger.warning(f"⚠️  Completed with {total_failures} failures")
    
    print("="*80)
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
    print("="*80)  # NOTE: Visual separator for terminal output
    logger.info(f"🚀 STARTING API-DRIVEN FILE PROCESSING")
    logger.info(f"📁 Input directory:  {INPUT_DIR}")
    logger.info(f"📁 Output directory: {OUTPUT_DIR}")
    if filter_filenames:
        logger.info(f"🔍 Filter mode: Processing only {len(filter_filenames)} specific files from API")
    else:
        logger.info(f"🔍 Mode: Processing all PNG files in directory")
    print("="*80)
    
    # NOTE: Check dependencies first
    if not check_dependencies():
        engine_name = "Potrace" if CONVERSION_ENGINE == "potrace" else "VTracer"
        deps = f" {engine_name}, and svgcleaner"
        if ENABLE_SVGO:
            deps += ", and svgo"
        logger.error(f"❌ Missing required dependencies. Please install {deps}")
        return False
    
    print("="*80)
    
    # Track missing files separately
    missing_files = []
    invalid_files = []
    
    # NOTE: Find all PNG files in input directory
    png_pattern = str(INPUT_DIR / "*.png")
    all_png_files = glob.glob(png_pattern)
    data = Data()
    injector = PathInjector()

    # NOTE: Apply filename filter if provided with validation
    if filter_filenames:
        # Validate files from API before processing
        validation_results = check_missing_files(filter_filenames, INPUT_DIR)
        
        missing_files = validation_results['missing']
        invalid_files = validation_results['invalid']
        valid_filenames = validation_results['valid']
        
        # Create a set for faster lookup
        filter_set = set(filter_filenames)
        png_files = [f for f in all_png_files if os.path.basename(f) in filter_set]
        
        logger.info(f"📊 Found {len(all_png_files)} total PNG files in directory")
        logger.info(f"🎯 Filtered to {len(png_files)} valid files to process")
        
        # Report missing files in detail
        if missing_files:
            print()
            logger.warning(f"⚠️  MISSING FILES REPORT ({len(missing_files)} files):")
            for i, filename in enumerate(missing_files[:10], 1):  # Show first 10
                logger.warning(f"   {i}. {filename}")
            if len(missing_files) > 10:
                logger.warning(f"   ... and {len(missing_files) - 10} more missing files")
            print()
        
        # Report invalid files in detail
        if invalid_files:
            print()
            logger.warning(f"⚠️  INVALID FILES REPORT ({len(invalid_files)} files):")
            for i, (filename, reason) in enumerate(invalid_files[:10], 1):  # Show first 10
                logger.warning(f"   {i}. {filename} - {reason}")
            if len(invalid_files) > 10:
                logger.warning(f"   ... and {len(invalid_files) - 10} more invalid files")
            print()
        
        if not png_files:
            logger.error(f"❌ No valid PNG files found to process after validation")
            logger.error(f"   API requested: {len(filter_filenames)} files")
            logger.error(f"   Missing: {len(missing_files)} files")
            logger.error(f"   Invalid: {len(invalid_files)} files")
            logger.error(f"   Available in directory: {len(all_png_files)} files")
            print("="*80)
            return False
    else:
        png_files = all_png_files
        if not png_files:
            logger.warning(f"❌ No PNG files found in {INPUT_DIR}")
            print("="*80)
            return False
    
    # NOTE: Sort files for consistent processing order
    png_files.sort()
    total_files = len(png_files)
    logger.info(f"📊 Processing {total_files} PNG files")
    print("="*80)
    
    processed_count = 0
    conversion_failed_count = 0
    skipped_count = 0
    overall_start = time.perf_counter()
    for index, png_file_path in enumerate(png_files, 1):
        temp_png = None
        try:
            start = time.perf_counter()
    
            png_path = Path(png_file_path)
            # NOTE: Generate output SVG path with same filename but .svg extension
            svg_filename = png_path.stem + ".svg"
            output_svg_path = OUTPUT_DIR / svg_filename
            
            print()  # NOTE: Add spacing between files for readability
            logger.info(f"📋 PROCESSING FILE {index}/{total_files}")
            logger.info(f"   📄 File: {png_path.name}")
            logger.info(f"   📏 Size: {png_path.stat().st_size} bytes")
            
            # NOTE: Skip if SVG already exists unless we want to overwrite
            if output_svg_path.exists():
                skipped_count += 1
                logger.info(f"   ⭐️  SKIPPED: SVG already exists ({svg_filename})")
                logger.info(f"   📊 Progress: {index}/{total_files} files checked")
                continue
            
            # NOTE: Convert PNG to SVG using local processing function
            logger.info(f"   🎯 Starting conversion process...")
            # Get the Textless PNG 
            # Convert it to SVG using local processing function
            img, w,h  = load_image(png_path)
            detections = recognize_txt(img)  
            img,detections = resize_img_det(img, detections)         
            img = remove_txt(img, detections)
            temp_png = Path("/dev/shm") / f"{png_path.stem}_notext.png"

            cv.imwrite(str(temp_png), img)

            resultant_svg_content = convert_png_to_svg_local(temp_png)
            
            if not resultant_svg_content:
                conversion_failed_count += 1
                logger.error(f"   ❌ CONVERSION FAILED: Could not process {png_path.name}")
                continue

            floor_parts = svg_filename.split("_")
            floor_id = floor_parts[0]

            floor_name = data.get_floor(floor_id)
            paths_string = data.get_paths(floor_id)
            floor_locations = data.get_locations(floor_id)
            tree = inject_elements(
                svg=resultant_svg_content,
                detections=detections,
                floor_id=floor_id,
                floor_name=floor_name,
                floor_locations=floor_locations,
                paths=paths_string,
                injector=injector
            )

            tree.write(os.path.join(OUTPUT_DIR, svg_filename), encoding="utf-8", xml_declaration=True)

            # svg_bytes = ET.tostring(tree.getroot(), encoding="utf-8", xml_declaration=True)
            # upload_response = data.upload_floor(floor_id, svg_bytes)
            # print("Upload response:", upload_response)
            
            processed_count += 1
            final_size = len(resultant_svg_content)
            end = time.perf_counter()
            logger.info(f"   ✅ COMPLETED: {png_path.name} -> {svg_filename} ({final_size} bytes)")
            logger.info(f"   🕒 Processing time: {end - start:.2f} seconds")
            logger.info(f"   📊 Progress: {index}/{total_files} files processed ({processed_count} successful)")
                
        except Exception as e:
            conversion_failed_count += 1
            logger.error(f"   ❌ PROCESSING ERROR: {png_file_path}: {str(e)}")
            logger.error(traceback.format_exc())
            logger.info(f"   📊 Progress: {index}/{total_files} files processed ({conversion_failed_count} conversion failures)")
        finally:
            if temp_png and temp_png.exists():
                try:
                    temp_png.unlink()
                    logger.info(f"   🧹 Cleaned up temporary file: {temp_png.name}")
                except Exception as cleanup_err:
                    logger.warning(f"   ⚠️  Failed to delete temp file {temp_png}: {cleanup_err}")
    overall_end = time.perf_counter()

    # NOTE: Final summary with clear visual separation
    print()
    print("="*80)
    logger.info(f"🏁 LOCAL FILE PROCESSING COMPLETED!")
    logger.info(f"📊 FINAL RESULTS:")
    logger.info(f"   ✅ Processed successfully: {processed_count} files")
    logger.info(f"   ⭐️  Skipped (already exist): {skipped_count} files")
    logger.info(
            f"   🕒 Total processing time: "
            f"{overall_end - overall_start:.2f} seconds"
        )
    # Show separate counts for different types of failures
    if filter_filenames:
        logger.info(f"   ❌ Missing files (not in directory): {len(missing_files)} files")
        logger.info(f"   ⚠️  Invalid files (corrupted/unreadable): {len(invalid_files)} files")
    logger.info(f"   ❌ Conversion failures: {conversion_failed_count} files")
    
    total_failures = len(missing_files) + len(invalid_files) + conversion_failed_count
    if total_failures > 0:
        logger.warning(f"   📊 Total failures: {total_failures} files")
    
    if filter_filenames:
        logger.info(f"   📁 Total requested from API: {len(filter_filenames)}")
    else:
        logger.info(f"   📁 Total files found: {total_files}")
    
    if processed_count > 0:
        logger.info(f"🎉 {processed_count} new SVG files created in {OUTPUT_DIR}")
    
    # Final status assessment
    if total_failures > 0:
        logger.warning(f"⚠️  Completed with {total_failures} failures")
    
    print("="*80)
    return True

def process_floors_from_api(api_endpoint: str):
    """
    Fetch floor URLs from API and process only those specific floors
    
    Args:
        api_endpoint: The API endpoint URL that returns list of floor image URLs
    
    Returns:
        bool: True if processing completed successfully, False otherwise
    """
    print("\n" + "="*80)
    logger.info(f"🌐 API-BASED FLOOR PROCESSING MODE")
    logger.info(f"📡 API Endpoint: {api_endpoint}")
    print("="*80)
    
    # Step 1: Fetch URLs from API
    floor_urls = fetch_floor_urls_from_api(api_endpoint)
    
    if not floor_urls:
        logger.error("❌ No floor URLs fetched from API. Cannot proceed.")
        return False
    
    # Step 2: Extract filenames from URLs
    logger.info(f"🔍 Extracting filenames from {len(floor_urls)} URLs...")
    filenames = extract_filenames_from_urls(floor_urls)
    
    if not filenames:
        logger.error("❌ No filenames extracted from URLs. Cannot proceed.")
        return False
    
    logger.info(f"✅ Successfully extracted {len(filenames)} filenames")
    logger.info(f"📋 Sample filenames:")
    for i, filename in enumerate(filenames[:5], 1):  # Show first 5
        logger.info(f"   {i}. {filename}")
    if len(filenames) > 5:
        logger.info(f"   ... and {len(filenames) - 5} more")
    
    print("="*80)
    
    # Step 3: Process only the filtered files
    logger.info(f"🎯 Starting filtered processing of {len(filenames)} floors...")
    success = process_filtered_files(filter_filenames=filenames)
    
    return success

def parse_arguments_and_update_config():
    """Parse CLI arguments and update configuration globals"""
    global CONVERSION_ENGINE, ENABLE_SVGO, INPUT_DIR, OUTPUT_DIR
    
    import argparse
    parser = argparse.ArgumentParser(description="Floor Map Processing Service - PNG to SVG Converter")
    parser.add_argument(
        "--engine", "-e",
        choices=["potrace", "vtracer"],
        default=CONVERSION_ENGINE,
        help="Conversion engine to use (default: potrace or CONVERSION_ENGINE env var)"
    )
    parser.add_argument(
        "--disable-svgo",
        action="store_true",
        default=not ENABLE_SVGO,
        help="Disable SVGO optimization (use svgcleaner only)"
    )
    parser.add_argument(
        "--input-dir", "-i",
        type=str,
        default=str(INPUT_DIR),
        help="Input directory containing PNG files"
    )
    parser.add_argument(
        "--output-dir", "-o",
        type=str,
        default=str(OUTPUT_DIR),
        help="Output directory for SVG files"
    )
    
    # Parse arguments
    args, unknown = parser.parse_known_args()
    
    CONVERSION_ENGINE = args.engine
    ENABLE_SVGO = not args.disable_svgo
    INPUT_DIR = Path(args.input_dir)
    OUTPUT_DIR = Path(args.output_dir)
    
    return args


if __name__ == "__main__":
    start_time = time.perf_counter()
    args = parse_arguments_and_update_config()

    print("\n" + "="*80)
    print("🗂️  FLOOR MAP PROCESSING SERVICE - PNG to SVG CONVERTER")
    print("="*80)
    logger.info("🚀 Starting PNG to SVG conversion...")
    logger.info(f"🐍 Python script: {__file__}")
    logger.info(f"⚙️  Conversion Engine: {CONVERSION_ENGINE.upper()}")
    
    # NOTE: Verify required directories exist before processing
    if not INPUT_DIR.exists():
        logger.error(f"❌ Input directory does not exist: {INPUT_DIR}")
        print("="*80 + "\n")
        exit(1)
    
    if not OUTPUT_DIR.exists():
        logger.info(f"📁 Creating output directory: {OUTPUT_DIR}")
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    
    # NOTE: Display optimization configuration
    logger.info(f"⚙️  SVGO Optimization: {'✅ Enabled' if ENABLE_SVGO else '⏭️  Disabled (using svgcleaner only)'}")
    if not ENABLE_SVGO:
        logger.info(f"💡 Tip: Set ENABLE_SVGO=true to enable additional SVGO optimization")
    
    # NOTE: Determine processing mode based on API_ENDPOINT
    API_ENDPOINT = os.getenv('API_ENDPOINT', '').strip()
    if API_ENDPOINT:
        logger.info(f"⚙️  Mode: API-based filtering (processing specific floors from API)")
        success = process_floors_from_api(API_ENDPOINT)
    else:
        logger.info(f"⚙️  Mode: Full directory processing (all PNG files)")
        logger.info(f"💡 Tip: Set API_ENDPOINT environment variable to process only specific floors")
        success = process_local_files()
    
    if success:
        logger.info("✅ Script completed successfully!")
    else:
        logger.error("❌ Script completed with errors!")
        exit(1)

    svg_directory="./vdms_svg_outputs"  # given by png -> svg converter
    output_directory="./uploads"     # temp folder, will write directly later
    ET.register_namespace('', "http://www.w3.org/2000/svg")

    files = []
    
    # assuming pngs are already converted to svgs inside the folder
    
    end_time = time.perf_counter()
    total_time = end_time - start_time

    logger.info(f"✅ Total conversion time: {total_time:.2f} seconds")
    print()  # NOTE: Final newline for clean terminal output
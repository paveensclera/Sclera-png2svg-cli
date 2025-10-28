import os
import subprocess
import logging
import glob
from datetime import datetime
from pathlib import Path

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

# NOTE: Local file processing directories
INPUT_DIR = Path("/home/sclera/images/png_floors")
OUTPUT_DIR = Path("/home/sclera/images/svg_floors")

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

def standardize_svg_for_frontend(svg_path: Path, output_filename: str) -> Path:
    """
    Standardize SVG dimensions and add white background for frontend compatibility
    
    NOTE: This function modifies the SVG to match frontend expectations:
    - Sets consistent dimensions (width='500', height='100%') 
    - Adds overflow='visible' for proper rendering
    - Inserts white background rectangle for consistent appearance
    """
    try:
        standardized_svg_path = TEMP_DIR / f"{output_filename}_standardized.svg"
        
        logger.info(f"     🎨 Standardizing SVG for frontend compatibility...")
        
        # Read the original SVG content
        with open(svg_path, 'r', encoding='utf-8') as f:
            svg_content = f.read()
        
        try:
            # NOTE: Parse SVG using xml.dom.minidom for manipulation
            from xml.dom import minidom
            dom = minidom.parseString(svg_content)
            svg_element = dom.getElementsByTagName('svg')[0]
            
            # NOTE: Set the same dimensions as the frontend code expects
            svg_element.setAttribute('overflow', 'visible')
            svg_element.setAttribute('width', '500')
            svg_element.setAttribute('height', '100%')
            
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
        with open(standardized_svg_path, 'w', encoding='utf-8') as f:
            f.write(modified_svg_content)
        
        if standardized_svg_path.exists():
            standardized_size = standardized_svg_path.stat().st_size
            logger.info(f"     ✅ SVG standardization completed ({standardized_size} bytes)")
            return standardized_svg_path
        else:
            # HACK: Return original path if standardization fails
            logger.warning(f"     ⚠️  Standardization failed, using original SVG")
            return svg_path
            
    except Exception as e:
        logger.error(f"     ❌ SVG standardization failed: {str(e)}")
        # HACK: Return original path if standardization fails completely
        return svg_path

def optimize_svg(svg_path: Path, output_filename: str) -> Path:
    """
    Optimize SVG file using both svgcleaner and svgo for maximum compression and quality
    
    NOTE: This function applies a two-stage optimization process:
    1. svgcleaner - removes unnecessary elements and optimizes structure
    2. svgo - further compresses and optimizes the SVG content
    """
    try:
        original_size = svg_path.stat().st_size
        logger.info(f"     📈 Starting SVG optimization (original size: {original_size} bytes)")
        
        # Stage 1: Clean with svgcleaner
        # NOTE: svgcleaner is excellent at removing unused elements and cleaning SVG structure
        cleaned_svg_path = TEMP_DIR / f"{output_filename}_cleaned.svg"
        
        svgcleaner_cmd = [
            "svgcleaner",
            str(svg_path),
            str(cleaned_svg_path),
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
        result = subprocess.run(svgcleaner_cmd, capture_output=True, text=True, timeout=60)
        
        if result.returncode != 0:
            logger.warning(f"     ⚠️  svgcleaner failed: {result.stderr}")
            # HACK: If svgcleaner fails, continue with original file for svgo optimization
            cleaned_svg_path = svg_path
        else:
            cleaned_size = cleaned_svg_path.stat().st_size
            reduction_percent = ((original_size - cleaned_size) / original_size) * 100
            logger.info(f"     ✅ svgcleaner completed. Size: {original_size} -> {cleaned_size} bytes ({reduction_percent:.1f}% reduction)")
        
        # Stage 2: Optimize with svgo
        # NOTE: svgo provides additional compression and modern optimization techniques
        optimized_svg_path = TEMP_DIR / f"{output_filename}_optimized.svg"
        
        svgo_cmd = [
            "svgo",
            str(cleaned_svg_path),
            "-o", str(optimized_svg_path),
            "--enable=removeDoctype",
            "--enable=removeXMLProcInst", 
            "--enable=removeComments",
            "--enable=removeMetadata",
            "--enable=removeEditorsNSData",
            "--enable=cleanupAttrs",
            "--enable=mergeStyles",
            "--enable=inlineStyles",
            "--enable=minifyStyles",
            "--enable=cleanupNumericValues",
            "--enable=convertColors",
            "--enable=removeUnknownsAndDefaults",
            "--enable=removeNonInheritedGroupAttrs",
            "--enable=removeUselessStrokeAndFill",
            "--enable=removeViewBox",
            "--enable=cleanupEnableBackground",
            "--enable=removeHiddenElems",
            "--enable=removeEmptyText",
            "--enable=convertShapeToPath",
            "--enable=convertEllipseToCircle",
            "--enable=moveElemsAttrsToGroup",
            "--enable=moveGroupAttrsToElems",
            "--enable=collapseGroups",
            "--enable=convertPathData",
            "--enable=convertTransform",
            "--enable=removeEmptyAttrs",
            "--enable=removeEmptyContainers",
            "--enable=mergePaths",
            "--enable=removeUnusedNS",
            "--enable=sortDefsChildren",
            "--enable=removeTitle",
            "--enable=removeDesc"
        ]
        
        logger.info("     🚀 Running svgo optimization...")
        result = subprocess.run(svgo_cmd, capture_output=True, text=True, timeout=60)
        
        if result.returncode != 0:
            logger.warning(f"     ⚠️  svgo failed: {result.stderr}")
            # HACK: If svgo fails, use the svgcleaner result as fallback
            if cleaned_svg_path.exists():
                optimized_svg_path = cleaned_svg_path
            else:
                optimized_svg_path = svg_path
        
        # Validate the optimized SVG
        if not optimized_svg_path.exists() or optimized_svg_path.stat().st_size < 50:
            logger.error("     ❌ Optimized SVG is invalid or too small, using original")
            # FIXME: Better validation needed - check if SVG is actually parseable
            optimized_svg_path = svg_path
        else:
            final_size = optimized_svg_path.stat().st_size
            compression_ratio = ((original_size - final_size) / original_size) * 100
            logger.info(f"     ✅ svgo optimization completed successfully!")
            logger.info(f"     📊 Final optimization results: {original_size} -> {final_size} bytes ({compression_ratio:.1f}% total reduction)")
        
        # Clean up intermediate files
        logger.info(f"     🧹 Cleaning up optimization intermediate files...")
        try:
            if cleaned_svg_path != svg_path and cleaned_svg_path != optimized_svg_path and cleaned_svg_path.exists():
                cleaned_svg_path.unlink()
                logger.info(f"     🗑️  Removed optimization temp file: {cleaned_svg_path.name}")
        except Exception as e:
            logger.warning(f"     ⚠️  Failed to clean up intermediate optimization files: {str(e)}")
        
        return optimized_svg_path
        
    except subprocess.TimeoutExpired:
        logger.error("SVG optimization process timed out")
        # HACK: Return original file if optimization times out
        return svg_path
    except Exception as e:
        logger.error(f"SVG optimization failed: {str(e)}")
        # HACK: Return original file if optimization fails completely
        return svg_path

def convert_png_to_svg_local(png_path: Path, output_svg_path: Path) -> Path:
    """Convert PNG to SVG using ImageMagick and Potrace for local file processing"""
    try:
        # NOTE: Generate temporary filename for intermediate processing
        temp_filename = png_path.stem + "_temp"
        pbm_path = TEMP_DIR / f"{temp_filename}.pbm"
        temp_svg_path = TEMP_DIR / f"{temp_filename}.svg"

        # NOTE: Enhanced logging for clear progress tracking
        logger.info(f"📄 CONVERTING: {png_path.name} -> {output_svg_path.name}")
        logger.info(f"   📂 Input:  {png_path}")
        logger.info(f"   📂 Output: {output_svg_path}")

        # Step 1: Convert PNG to PBM using ImageMagick
        logger.info(f"   🖼️  Step 1/4: Converting PNG to PBM using ImageMagick...")
        imagemagick_cmd = [
            "convert", str(png_path),
            "-threshold", "99%",
            str(pbm_path)
        ]
        result = subprocess.run(imagemagick_cmd, capture_output=True, text=True, timeout=300) # 120 seconds is too short for large PNG files
        if result.returncode != 0:
            logger.error(f"   ❌ ImageMagick conversion failed for {png_path}: {result.stderr}")
            return None
        logger.info(f"   ✅ Step 1/4 completed: PBM file created ({pbm_path.stat().st_size} bytes)")

        # Step 2: Convert PBM to SVG using Potrace
        logger.info(f"   🖊️  Step 2/4: Converting PBM to SVG using Potrace...")
        potrace_cmd = [
            "potrace", str(pbm_path),
            "-s",
            "-o", str(temp_svg_path)
        ]
        result = subprocess.run(potrace_cmd, capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            logger.error(f"   ❌ Potrace conversion failed for {png_path}: {result.stderr}")
            return None

        # Validate initial SVG output
        if not temp_svg_path.exists() or temp_svg_path.stat().st_size < 100:
            logger.error(f"   ❌ Generated SVG file is invalid or too small for {png_path}")
            return None
        logger.info(f"   ✅ Step 2/4 completed: SVG file created ({temp_svg_path.stat().st_size} bytes)")

        # Step 3: Standardize SVG for frontend compatibility
        # NOTE: This step adds consistent dimensions and white background for smooth frontend integration
        logger.info(f"   🎨 Step 3/4: Standardizing SVG for frontend compatibility...")
        standardized_svg_path = standardize_svg_for_frontend(temp_svg_path, temp_filename)
        logger.info(f"   ✅ Step 3/4 completed: SVG standardized for frontend")

        # Step 4: Optimize the SVG using svgcleaner and svgo
        logger.info(f"   ⚡ Step 4/4: Optimizing SVG file...")
        optimized_svg_path = optimize_svg(standardized_svg_path, temp_filename)
        
        # NOTE: Move optimized SVG to final output location
        if optimized_svg_path.exists():
            # FIXME: Use shutil.move for better cross-filesystem compatibility
            import shutil
            shutil.move(str(optimized_svg_path), str(output_svg_path))
            final_size = output_svg_path.stat().st_size
            logger.info(f"   ✅ Step 4/4 completed: SVG optimized and saved ({final_size} bytes)")
            logger.info(f"🎉 SUCCESS: {png_path.name} -> {output_svg_path.name} (Final size: {final_size} bytes)")
        
        # Clean up intermediate files
        logger.info(f"   🧹 Cleaning up intermediate files...")
        try:
            if pbm_path.exists():
                pbm_path.unlink()
                logger.info(f"   🗑️  Removed: {pbm_path.name}")
            if temp_svg_path.exists() and temp_svg_path != optimized_svg_path:
                temp_svg_path.unlink()
                logger.info(f"   🗑️  Removed: {temp_svg_path.name}")
            if standardized_svg_path.exists() and standardized_svg_path != optimized_svg_path and standardized_svg_path != temp_svg_path:
                standardized_svg_path.unlink()
                logger.info(f"   🗑️  Removed: {standardized_svg_path.name}")
        except Exception as e:
            logger.warning(f"   ⚠️  Failed to clean up intermediate files: {str(e)}")

        return output_svg_path if output_svg_path.exists() else None

    except subprocess.TimeoutExpired:
        logger.error(f"   ❌ Conversion process timed out for {png_path}")
        return None
    except Exception as e:
        logger.error(f"   ❌ Conversion failed for {png_path}: {str(e)}")
        return None

def check_dependencies():
    """Check if required tools are available"""
    tools = {
        "ImageMagick": ["convert", "--version"],
        "Potrace": ["potrace", "--version"],
        "svgcleaner": ["svgcleaner", "--version"],
        "svgo": ["svgo", "--version"]
    }
    
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
    
    return all_ok

def process_local_files():
    """
    NOTE: Main function for local file processing
    Scans INPUT_DIR for PNG files and converts them to SVG in OUTPUT_DIR
    """
    print("="*80)  # NOTE: Visual separator for terminal output
    logger.info(f"🚀 STARTING LOCAL FILE PROCESSING")
    logger.info(f"📁 Input directory:  {INPUT_DIR}")
    logger.info(f"📁 Output directory: {OUTPUT_DIR}")
    print("="*80)
    
    # NOTE: Check dependencies first
    if not check_dependencies():
        logger.error("❌ Missing required dependencies. Please install ImageMagick, Potrace, svgcleaner, and svgo")
        return False
    
    print("="*80)
    
    # NOTE: Find all PNG files in input directory
    png_pattern = str(INPUT_DIR / "*.png")
    png_files = glob.glob(png_pattern)
    
    if not png_files:
        logger.warning(f"❌ No PNG files found in {INPUT_DIR}")
        print("="*80)
        return False
    
    # NOTE: Sort files for consistent processing order
    png_files.sort()
    total_files = len(png_files)
    logger.info(f"📊 Found {total_files} PNG files to process")
    print("="*80)
    
    processed_count = 0
    failed_count = 0
    skipped_count = 0
    
    # NOTE: Process each file individually with detailed progress tracking
    for index, png_file_path in enumerate(png_files, 1):
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
            result_path = convert_png_to_svg_local(png_path, output_svg_path)
            
            if result_path and result_path.exists():
                processed_count += 1
                final_size = result_path.stat().st_size
                logger.info(f"   ✅ COMPLETED: {png_path.name} -> {svg_filename} ({final_size} bytes)")
                logger.info(f"   📊 Progress: {index}/{total_files} files processed ({processed_count} successful)")
            else:
                failed_count += 1
                logger.error(f"   ❌ FAILED: Could not process {png_path.name}")
                logger.info(f"   📊 Progress: {index}/{total_files} files processed ({failed_count} failed)")
                
        except Exception as e:
            failed_count += 1
            logger.error(f"   ❌ ERROR processing {png_file_path}: {str(e)}")
            logger.info(f"   📊 Progress: {index}/{total_files} files processed ({failed_count} failed)")
    
    # NOTE: Final summary with clear visual separation
    print()
    print("="*80)
    logger.info(f"🏁 LOCAL FILE PROCESSING COMPLETED!")
    logger.info(f"📊 FINAL RESULTS:")
    logger.info(f"   ✅ Processed successfully: {processed_count} files")
    logger.info(f"   ⭐️  Skipped (already exist): {skipped_count} files") 
    logger.info(f"   ❌ Failed: {failed_count} files")
    logger.info(f"   📁 Total files found: {total_files}")
    
    if processed_count > 0:
        logger.info(f"🎉 {processed_count} new SVG files created in {OUTPUT_DIR}")
    
    print("="*80)
    return True

if __name__ == "__main__":
    print("\n" + "="*80)
    print("🗂️  FLOOR MAP PROCESSING SERVICE - LOCAL FILE CONVERTER")
    print("="*80)
    logger.info("🚀 Starting local PNG to SVG conversion...")
    logger.info(f"🐍 Python script: {__file__}")
    logger.info(f"⚙️  Mode: Local PNG to SVG conversion")
    
    # NOTE: Verify required directories exist before processing
    if not INPUT_DIR.exists():
        logger.error(f"❌ Input directory does not exist: {INPUT_DIR}")
        print("="*80 + "\n")
        exit(1)
    
    if not OUTPUT_DIR.exists():
        logger.info(f"📁 Creating output directory: {OUTPUT_DIR}")
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    
    # NOTE: Start the local file processing
    success = process_local_files()
    
    if success:
        logger.info("✅ Script completed successfully!")
    else:
        logger.error("❌ Script completed with errors!")
        exit(1)
    
    print()  # NOTE: Final newline for clean terminal output
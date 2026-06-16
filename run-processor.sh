#!/bin/bash
# NOTE: Script to run the isolated PNG processor

# Configuration
PNG_INPUT_DIR="/home/sclera/images/png_floors"
SVG_OUTPUT_DIR="/home/sclera/images/svg_floors"
IMAGE_NAME="png-processor:latest"

# Optional: Disable SVGO optimization (uncomment to disable)
export ENABLE_SVGO=false

# Optional: Default conversion engine (override with CONVERSION_ENGINE environment variable)
export CONVERSION_ENGINE="${CONVERSION_ENGINE:-potrace}"

echo "PNG to SVG Processing - Isolated Container"
echo "Input directory: $PNG_INPUT_DIR"
echo "Output directory: $SVG_OUTPUT_DIR"
echo "Conversion engine: $CONVERSION_ENGINE"
echo ""

# Create directories if they don't exist
mkdir -p "$PNG_INPUT_DIR" "$SVG_OUTPUT_DIR"

# Check if PNG files exist
PNG_COUNT=$(find "$PNG_INPUT_DIR" -name "*.png" -type f 2>/dev/null | wc -l)

if [ "$PNG_COUNT" -eq 0 ]; then
    echo "No PNG files found in $PNG_INPUT_DIR"
    echo ""
    echo "To add files:"
    echo "1. Copy PNG files: cp /path/to/your/files/*.png $PNG_INPUT_DIR"
    echo "2. Run this script again: $0"
    echo ""
    echo "Example:"
    echo "  cp ~/Desktop/*.png $PNG_INPUT_DIR"
    echo "  $0"
    exit 1
fi

echo "Found $PNG_COUNT PNG files to process"

# Show current production status (should be unchanged)
echo ""
echo "Production containers (remain unchanged):"
docker ps --format "table {{.Names}}\t{{.Status}}"
echo ""

echo "Starting PNG processing container..."
echo "This is a temporary container that will be removed when done."
echo "SVGO Optimization: ${ENABLE_SVGO}"
echo ""

# Run the processing container
docker run --rm \
    --name "png-processor-$(date +%s)" \
    -v "$PNG_INPUT_DIR":/home/sclera/images/png_floors \
    -v "$SVG_OUTPUT_DIR":/home/sclera/images/svg_floors \
    -e ENABLE_SVGO="$ENABLE_SVGO" \
    -e CONVERSION_ENGINE="$CONVERSION_ENGINE" \
    "$IMAGE_NAME" "$@"

# Check results
if [ $? -eq 0 ]; then
    echo ""
    echo "Processing completed successfully!"
    
    # Count and show results
    SVG_COUNT=$(find "$SVG_OUTPUT_DIR" -name "*.svg" -type f 2>/dev/null | wc -l)
    echo "SVG files created: $SVG_COUNT"
    
    if [ "$SVG_COUNT" -gt 0 ]; then
        echo ""
        echo "Generated files:"
        ls -la "$SVG_OUTPUT_DIR"/*.svg | head -10
        if [ "$SVG_COUNT" -gt 10 ]; then
            echo "... and $((SVG_COUNT - 10)) more files"
        fi
        
        echo ""
        echo "Files are ready in: $SVG_OUTPUT_DIR"
    fi
else
    echo ""
    echo "Processing failed. Check the error messages above."
    exit 1
fi

echo ""
echo "Production containers remain unchanged:"
docker ps --format "table {{.Names}}\t{{.Status}}"
#!/bin/bash
# NOTE: Script to run the PNG processor with API filtering

# Configuration
PNG_INPUT_DIR="/home/sclera/images/png_floors"
SVG_OUTPUT_DIR="/home/sclera/images/svg_floors"
IMAGE_NAME="png-processor:latest"

# API Endpoint Configuration
# You can override this by setting API_ENDPOINT environment variable before running
API_ENDPOINT="${API_ENDPOINT:-http://localhost:8082/api/v1/mapservice/convert}"

# Optional: Disable SVGO optimization (uncomment to disable)
export ENABLE_SVGO=false

echo "================================================================================"
echo "PNG to SVG Processing - API-Filtered Mode"
echo "================================================================================"
echo "Input directory:  $PNG_INPUT_DIR"
echo "Output directory: $SVG_OUTPUT_DIR"
echo "API Endpoint:     $API_ENDPOINT"
echo "================================================================================"
echo ""

# Create directories if they don't exist
mkdir -p "$PNG_INPUT_DIR" "$SVG_OUTPUT_DIR"

# Check if PNG files exist
PNG_COUNT=$(find "$PNG_INPUT_DIR" -name "*.png" -type f 2>/dev/null | wc -l)

if [ "$PNG_COUNT" -eq 0 ]; then
    echo "⚠️  No PNG files found in $PNG_INPUT_DIR"
    echo ""
    echo "To add files:"
    echo "1. Copy PNG files: cp /path/to/your/files/*.png $PNG_INPUT_DIR"
    echo "2. Run this script again: $0"
    echo ""
    exit 1
fi

echo "📊 Found $PNG_COUNT PNG files in directory"
echo "🌐 Will fetch list from API and process only matching files"
echo ""

# Show current production status (should be unchanged)
echo "Production containers (remain unchanged):"
docker ps --format "table {{.Names}}\t{{.Status}}" 2>/dev/null || echo "No containers running"
echo ""

echo "🚀 Starting PNG processing container with API filtering..."
echo "   (This is a temporary container that will be removed when done)"
echo "   SVGO Optimization: ${ENABLE_SVGO}"
echo ""

# Run the processing container with API_ENDPOINT environment variable
docker run --rm \
    --name "png-processor-api-$(date +%s)" \
    -e API_ENDPOINT="$API_ENDPOINT" \
    -e ENABLE_SVGO="$ENABLE_SVGO" \
    -v "$PNG_INPUT_DIR":/home/sclera/images/png_floors \
    -v "$SVG_OUTPUT_DIR":/home/sclera/images/svg_floors \
    "$IMAGE_NAME"

# Check results
if [ $? -eq 0 ]; then
    echo ""
    echo "✅ Processing completed successfully!"
    
    # Count and show results
    SVG_COUNT=$(find "$SVG_OUTPUT_DIR" -name "*.svg" -type f 2>/dev/null | wc -l)
    echo "📊 Total SVG files in output directory: $SVG_COUNT"
    
    if [ "$SVG_COUNT" -gt 0 ]; then
        echo ""
        echo "📁 Generated files:"
        ls -lh "$SVG_OUTPUT_DIR"/*.svg 2>/dev/null | tail -10
        if [ "$SVG_COUNT" -gt 10 ]; then
            echo "   ... showing last 10 of $SVG_COUNT files"
        fi
        
        echo ""
        echo "✨ Files are ready in: $SVG_OUTPUT_DIR"
    fi
else
    echo ""
    echo "❌ Processing failed. Check the error messages above."
    exit 1
fi

echo ""
echo "Production containers remain unchanged:"
docker ps --format "table {{.Names}}\t{{.Status}}" 2>/dev/null || echo "No containers running"
echo ""


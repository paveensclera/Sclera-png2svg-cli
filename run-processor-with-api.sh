#!/bin/bash
# NOTE: Script to run the PNG processor with API filtering

# Configuration
PNG_INPUT_DIR="/home/sclera/images/png_floors"
SVG_OUTPUT_DIR="/home/sclera/images/svg_floors"
IMAGE_NAME="png-processor:latest"

# Load environment files if they exist (single source of truth)
load_env() {
    local env_file="$1"
    if [ -f "$env_file" ]; then
        while IFS= read -r line || [ -n "$line" ]; do
            [[ "$line" =~ ^[[:space:]]*# ]] && continue
            [[ -z "${line//[[:space:]]/}" ]] && continue
            if [[ "$line" =~ ^[[:space:]]*([^=[:space:]]+)[[:space:]]*=[[:space:]]*(.*)$ ]]; then
                key="${BASH_REMATCH[1]}"
                val="${BASH_REMATCH[2]}"
                val="${val#\"}"
                val="${val%\"}"
                val="${val#\'}"
                val="${val%\'}"
                export "$key"="$val"
            fi
        done < "$env_file"
    fi
}
load_env ".env"

# API Endpoint & Base URL Configuration
export BASE_URL="${BASE_URL:-http://host.docker.internal:8082/api/v1/migration}"
export API_ENDPOINT="${API_ENDPOINT:-${BASE_URL}/convert}"

# Optional: Enable/Disable SVGO optimization
export ENABLE_SVGO="${ENABLE_SVGO:-true}"
export CONVERSION_ENGINE="${CONVERSION_ENGINE:-potrace}"
export AUTO_DETECT_ENGINE="${AUTO_DETECT_ENGINE:-false}"


echo "================================================================================"
echo "PNG to SVG Processing - API-Filtered Mode"
echo "================================================================================"
echo "Input directory:  $PNG_INPUT_DIR"
echo "Output directory: $SVG_OUTPUT_DIR"
echo "API Endpoint:     $API_ENDPOINT"
echo "Conversion engine: $CONVERSION_ENGINE"
echo "Auto-detect engine: $AUTO_DETECT_ENGINE"
echo "================================================================================"
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
    exit 1
fi

echo "Found $PNG_COUNT PNG files in directory"
echo "Will fetch list from API and process only matching files"
echo ""

# Show current production status (should be unchanged)
echo "Production containers (remain unchanged):"
docker ps --format "table {{.Names}}\t{{.Status}}" 2>/dev/null || echo "No containers running"
echo ""

echo "Starting PNG processing container with API filtering..."
echo "   (This is a temporary container that will be removed when done)"
echo "   SVGO Optimization: ${ENABLE_SVGO}"
echo ""

# Run the processing container with API_ENDPOINT environment variable
docker run --rm \
    --cpus "2.0" \
    --memory "4g" \
    --blkio-weight 100 \
    --oom-score-adj 1000 \
    --network sclera_bridge \
    --name "png-processor-api-$(date +%s)" \
    -e API_ENDPOINT="$API_ENDPOINT" \
    -e ENABLE_SVGO="$ENABLE_SVGO" \
    -v "$PNG_INPUT_DIR":/home/sclera/images/png_floors \
    -v "$SVG_OUTPUT_DIR":/home/sclera/images/svg_floors \
    -v "$SVG_OUTPUT_DIR":/app/vdms_svg_outputs \
    --add-host=host.docker.internal:host-gateway \
    -e BASE_URL="$BASE_URL" \
    -e CONVERSION_ENGINE="$CONVERSION_ENGINE" \
    -e AUTO_DETECT_ENGINE="$AUTO_DETECT_ENGINE" \
    "$IMAGE_NAME" "$@"

# Check results
if [ $? -eq 0 ]; then
    echo ""
    echo "Processing completed successfully!"
    
    # Count and show results
    SVG_COUNT=$(find "$SVG_OUTPUT_DIR" -name "*.svg" -type f 2>/dev/null | wc -l)
    echo "Total SVG files in output directory: $SVG_COUNT"
    
    if [ "$SVG_COUNT" -gt 0 ]; then
        echo ""
        echo "Generated files:"
        ls -lh "$SVG_OUTPUT_DIR"/*.svg 2>/dev/null | tail -10
        if [ "$SVG_COUNT" -gt 10 ]; then
            echo "   ... showing last 10 of $SVG_COUNT files"
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
docker ps --format "table {{.Names}}\t{{.Status}}" 2>/dev/null || echo "No containers running"
echo ""


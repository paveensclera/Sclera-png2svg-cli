#!/bin/bash
# ==============================================================================
# PNG to SVG Processor - Unified Runner Script
# Supports both Single-Tenant and Multi-Tenant processing modes
# Configurable via .env file or command-line arguments
# ==============================================================================

# Directories & Image Configuration
PNG_INPUT_DIR="${PNG_INPUT_DIR:-/home/sclera/images/png_floors}"
SVG_OUTPUT_DIR="${SVG_OUTPUT_DIR:-/home/sclera/images/svg_floors}"
IMAGE_NAME="${IMAGE_NAME:-png-processor:latest}"

# Load environment files (single source of truth)
load_env() {
    local env_file="$1"
    if [ -f "$env_file" ]; then
        while IFS= read -r line || [ -n "$line" ]; do
            line="${line%$'\r'}"
            [[ "$line" =~ ^[[:space:]]*# ]] && continue
            [[ -z "${line//[[:space:]]/}" ]] && continue
            if [[ "$line" =~ ^[[:space:]]*([^=[:space:]]+)[[:space:]]*=[[:space:]]*(.*)$ ]]; then
                key="${BASH_REMATCH[1]}"
                val="${BASH_REMATCH[2]}"
                val="${val%$'\r'}"
                val="${val#\"}"
                val="${val%\"}"
                val="${val#\'}"
                val="${val%\'}"
                val="${val%$'\r'}"
                export "$key"="$val"
            fi
        done < "$env_file"
    fi
}
load_env ".env"
load_env ".env.local"

# Show help function
show_help() {
    echo "================================================================================"
    echo "PNG to SVG Processor - Usage Guide"
    echo "================================================================================"
    echo "Usage:"
    echo "  $0 [options] [-- any-main.py-args]"
    echo ""
    echo "Options:"
    echo "  -m, --mode <mode>        Execution mode: 'single-tenant' (default) or 'multi-tenant'"
    echo "  -v, --vdms <id(s)>       VDMS filter for multi-tenant (e.g. 'VDMS885' or 'VDMS885,VDMS886')"
    echo "  -e, --engine <engine>    Conversion engine: 'potrace' (default) or 'vtracer'"
    echo "  -a, --auto-detect        Auto-detect engine based on image color (vtracer for color, potrace for B&W)"
    echo "      --disable-ocr        Disable OCR text recognition and removal"
    echo "      --enable-ocr         Enable OCR text recognition and removal"
    echo "      --disable-svgo       Disable SVGO optimization (use svgcleaner only)"
    echo "  -i, --input-dir <dir>    Input directory containing PNG files (default: /home/sclera/images/png_floors)"
    echo "  -o, --output-dir <dir>   Output directory for SVG files (default: /home/sclera/images/svg_floors)"
    echo "  -h, --help               Show this help message"
    echo ""
    echo "Environment Variables (can be set in .env):"
    echo "  MODE                     'single-tenant' or 'multi-tenant'"
    echo "  BASE_URL                 Base API URL for single-tenant (e.g. http://host.docker.internal:8888/api/v1/migration)"
    echo "  MULTI_TENANT_BASEURL     Base cloud URL for multi-tenant (e.g. https://qa-tenant2.sclera.com)"
    echo "  VDMS_FILTER              Filter specific VDMS box(es) (e.g. VDMS885 or VDMS885,VDMS886)"
    echo "  CONVERSION_ENGINE        'potrace' or 'vtracer'"
    echo "  AUTO_DETECT_ENGINE       'true' or 'false'"
    echo "  ENABLE_OCR               'true' or 'false'"
    echo "  ENABLE_SVGO              'true' or 'false'"
    echo ""
    echo "Examples:"
    echo "  # Run single-tenant (reads config from .env):"
    echo "  $0 --mode single-tenant"
    echo ""
    echo "  # Run multi-tenant for specific VDMS:"
    echo "  $0 --mode multi-tenant --vdms VDMS885"
    echo ""
    echo "  # Run multi-tenant for all VDMS with auto-detect engine:"
    echo "  $0 --mode multi-tenant --auto-detect"
    echo "================================================================================"
    exit 0
}

# Parse CLI arguments to override environment variables
PASSTHROUGH_ARGS=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        -h|--help)
            show_help
            ;;
        -m|--mode)
            export MODE="$2"
            shift 2
            ;;
        --mode=*)
            export MODE="${1#*=}"
            shift
            ;;
        -v|--vdms)
            export VDMS_FILTER="$2"
            shift 2
            ;;
        --vdms=*)
            export VDMS_FILTER="${1#*=}"
            shift
            ;;
        -e|--engine)
            export CONVERSION_ENGINE="$2"
            shift 2
            ;;
        --engine=*)
            export CONVERSION_ENGINE="${1#*=}"
            shift
            ;;
        -a|--auto-detect|--auto-detect-engine)
            export AUTO_DETECT_ENGINE="true"
            shift
            ;;
        --disable-ocr|--no-ocr)
            export ENABLE_OCR="false"
            shift
            ;;
        --enable-ocr|--ocr)
            export ENABLE_OCR="true"
            shift
            ;;
        --disable-svgo)
            export ENABLE_SVGO="false"
            shift
            ;;
        -i|--input-dir)
            export PNG_INPUT_DIR="$2"
            shift 2
            ;;
        --input-dir=*)
            export PNG_INPUT_DIR="${1#*=}"
            shift
            ;;
        -o|--output-dir)
            export SVG_OUTPUT_DIR="$2"
            shift 2
            ;;
        --output-dir=*)
            export SVG_OUTPUT_DIR="${1#*=}"
            shift
            ;;
        *)
            PASSTHROUGH_ARGS+=("$1")
            shift
            ;;
    esac
done

# Normalize MODE (defaults to single-tenant if not multi-tenant)
MODE_LOWER="$(echo "${MODE:-single-tenant}" | tr '[:upper:]' '[:lower:]' | tr -d '[:space:]')"
if [[ "$MODE_LOWER" =~ ^(multi-tenant|multitenant|multi_tenant|multi)$ ]]; then
    export MODE="multi-tenant"
else
    export MODE="single-tenant"
fi

# Environment & URL Configuration Defaults
export BASE_URL="${BASE_URL:-http://host.docker.internal:8888/api/v1/migration}"
export MULTI_TENANT_BASEURL="${MULTI_TENANT_BASEURL:-https://tenant.com}"
export API_ENDPOINT="${API_ENDPOINT:-${BASE_URL}/convert}"
export ENABLE_SVGO="${ENABLE_SVGO:-true}"
export ENABLE_OCR="${ENABLE_OCR:-true}"
export CONVERSION_ENGINE="${CONVERSION_ENGINE:-potrace}"
export AUTO_DETECT_ENGINE="${AUTO_DETECT_ENGINE:-false}"
export OCR_ENGINE="${OCR_ENGINE:-onnxruntime}"
export VDMS_FILTER="${VDMS_FILTER:-}"

echo "================================================================================"
if [ "$MODE" = "multi-tenant" ]; then
    echo "PNG to SVG Processing - Multi-Tenant Cloud Mode"
else
    echo "PNG to SVG Processing - Single-Tenant Mode"
fi
echo "================================================================================"
echo "Mode:              $MODE"
if [ "$MODE" = "multi-tenant" ]; then
    echo "Multi-Tenant URL:  $MULTI_TENANT_BASEURL"
    if [ -n "$VDMS_FILTER" ]; then
        echo "VDMS Filter:       $VDMS_FILTER"
    else
        echo "VDMS Filter:       All VDMS"
    fi
else
    echo "Single-Tenant URL: $BASE_URL"
    echo "API Endpoint:      $API_ENDPOINT"
    echo "Input directory:   $PNG_INPUT_DIR"
fi
echo "Output directory:  $SVG_OUTPUT_DIR"
echo "Conversion engine: $CONVERSION_ENGINE"
echo "Auto-detect engine: $AUTO_DETECT_ENGINE"
echo "OCR Processing:    $ENABLE_OCR"
echo "SVGO Optimization: $ENABLE_SVGO"
echo "================================================================================"
echo ""

# Ensure output directory exists
mkdir -p "$SVG_OUTPUT_DIR"

# Mode-specific input verification
if [ "$MODE" != "multi-tenant" ]; then
    mkdir -p "$PNG_INPUT_DIR"
    PNG_COUNT=$(find "$PNG_INPUT_DIR" -name "*.png" -type f 2>/dev/null | wc -l)

    if [ "$PNG_COUNT" -eq 0 ]; then
        echo "No PNG files found in $PNG_INPUT_DIR"
        echo ""
        echo "To add files:"
        echo "1. Copy PNG files: cp /path/to/your/files/*.png $PNG_INPUT_DIR"
        echo "2. Run this script again: $0"
        echo ""
        echo "Tip: If you want to run in Multi-Tenant Cloud Mode instead:"
        echo "     $0 --mode multi-tenant"
        echo ""
        exit 1
    fi

    echo "Found $PNG_COUNT PNG files in input directory"
    echo "Will fetch list from API and process matching files"
    echo ""
else
    echo "Running in multi-tenant cloud mode. Floor maps will be fetched directly from API."
    echo ""
fi

# Check Docker
if ! docker ps >/dev/null 2>&1; then
    echo "Error: Docker daemon is not accessible. Please ensure Docker is running."
    exit 1
fi

# Determine Docker Network Argument
NETWORK_ARG=""
if [ "$MODE" != "multi-tenant" ]; then
    if docker network inspect sclera_bridge >/dev/null 2>&1; then
        NETWORK_ARG="--network sclera_bridge"
    fi
fi

# Build arguments array for container
CONTAINER_ARGS=()
CONTAINER_ARGS+=(--mode "$MODE")
CONTAINER_ARGS+=(--engine "$CONVERSION_ENGINE")
if [ "$AUTO_DETECT_ENGINE" = "true" ]; then
    CONTAINER_ARGS+=(--auto-detect)
fi
if [ "$ENABLE_OCR" = "false" ]; then
    CONTAINER_ARGS+=(--disable-ocr)
elif [ "$ENABLE_OCR" = "true" ]; then
    CONTAINER_ARGS+=(--enable-ocr)
fi
if [ "$ENABLE_SVGO" = "false" ]; then
    CONTAINER_ARGS+=(--disable-svgo)
fi
if [ -n "$VDMS_FILTER" ]; then
    CONTAINER_ARGS+=(--vdms "$VDMS_FILTER")
fi
CONTAINER_ARGS+=("${PASSTHROUGH_ARGS[@]}")

# Run the processing container
echo "Starting PNG processing container..."
docker run --rm \
    $NETWORK_ARG \
    --cpus "2.0" \
    --memory "4g" \
    --name "png-processor-$(date +%s)" \
    -e MODE="$MODE" \
    -e MULTI_TENANT_BASEURL="$MULTI_TENANT_BASEURL" \
    -e API_ENDPOINT="$API_ENDPOINT" \
    -e BASE_URL="$BASE_URL" \
    -e VDMS_FILTER="$VDMS_FILTER" \
    -e ENABLE_SVGO="$ENABLE_SVGO" \
    -e ENABLE_OCR="$ENABLE_OCR" \
    -e CONVERSION_ENGINE="$CONVERSION_ENGINE" \
    -e AUTO_DETECT_ENGINE="$AUTO_DETECT_ENGINE" \
    -e OCR_ENGINE="$OCR_ENGINE" \
    -v "$PNG_INPUT_DIR":/home/sclera/images/png_floors \
    -v "$SVG_OUTPUT_DIR":/home/sclera/images/svg_floors \
    -v "$SVG_OUTPUT_DIR":/app/vdms_svg_outputs \
    --add-host=host.docker.internal:host-gateway \
    "$IMAGE_NAME" \
    "${CONTAINER_ARGS[@]}"

# Check results
EXIT_CODE=$?
if [ $EXIT_CODE -eq 0 ]; then
    echo ""
    echo "Processing completed successfully!"
    
    SVG_COUNT=$(find "$SVG_OUTPUT_DIR" -name "*.svg" -type f 2>/dev/null | wc -l)
    echo "Total SVG files in output directory: $SVG_COUNT"
    
    if [ "$SVG_COUNT" -gt 0 ]; then
        echo ""
        echo "Recent generated files:"
        ls -lh "$SVG_OUTPUT_DIR"/*.svg 2>/dev/null | tail -10
        if [ "$SVG_COUNT" -gt 10 ]; then
            echo "   ... showing last 10 of $SVG_COUNT files"
        fi
        
        echo ""
        echo "Files are ready in: $SVG_OUTPUT_DIR"
    fi
else
    echo ""
    echo "Processing failed with exit code $EXIT_CODE. Check the error messages above."
    exit $EXIT_CODE
fi

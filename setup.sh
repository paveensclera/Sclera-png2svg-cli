#!/bin/bash
# NOTE: Complete setup script for isolated PNG processor

echo "================================================================================"
echo "PNG to SVG Processor Setup"
echo "================================================================================"
echo "This creates an isolated container app that won't affect your production environment."
echo ""

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
load_env ".env.local"

# Check and decide OCR_ENGINE based on environment
OCR_ENGINE=$(echo "${OCR_ENGINE:-${OCR_ENGINE_TYPE:-onnxruntime}}" | tr '[:upper:]' '[:lower:]' | xargs)

if [ "$OCR_ENGINE" != "openvino" ] && [ "$OCR_ENGINE" != "onnxruntime" ]; then
    echo "⚠️  Invalid OCR_ENGINE='$OCR_ENGINE'. Supported options: 'openvino' or 'onnxruntime'."
    echo "    Defaulting OCR_ENGINE to 'onnxruntime' to minimize image size."
    OCR_ENGINE="onnxruntime"
fi

echo "OCR Engine Configuration:"
echo "  • Engine: $OCR_ENGINE"
if [ "$OCR_ENGINE" = "openvino" ]; then
    echo "  • Library: openvino (optimized inference)"
else
    echo "  • Library: onnxruntime (reduced build size)"
fi
echo ""

# Show current production status (should remain unchanged)
echo "Current production containers (will remain untouched):"
docker ps --format "table {{.Names}}\t{{.Image}}\t{{.Status}}" 2>/dev/null || echo "Docker daemon not running or no containers"
echo ""

# Build the isolated PNG processor image with selected OCR_ENGINE
echo "Building isolated PNG processor image (OCR_ENGINE=${OCR_ENGINE})..."
docker build --build-arg OCR_ENGINE="${OCR_ENGINE}" -t png-processor:latest .

if [ $? -ne 0 ]; then
    echo ""
    echo "Build failed! Check the error messages above."
    exit 1
fi

echo ""
echo "Build completed successfully!"

# Test the image dependencies
echo ""
echo "Testing container dependencies and OCR engine..."
docker run --rm --entrypoint python png-processor:latest -c "
import subprocess
import sys

def test_tool(cmd, name):
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            print(f'✓ {name}: Available')
            return True
        else:
            print(f'✗ {name}: Error - {result.stderr.strip()}')
            return False
    except Exception as e:
        print(f'✗ {name}: Failed - {str(e)}')
        return False

print('Testing system dependencies...')
success = True
success &= test_tool(['python', '--version'], 'Python')
success &= test_tool(['convert', '-version'], 'ImageMagick')
success &= test_tool(['potrace', '--version'], 'Potrace')
success &= test_tool(['vtracer', '--help'], 'VTracer')
success &= test_tool(['svgcleaner', '--version'], 'svgcleaner')
success &= test_tool(['svgo', '--version'], 'svgo')

# Test Python OCR engine initialization
try:
    import os
    from utils.ocr import init_ocr_engine
    engine = init_ocr_engine()
    engine_name = os.getenv('OCR_ENGINE', 'onnxruntime')
    print(f'✓ RapidOCR ({engine_name}): Initialized successfully')
except Exception as e:
    print(f'✗ RapidOCR: Initialization failed - {str(e)}')
    success = False

if success:
    print('\nAll dependencies and OCR engine are working correctly!')
    sys.exit(0)
else:
    print('\nSome dependencies failed!')
    sys.exit(1)
"

if [ $? -eq 0 ]; then
    echo ""
    echo "================================================================================"
    echo "Setup completed successfully!"
    echo "================================================================================"
    echo ""
    echo "Your isolated PNG processor is ready to use."
    echo "Production containers remain unchanged:"
    docker ps --format "table {{.Names}}\t{{.Status}}" 2>/dev/null || echo "No containers running"
    echo ""
    echo "Next steps:"
    echo "1. Configure mode in .env or via CLI flags (single-tenant or multi-tenant)"
    echo "2. Run processor:"
    echo "   - Single-Tenant: ./run-processor-with-api.sh --mode single-tenant"
    echo "   - Multi-Tenant:  ./run-processor-with-api.sh --mode multi-tenant --vdms <VDMS_ID>"
    echo "   - Monitored:     ./run-conversion-with-monitoring.sh"
    echo "3. Check results in: /home/sclera/images/svg_floors/"
    echo ""
    echo "Engine Configuration (.env or CLI flags):"
    echo "  - OCR_ENGINE=onnxruntime    (default, reduced build size) or openvino"
    echo "  - CONVERSION_ENGINE=potrace (default, B&W plans) or vtracer (color/complex)"
    echo "  - AUTO_DETECT_ENGINE=true   (auto-picks vtracer for color, potrace for B&W)"
    echo ""
    echo "Example rebuild with OpenVINO:"
    echo "  OCR_ENGINE=openvino ./setup.sh"
else
    echo ""
    echo "Setup failed! Please check the error messages above."
    exit 1
fi


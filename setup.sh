#!/bin/bash
# NOTE: Complete setup script for isolated PNG processor

echo "PNG to SVG Processor Setup"
echo "This creates an isolated app that won't affect your production environment"
echo ""

# Show current production status (should remain unchanged)
echo "Current production containers (will remain untouched):"
docker ps --format "table {{.Names}}\t{{.Image}}\t{{.Status}}"
echo ""

# Build the isolated PNG processor image
echo "Building isolated PNG processor..."
docker build -t png-processor:latest .

if [ $? -ne 0 ]; then
    echo "Build failed! Check the error messages above."
    exit 1
fi

echo ""
echo "Build completed successfully!"

# Test the image
echo ""
echo "Testing the container dependencies..."
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

print('Testing dependencies...')
success = True
success &= test_tool(['python', '--version'], 'Python')
success &= test_tool(['convert', '-version'], 'ImageMagick')
success &= test_tool(['potrace', '--version'], 'Potrace')
success &= test_tool(['vtracer', '--help'], 'VTracer')
success &= test_tool(['svgcleaner', '--version'], 'svgcleaner')
success &= test_tool(['svgo', '--version'], 'svgo')

if success:
    print('\\nAll dependencies are working correctly!')
    sys.exit(0)
else:
    print('\\nSome dependencies failed!')
    sys.exit(1)
"

if [ $? -eq 0 ]; then
    echo ""
    echo "Setup completed successfully!"
    echo ""
    echo "Your isolated PNG processor is ready to use."
    echo "Production containers remain unchanged:"
    docker ps --format "table {{.Names}}\t{{.Status}}"
    echo ""
    echo "Next steps:"
    echo "1. Put PNG files in: /home/sclera/images/png_floors/"
    echo "2. Run: ./run-processor.sh"
    echo "3. Check results in: /home/sclera/images/svg_floors/"
else
    echo ""
    echo "Setup failed! Please check the error messages above."
    exit 1
fi
#!/bin/bash
# Script to rebuild the Docker image with the new ImageMagick policy and test it

set -e

echo "🔧 Rebuilding PNG processor with enhanced ImageMagick policy..."
echo ""

# Build the Docker image
echo "📦 Building Docker image..."
docker build -t png-processor:latest .

if [ $? -eq 0 ]; then
    echo "✅ Docker image built successfully!"
    echo ""
    
    # Test the policy is correctly applied
    echo "🔍 Testing ImageMagick policy configuration..."
    docker run --rm png-processor:latest bash -c "
        echo 'ImageMagick version:'
        convert -version | head -3
        echo ''
        echo 'Policy file location and contents:'
        for path in /etc/ImageMagick-7/policy.xml /etc/ImageMagick/policy.xml /usr/local/etc/ImageMagick-7/policy.xml; do
            if [ -f \"\$path\" ]; then
                echo \"Found policy at: \$path\"
                echo \"Memory limit: \$(grep 'name=\"memory\"' \"\$path\" || echo 'Not found')\"
                echo \"Map limit: \$(grep 'name=\"map\"' \"\$path\" || echo 'Not found')\"
                echo \"Area limit: \$(grep 'name=\"area\"' \"\$path\" || echo 'Not found')\"
                echo \"Disk limit: \$(grep 'name=\"disk\"' \"\$path\" || echo 'Not found')\"
                break
            fi
        done
    "
    
    echo ""
    echo "✅ Build completed! You can now run your PNG processing:"
    echo ""
    echo "   ./run-processor.sh"
    echo ""
    echo "📋 Changes made:"
    echo "   • Increased memory limit from 2GiB to 8GiB"
    echo "   • Increased map limit from 2GiB to 8GiB" 
    echo "   • Increased area limit from 64MP to 256MP"
    echo "   • Increased disk limit from 2GiB to 8GiB"
    echo "   • Added width/height limits of 64KP each"
    echo "   • Explicitly allowed PNG and PBM formats"
    echo "   • Added path permissions for temp directories"
    echo ""
    echo "🎯 This should resolve the 'cache resources exhausted' errors"
    echo "   for your large PNG files (28MB+ files should now work)."
    
else
    echo "❌ Docker build failed! Please check the error messages above."
    exit 1
fi

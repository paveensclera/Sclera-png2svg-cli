#!/bin/bash
# NOTE: Cleanup script to remove PNG processor resources

echo "PNG Processor Cleanup"
echo "This will remove the PNG processor image and containers (production remains safe)"
echo ""

# Show current containers
echo "Current containers:"
docker ps -a --format "table {{.Names}}\t{{.Image}}\t{{.Status}}"
echo ""

# Remove any stopped PNG processor containers
echo "Removing PNG processor containers..."
docker ps -a --filter "name=png-processor" --format "{{.Names}}" | while read container; do
    if [ -n "$container" ]; then
        echo "Removing container: $container"
        docker rm "$container" 2>/dev/null || echo "Container $container already removed"
    fi
done

# Remove PNG processor image
echo ""
echo "Removing PNG processor image..."
if docker images png-processor:latest --format "{{.Repository}}" | grep -q "png-processor"; then
    docker rmi png-processor:latest
    echo "PNG processor image removed"
else
    echo "PNG processor image not found (already removed)"
fi

echo ""
echo "Cleanup completed!"
echo ""
echo "Production containers remain untouched:"
docker ps --format "table {{.Names}}\t{{.Image}}\t{{.Status}}"
echo ""
echo "To rebuild the PNG processor:"
echo "  ./setup.sh"
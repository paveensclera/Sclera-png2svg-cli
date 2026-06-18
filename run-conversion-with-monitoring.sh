#!/bin/bash
# PNG to SVG Conversion with Real-Time Resource Monitoring
# No additional packages required - uses Docker stats API

set -e

# Configuration
PNG_INPUT_DIR="${PNG_INPUT_DIR:-/home/sclera/images/png_floors}"
SVG_OUTPUT_DIR="${SVG_OUTPUT_DIR:-/home/sclera/images/svg_floors}"
IMAGE_NAME="${IMAGE_NAME:-png-processor:latest}"
API_ENDPOINT="${API_ENDPOINT:-http://localhost:8092/api/v1/migration/convert}"
ENABLE_SVGO="${ENABLE_SVGO:-false}"
CONVERSION_ENGINE="${CONVERSION_ENGINE:-potrace}"


# Monitoring configuration
MONITOR_INTERVAL=1  # seconds
STATS_FILE="/tmp/resource_stats_$$.txt"
MONITOR_PID=""

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

# Cleanup on exit
cleanup() {
    if [ -n "$MONITOR_PID" ] && ps -p $MONITOR_PID > /dev/null 2>&1; then
        kill $MONITOR_PID 2>/dev/null || true
    fi
    rm -f "$STATS_FILE" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

# Print header
print_header() {
    clear
    echo -e "${BLUE}================================================================================${NC}"
    echo -e "${BOLD}PNG to SVG Conversion with Resource Monitoring${NC}"
    echo -e "${BLUE}================================================================================${NC}"
    echo -e "Input directory:  ${CYAN}$PNG_INPUT_DIR${NC}"
    echo -e "Output directory: ${CYAN}$SVG_OUTPUT_DIR${NC}"
    echo -e "Docker image:     ${CYAN}$IMAGE_NAME${NC}"
    if [ -n "$API_ENDPOINT" ]; then
        echo -e "API Endpoint:     ${CYAN}$API_ENDPOINT${NC}"
        echo -e "Mode:             ${YELLOW}API-filtered processing${NC}"
    else
        echo -e "Mode:             ${YELLOW}Process all files${NC}"
    fi
    echo -e "SVGO Optimization: ${CYAN}$ENABLE_SVGO${NC}"
    echo -e "Conversion engine: ${CYAN}$CONVERSION_ENGINE${NC}"
    echo -e "${BLUE}================================================================================${NC}"
    echo ""
}

# Check prerequisites
check_prerequisites() {
    echo -e "${YELLOW}🔍 Checking prerequisites...${NC}"
    
    # Check directories
    if [ ! -d "$PNG_INPUT_DIR" ]; then
        echo -e "${RED}❌ Input directory does not exist: $PNG_INPUT_DIR${NC}"
        exit 1
    fi
    
    # Create output directory
    mkdir -p "$SVG_OUTPUT_DIR" 2>/dev/null || true
    
    # Check for PNG files
    PNG_COUNT=$(find "$PNG_INPUT_DIR" -name "*.png" -type f 2>/dev/null | wc -l)
    if [ "$PNG_COUNT" -eq 0 ]; then
        echo -e "${RED}❌ No PNG files found in $PNG_INPUT_DIR${NC}"
        exit 1
    fi
    echo -e "${GREEN}✅ Found $PNG_COUNT PNG files${NC}"
    
    # Check Docker
    if ! docker ps >/dev/null 2>&1; then
        echo -e "${RED}❌ Docker is not accessible${NC}"
        exit 1
    fi
    echo -e "${GREEN}✅ Docker is accessible${NC}"
    
    # Check if image exists
    if ! docker images --format "{{.Repository}}:{{.Tag}}" | grep -q "^${IMAGE_NAME}$"; then
        echo -e "${RED}❌ Docker image not found: $IMAGE_NAME${NC}"
        echo -e "${YELLOW}   Build it with: docker build -t $IMAGE_NAME .${NC}"
        exit 1
    fi
    echo -e "${GREEN}✅ Docker image found: $IMAGE_NAME${NC}"
    
    echo ""
}

# Fetch API filenames
fetch_api_filenames() {
    if [ -z "$API_ENDPOINT" ]; then
        return 0
    fi
    
    echo -e "${YELLOW}🌐 Fetching file list from API: $API_ENDPOINT${NC}"
    
    if command -v curl >/dev/null 2>&1; then
        local response=$(curl -s "$API_ENDPOINT" 2>/dev/null)
        
        if [ $? -eq 0 ] && [ -n "$response" ]; then
            echo -e "${GREEN}✅ API request successful${NC}"
            echo ""
            
            # Extract and display all filenames from URLs
            echo -e "${CYAN}📋 Files to process from API:${NC}"
            echo "$response" | grep -o '"[^"]*\.png"' | sed 's/"//g' | while IFS= read -r url; do
                filename=$(basename "$url")
                echo -e "   • $filename"
            done
        else
            echo -e "${YELLOW}⚠️  API request failed, will process all files${NC}"
        fi
    else
        echo -e "${YELLOW}⚠️  curl not available, skipping API check${NC}"
    fi
    
    echo ""
}

# Show system resources
show_system_resources() {
    echo -e "${YELLOW}💻 System Resources:${NC}"
    
    # CPU cores
    if [ -f /proc/cpuinfo ]; then
        CPU_CORES=$(grep -c ^processor /proc/cpuinfo)
        echo -e "   CPU Cores: ${CYAN}$CPU_CORES${NC}"
    fi
    
    # Memory
    if [ -f /proc/meminfo ]; then
        TOTAL_MEM=$(awk '/MemTotal/ {printf "%.1f", $2/1024/1024}' /proc/meminfo)
        AVAIL_MEM=$(awk '/MemAvailable/ {printf "%.1f", $2/1024/1024}' /proc/meminfo)
        echo -e "   Total RAM: ${CYAN}${TOTAL_MEM} GB${NC}"
        echo -e "   Available RAM: ${CYAN}${AVAIL_MEM} GB${NC}"
    fi
    
    echo ""
}

# Monitor container resources silently (collect data only)
# Convert a raw docker stats value (e.g. "1.22MB", "450B", "2.1GB") to MB
to_mb() {
    local val=$(echo "$1" | sed 's/[^0-9.]//g')
    local unit=$(echo "$1" | sed 's/[0-9.]//g' | tr '[:lower:]' '[:upper:]')
    
    if [[ -z "$val" ]]; then
        echo "0.00"
        return
    fi
    
    case "$unit" in
        *GB*|*G*)
            echo "$val" | awk '{printf "%.2f", $1 * 1024}'
            ;;
        *MB*|*M*)
            echo "$val" | awk '{printf "%.2f", $1}'
            ;;
        *KB*|*K*)
            echo "$val" | awk '{printf "%.2f", $1 / 1024}'
            ;;
        *)
            # Bytes
            echo "$val" | awk '{printf "%.2f", $1 / 1024 / 1024}'
            ;;
    esac
}

# Monitor container resources silently (collect data only)
monitor_container() {
    local container_id="$1"
    
    # Initialize stats file
    echo "timestamp,cpu_percent,memory_mb,block_read_mb,block_write_mb,net_in_mb,net_out_mb" > "$STATS_FILE"
    
    while docker ps --filter "id=$container_id" --format "{{.ID}}" | grep -q "$container_id"; do
        # Get Docker stats
        local stats=$(docker stats --no-stream --format "{{.CPUPerc}},{{.MemUsage}},{{.BlockIO}},{{.NetIO}}" "$container_id" 2>/dev/null || echo "0.00%,0B / 0B,0B / 0B,0B / 0B")
        
        # Split fields
        local cpu_raw=$(echo "$stats" | cut -d',' -f1 | sed 's/%//')
        local mem_raw=$(echo "$stats" | cut -d',' -f2 | cut -d'/' -f1 | xargs)
        local block_raw=$(echo "$stats" | cut -d',' -f3)
        local net_raw=$(echo "$stats" | cut -d',' -f4)
        
        # Parse Block I/O
        local block_read_raw=$(echo "$block_raw" | cut -d'/' -f1 | xargs)
        local block_write_raw=$(echo "$block_raw" | cut -d'/' -f2 | xargs)
        
        # Parse Network I/O
        local net_in_raw=$(echo "$net_raw" | cut -d'/' -f1 | xargs)
        local net_out_raw=$(echo "$net_raw" | cut -d'/' -f2 | xargs)
        
        local mem_mb=$(to_mb "$mem_raw")
        local block_read_mb=$(to_mb "$block_read_raw")
        local block_write_mb=$(to_mb "$block_write_raw")
        local net_in_mb=$(to_mb "$net_in_raw")
        local net_out_mb=$(to_mb "$net_out_raw")
        
        # Save to file with current timestamp
        echo "$(date +%s),$cpu_raw,$mem_mb,$block_read_mb,$block_write_mb,$net_in_mb,$net_out_mb" >> "$STATS_FILE"
        
        sleep $MONITOR_INTERVAL
    done
}

# Get current resource usage
get_current_resources() {
    local container_id="$1"
    
    # Get Docker stats
    local stats=$(docker stats --no-stream --format "{{.CPUPerc}},{{.MemUsage}},{{.BlockIO}}" "$container_id" 2>/dev/null || echo "0.00%,0B / 0B,0B / 0B")
    
    # Split fields
    local cpu_raw=$(echo "$stats" | cut -d',' -f1 | sed 's/%//')
    local mem_raw=$(echo "$stats" | cut -d',' -f2 | cut -d'/' -f1 | xargs)
    local block_raw=$(echo "$stats" | cut -d',' -f3)
    
    local block_read_raw=$(echo "$block_raw" | cut -d'/' -f1 | xargs)
    local block_write_raw=$(echo "$block_raw" | cut -d'/' -f2 | xargs)
    
    local mem_mb=$(to_mb "$mem_raw")
    local block_read_mb=$(to_mb "$block_read_raw")
    local block_write_mb=$(to_mb "$block_write_raw")
    
    echo "${cpu_raw},${mem_mb},${block_read_mb},${block_write_mb}"
}

# Calculate and display resource summary
show_resource_summary() {
    if [ ! -f "$STATS_FILE" ] || [ $(wc -l < "$STATS_FILE") -le 1 ]; then
        return
    fi
    
    echo ""
    echo -e "${BLUE}================================================================================${NC}"
    echo -e "${BOLD}📊 Resource Usage Summary${NC}"
    echo -e "${BLUE}================================================================================${NC}"
    echo ""
    
    # Calculate statistics using awk
    local stats=$(awk -F',' 'NR>1 {
        cpu_sum+=$2; cpu_count++;
        if($2>cpu_max) cpu_max=$2;
        if(cpu_min=="" || $2<cpu_min) cpu_min=$2;
        
        mem_sum+=$3; mem_count++;
        if($3>mem_max) mem_max=$3;
        if(mem_min=="" || $3<mem_min) mem_min=$3;
        
        if($4>block_read_max) block_read_max=$4;
        if($5>block_write_max) block_write_max=$5;
        if($6>net_in_max) net_in_max=$6;
        if($7>net_out_max) net_out_max=$7;
        
        if(NR==2) start_time=$1;
        end_time=$1;
    }
    END {
        if(cpu_count>0) {
            cpu_avg=cpu_sum/cpu_count;
            mem_avg=mem_sum/mem_count;
            duration=end_time-start_time;
            printf "%.2f,%.2f,%.2f,%.2f,%.2f,%.2f,%.2f,%.2f,%.2f,%.2f,%.0f", 
                cpu_avg, cpu_max, cpu_min, 
                mem_avg, mem_max, mem_min, 
                block_read_max, block_write_max, 
                net_in_max, net_out_max, 
                duration;
        }
    }' "$STATS_FILE")
    
    if [ -z "$stats" ]; then
        return
    fi
    
    local cpu_avg=$(echo "$stats" | cut -d',' -f1)
    local cpu_max=$(echo "$stats" | cut -d',' -f2)
    local cpu_min=$(echo "$stats" | cut -d',' -f3)
    local mem_avg=$(echo "$stats" | cut -d',' -f4)
    local mem_max=$(echo "$stats" | cut -d',' -f5)
    local mem_min=$(echo "$stats" | cut -d',' -f6)
    local block_read=$(echo "$stats" | cut -d',' -f7)
    local block_write=$(echo "$stats" | cut -d',' -f8)
    local net_in=$(echo "$stats" | cut -d',' -f9)
    local net_out=$(echo "$stats" | cut -d',' -f10)
    local duration=$(echo "$stats" | cut -d',' -f11)
    
    # Display duration
    echo -e "${BOLD}⏱️  Duration: ${CYAN}${duration}${NC} seconds"
    echo ""
    
    # Display CPU stats
    echo -e "${BOLD}💻 CPU Usage:${NC}"
    echo -e "   Average: ${CYAN}${cpu_avg}%${NC}"
    echo -e "   Maximum: ${CYAN}${cpu_max}%${NC}"
    echo -e "   Minimum: ${CYAN}${cpu_min}%${NC}"
    
    # CPU impact assessment
    if (( $(echo "$cpu_avg > 75" | bc -l 2>/dev/null || echo 0) )); then
        echo -e "   Impact:  ${RED}🔴 HIGH - Significant CPU usage${NC}"
    elif (( $(echo "$cpu_avg > 40" | bc -l 2>/dev/null || echo 0) )); then
        echo -e "   Impact:  ${YELLOW}🟡 MODERATE - Noticeable CPU usage${NC}"
    else
        echo -e "   Impact:  ${GREEN}🟢 LOW - Minimal CPU usage${NC}"
    fi
    
    echo ""
    
    # Display memory stats
    echo -e "${BOLD}🧠 Memory Usage:${NC}"
    echo -e "   Average: ${CYAN}${mem_avg} MB${NC}"
    echo -e "   Maximum: ${CYAN}${mem_max} MB${NC}"
    echo -e "   Minimum: ${CYAN}${mem_min} MB${NC}"
    
    # Memory impact assessment (mem_avg is in MB)
    if (( $(echo "$mem_avg > 2048" | bc -l 2>/dev/null || echo 0) )); then
        echo -e "   Impact:  ${RED}🔴 HIGH - Significant memory usage${NC}"
    elif (( $(echo "$mem_avg > 1024" | bc -l 2>/dev/null || echo 0) )); then
        echo -e "   Impact:  ${YELLOW}🟡 MODERATE - Noticeable memory usage${NC}"
    else
        echo -e "   Impact:  ${GREEN}🟢 LOW - Minimal memory usage${NC}"
    fi
    
    echo ""
    
    # Display Disk I/O volume
    echo -e "${BOLD}💾 Disk I/O Volume:${NC}"
    echo -e "   Total Read:  ${CYAN}${block_read} MB${NC}"
    echo -e "   Total Write: ${CYAN}${block_write} MB${NC}"
    
    echo ""
    
    # Display Network volume
    echo -e "${BOLD}🌐 Network Volume:${NC}"
    echo -e "   Total Received: ${CYAN}${net_in} MB${NC}"
    echo -e "   Total Sent:     ${CYAN}${net_out} MB${NC}"
    
    echo ""
    echo -e "${BLUE}================================================================================${NC}"
    echo ""
}

# Check and display results
check_results() {
    echo -e "${BLUE}================================================================================${NC}"
    echo -e "${BOLD}✅ Conversion Results${NC}"
    echo -e "${BLUE}================================================================================${NC}"
    echo ""
    
    SVG_COUNT=$(find "$SVG_OUTPUT_DIR" -name "*.svg" -type f 2>/dev/null | wc -l)
    echo -e "${BOLD}📊 Total SVG files: ${CYAN}$SVG_COUNT${NC}"
    
    if [ "$SVG_COUNT" -gt 0 ]; then
        echo ""
        echo -e "${YELLOW}📁 All generated SVG files:${NC}"
        echo ""
        
        find "$SVG_OUTPUT_DIR" -name "*.svg" -type f -printf "%T@ %s %f\n" 2>/dev/null | \
        sort -rn | \
        awk '{
            size_mb = $2 / 1024 / 1024;
            printf "   %3d. %-65s (%.2f MB)\n", NR, $3, size_mb;
        }'
        
        echo ""
        echo -e "${GREEN}✨ All files are ready in: $SVG_OUTPUT_DIR${NC}"
    else
        echo -e "${YELLOW}⚠️  No SVG files found in output directory${NC}"
    fi
    
    echo ""
}

# Main execution
main() {
    print_header
    check_prerequisites
    fetch_api_filenames
    show_system_resources
    
    # Container name
    CONTAINER_NAME="png-processor-$(date +%s)"
    
    echo -e "${YELLOW}🚀 Starting PNG to SVG conversion container...${NC}"
    echo ""
    
    # Start container in background
    docker run -d --rm \
        --cpu-shares 128 \
        --cpus "4.0" \
        --blkio-weight 100 \
        --memory-reservation "512m" \
        --oom-score-adj 1000 \
        --memory "4g" \
        --shm-size 1g \
        --name "$CONTAINER_NAME" \
        -e API_ENDPOINT="$API_ENDPOINT" \
        -e ENABLE_SVGO="$ENABLE_SVGO" \
        -v "$PNG_INPUT_DIR":/home/sclera/images/png_floors \
        -v "$SVG_OUTPUT_DIR":/home/sclera/images/svg_floors \
        -v "$SVG_OUTPUT_DIR":/app/vdms_svg_outputs \
        --add-host=host.docker.internal:host-gateway \
        -e BASE_URL=http://localhost:8092/api/v1/migration \
        -e CONVERSION_ENGINE="$CONVERSION_ENGINE" \
        "$IMAGE_NAME" "$@" > /dev/null
    
    echo -e "${GREEN}✅ Container started: $CONTAINER_NAME${NC}"
    echo -e "${YELLOW}📊 Monitoring resources in real-time...${NC}"
    echo ""
    
    # Get container ID
    CONTAINER_ID=$(docker ps --filter "name=$CONTAINER_NAME" --format "{{.ID}}")
    
    if [ -z "$CONTAINER_ID" ]; then
        echo -e "${RED}❌ Failed to get container ID${NC}"
        exit 1
    fi
    
    # Start monitoring in background (silent data collection)
    monitor_container "$CONTAINER_ID" &
    MONITOR_PID=$!
    
    # Small delay to let monitoring start
    sleep 1
    
    # Show container logs with resource tracking after each file
    echo -e "${BLUE}────────────────────────────────────────────────────────────────────────────────${NC}"
    echo -e "${YELLOW}📋 Conversion Progress:${NC}"
    echo -e "${BLUE}────────────────────────────────────────────────────────────────────────────────${NC}"
    echo ""
    
    # Follow logs and show resources after each SUCCESS line
    docker logs -f "$CONTAINER_ID" 2>&1 | while IFS= read -r line; do
        echo "$line"
        
        # Check if this line indicates a file conversion completed
        if echo "$line" | grep -q "🎉 SUCCESS:"; then
            # Get current resource usage
            local resources=$(get_current_resources "$CONTAINER_ID")
            local cpu=$(echo "$resources" | cut -d',' -f1)
            local mem=$(echo "$resources" | cut -d',' -f2)
            local read_mb=$(echo "$resources" | cut -d',' -f3)
            local write_mb=$(echo "$resources" | cut -d',' -f4)
            
            # Display resources after this file
            echo -e "   ${CYAN}[Resources]${NC} 💻 CPU: ${BOLD}${cpu}%${NC} | 🧠 RAM: ${BOLD}${mem} MB${NC} | 💾 Disk Read: ${BOLD}${read_mb} MB${NC} | 💾 Disk Write: ${BOLD}${write_mb} MB${NC}"
            echo ""
        fi
    done
    
    # Wait for container to finish
    docker wait "$CONTAINER_ID" > /dev/null 2>&1
    EXIT_CODE=$?
    
    # Stop monitoring
    if [ -n "$MONITOR_PID" ] && ps -p $MONITOR_PID > /dev/null 2>&1; then
        kill $MONITOR_PID 2>/dev/null || true
        wait $MONITOR_PID 2>/dev/null || true
    fi
    
    echo ""
    echo ""
    
    # Check if conversion succeeded
    if [ $EXIT_CODE -ne 0 ]; then
        echo ""
        echo -e "${RED}❌ Conversion failed!${NC}"
        exit $EXIT_CODE
    fi
    
    # Show resource summary
    show_resource_summary
    
    # Show results
    check_results
}

# Run main function
main


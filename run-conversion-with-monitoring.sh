#!/bin/bash
# PNG to SVG Conversion with Real-Time Resource Monitoring
# Supports Single-Tenant and Multi-Tenant processing modes
# No additional packages required - uses Docker stats API

set -e

# Load environment files if they exist (single source of truth)
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
    echo "PNG to SVG Processor with Monitoring - Usage Guide"
    echo "================================================================================"
    echo "Usage:"
    echo "  $0 [options] [-- any-main.py-args]"
    echo ""
    echo "Options:"
    echo "  -m, --mode <mode>        Execution mode: 'single-tenant' (default) or 'multi-tenant'"
    echo "  -v, --vdms <id(s)>       VDMS filter for multi-tenant (e.g. 'VDMS885' or 'VDMS885,VDMS886')"
    echo "  -e, --engine <engine>    Conversion engine: 'potrace' (default) or 'vtracer'"
    echo "  -a, --auto-detect        Auto-detect engine based on image color (vtracer for color, potrace for B&W)"
    echo "      --disable-svgo       Disable SVGO optimization (use svgcleaner only)"
    echo "  -i, --input-dir <dir>    Input directory containing PNG files (default: /home/sclera/images/png_floors)"
    echo "  -o, --output-dir <dir>   Output directory for SVG files (default: /home/sclera/images/svg_floors)"
    echo "  -h, --help               Show this help message"
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

# Configuration Defaults
PNG_INPUT_DIR="${PNG_INPUT_DIR:-/home/sclera/images/png_floors}"
SVG_OUTPUT_DIR="${SVG_OUTPUT_DIR:-/home/sclera/images/svg_floors}"
IMAGE_NAME="${IMAGE_NAME:-png-processor:latest}"

export BASE_URL="${BASE_URL:-http://host.docker.internal:8888/api/v1/migration}"
export MULTI_TENANT_BASEURL="${MULTI_TENANT_BASEURL:-https://tenant.com}"
export API_ENDPOINT="${API_ENDPOINT:-${BASE_URL}/convert}"
export ENABLE_SVGO="${ENABLE_SVGO:-true}"
export CONVERSION_ENGINE="${CONVERSION_ENGINE:-potrace}"
export AUTO_DETECT_ENGINE="${AUTO_DETECT_ENGINE:-false}"
export VDMS_FILTER="${VDMS_FILTER:-}"

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
    echo -e "Mode:             ${CYAN}$MODE${NC}"
    if [ "$MODE" = "multi-tenant" ]; then
        echo -e "Multi-Tenant URL: ${CYAN}$MULTI_TENANT_BASEURL${NC}"
        if [ -n "$VDMS_FILTER" ]; then
            echo -e "VDMS Filter:      ${YELLOW}$VDMS_FILTER${NC}"
        else
            echo -e "VDMS Filter:      ${YELLOW}All VDMS${NC}"
        fi
    else
        echo -e "Input directory:  ${CYAN}$PNG_INPUT_DIR${NC}"
        echo -e "Single-Tenant URL:${CYAN}$BASE_URL${NC}"
        echo -e "API Endpoint:     ${CYAN}$API_ENDPOINT${NC}"
    fi
    echo -e "Output directory: ${CYAN}$SVG_OUTPUT_DIR${NC}"
    echo -e "Docker image:     ${CYAN}$IMAGE_NAME${NC}"
    echo -e "SVGO Optimization:${CYAN}$ENABLE_SVGO${NC}"
    echo -e "Conversion engine:${CYAN}$CONVERSION_ENGINE${NC}"
    echo -e "Auto-detect engine:${CYAN}$AUTO_DETECT_ENGINE${NC}"
    echo -e "${BLUE}================================================================================${NC}"
    echo ""
}

# Check prerequisites
check_prerequisites() {
    echo -e "${YELLOW}Checking prerequisites...${NC}"
    
    # Create output directory
    mkdir -p "$SVG_OUTPUT_DIR" 2>/dev/null || true
    
    # Check input directory and PNG files for single-tenant
    if [ "$MODE" != "multi-tenant" ]; then
        if [ ! -d "$PNG_INPUT_DIR" ]; then
            echo -e "${RED}Input directory does not exist: $PNG_INPUT_DIR${NC}"
            exit 1
        fi
        
        PNG_COUNT=$(find "$PNG_INPUT_DIR" -name "*.png" -type f 2>/dev/null | wc -l)
        if [ "$PNG_COUNT" -eq 0 ]; then
            echo -e "${RED}No PNG files found in $PNG_INPUT_DIR${NC}"
            exit 1
        fi
        echo -e "${GREEN}Found $PNG_COUNT PNG files in local directory${NC}"
    else
        echo -e "${GREEN}Multi-tenant cloud mode: Images will be downloaded from API${NC}"
    fi
    
    # Check Docker
    if ! docker ps >/dev/null 2>&1; then
        echo -e "${RED}Docker is not accessible${NC}"
        exit 1
    fi
    echo -e "${GREEN}Docker is accessible${NC}"
    
    # Check if image exists
    if ! docker images --format "{{.Repository}}:{{.Tag}}" | grep -q "^${IMAGE_NAME}$"; then
        echo -e "${RED}Docker image not found: $IMAGE_NAME${NC}"
        echo -e "${YELLOW}   Build it with: ./setup.sh${NC}"
        exit 1
    fi
    echo -e "${GREEN}Docker image found: $IMAGE_NAME${NC}"
    
    echo ""
}

# Fetch API filenames
fetch_api_filenames() {
    if [ "$MODE" = "multi-tenant" ]; then
        return 0
    fi

    if [ -z "$API_ENDPOINT" ]; then
        return 0
    fi
    
    echo -e "${YELLOW}Fetching file list from API: $API_ENDPOINT${NC}"
    
    if command -v curl >/dev/null 2>&1; then
        local response=$(curl -s "$API_ENDPOINT" 2>/dev/null)
        
        if [ $? -eq 0 ] && [ -n "$response" ]; then
            echo -e "${GREEN}API request successful${NC}"
            echo ""
            
            # Extract and display all filenames from URLs
            echo -e "${CYAN}Files to process from API:${NC}"
            echo "$response" | grep -o '"[^"]*\.png"' | sed 's/"//g' | while IFS= read -r url; do
                filename=$(basename "$url")
                echo -e "   • $filename"
            done
        else
            echo -e "${YELLOW}API request failed, will process local files${NC}"
        fi
    else
        echo -e "${YELLOW}curl not available, skipping API check${NC}"
    fi
    
    echo ""
}

# Show system resources
show_system_resources() {
    echo -e "${YELLOW}System Resources:${NC}"
    
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
to_mb() {
    local val=$(echo "$1" | sed 's/[^0-9.]//g')
    local unit=$(echo "$1" | sed 's/[0-9.]//g' | tr '[:lower:]' '[:upper:]')
    
    case "$unit" in
        B|BYTES)
            echo "$val" | awk '{printf "%.2f", $1/1024/1024}'
            ;;
        KB|KIB|K)
            echo "$val" | awk '{printf "%.2f", $1/1024}'
            ;;
        MB|MIB|M)
            echo "$val" | awk '{printf "%.2f", $1}'
            ;;
        GB|GIB|G)
            echo "$val" | awk '{printf "%.2f", $1*1024}'
            ;;
        *)
            echo "0.00"
            ;;
    esac
}

get_current_resources() {
    local container_id="$1"
    local stats=$(docker stats "$container_id" --no-stream --format "{{.CPUPerc}},{{.MemUsage}},{{.BlockIO}}" 2>/dev/null)
    
    if [ -n "$stats" ]; then
        local cpu=$(echo "$stats" | cut -d',' -f1 | tr -d '%')
        local mem_raw=$(echo "$stats" | cut -d',' -f2 | cut -d'/' -f1 | tr -d ' ')
        local mem=$(to_mb "$mem_raw")
        local block_raw=$(echo "$stats" | cut -d',' -f3)
        local read_raw=$(echo "$block_raw" | cut -d'/' -f1 | tr -d ' ')
        local write_raw=$(echo "$block_raw" | cut -d'/' -f2 | tr -d ' ')
        local read_mb=$(to_mb "$read_raw")
        local write_mb=$(to_mb "$write_raw")
        
        echo "$cpu,$mem,$read_mb,$write_mb"
    else
        echo "0.00,0.00,0.00,0.00"
    fi
}

monitor_container() {
    local container_id="$1"
    
    while docker ps --filter "id=$container_id" --format "{{.ID}}" | grep -q "$container_id"; do
        local stats=$(docker stats "$container_id" --no-stream --format "{{.CPUPerc}},{{.MemUsage}},{{.BlockIO}},{{.NetIO}}" 2>/dev/null)
        
        if [ -n "$stats" ]; then
            local timestamp=$(date +%s)
            local cpu=$(echo "$stats" | cut -d',' -f1 | tr -d '%')
            local mem_raw=$(echo "$stats" | cut -d',' -f2 | cut -d'/' -f1 | tr -d ' ')
            local mem=$(to_mb "$mem_raw")
            
            local block_raw=$(echo "$stats" | cut -d',' -f3)
            local block_read_raw=$(echo "$block_raw" | cut -d'/' -f1 | tr -d ' ')
            local block_write_raw=$(echo "$block_raw" | cut -d'/' -f2 | tr -d ' ')
            local block_read=$(to_mb "$block_read_raw")
            local block_write=$(to_mb "$block_write_raw")
            
            local net_raw=$(echo "$stats" | cut -d',' -f4)
            local net_in_raw=$(echo "$net_raw" | cut -d'/' -f1 | tr -d ' ')
            local net_out_raw=$(echo "$net_raw" | cut -d'/' -f2 | tr -d ' ')
            local net_in=$(to_mb "$net_in_raw")
            local net_out=$(to_mb "$net_out_raw")
            
            echo "$timestamp,$cpu,$mem,$block_read,$block_write,$net_in,$net_out" >> "$STATS_FILE"
        fi
        
        sleep "$MONITOR_INTERVAL"
    done
}

# Calculate statistics from collected data
calculate_stats() {
    if [ ! -f "$STATS_FILE" ] || [ ! -s "$STATS_FILE" ]; then
        echo "0,0,0,0,0,0,0,0,0,0,0"
        return
    fi
    
    awk -F',' '
    BEGIN {
        cpu_sum = 0; cpu_max = 0; cpu_min = 999999;
        mem_sum = 0; mem_max = 0; mem_min = 999999;
        first_read = -1; last_read = 0;
        first_write = -1; last_write = 0;
        first_net_in = -1; last_net_in = 0;
        first_net_out = -1; last_net_out = 0;
        start_time = 0; end_time = 0;
        count = 0;
    }
    {
        count++;
        time = $1;
        cpu = $2;
        mem = $3;
        bread = $4;
        bwrite = $5;
        nin = $6;
        nout = $7;
        
        if (count == 1) {
            start_time = time;
            first_read = bread;
            first_write = bwrite;
            first_net_in = nin;
            first_net_out = nout;
        }
        end_time = time;
        last_read = bread;
        last_write = bwrite;
        last_net_in = nin;
        last_net_out = nout;
        
        cpu_sum += cpu;
        if (cpu > cpu_max) cpu_max = cpu;
        if (cpu < cpu_min) cpu_min = cpu;
        
        mem_sum += mem;
        if (mem > mem_max) mem_max = mem;
        if (mem < mem_min) mem_min = mem;
    }
    END {
        if (count > 0) {
            cpu_avg = cpu_sum / count;
            mem_avg = mem_sum / count;
            total_bread = (last_read >= first_read) ? (last_read - first_read) : last_read;
            total_bwrite = (last_write >= first_write) ? (last_write - first_write) : last_write;
            total_nin = (last_net_in >= first_net_in) ? (last_net_in - first_net_in) : last_net_in;
            total_nout = (last_net_out >= first_net_out) ? (last_net_out - first_net_out) : last_net_out;
            duration = end_time - start_time;
            if (duration == 0) duration = count;
            
            printf "%.2f,%.2f,%.2f,%.2f,%.2f,%.2f,%.2f,%.2f,%.2f,%.2f,%d\n",
                cpu_avg, cpu_max, cpu_min,
                mem_avg, mem_max, mem_min,
                total_bread, total_bwrite,
                total_nin, total_nout,
                duration;
        } else {
            print "0,0,0,0,0,0,0,0,0,0,0";
        }
    }
    ' "$STATS_FILE"
}

# Display resource impact summary
show_resource_summary() {
    local stats=$(calculate_stats)
    
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
    
    echo -e "${BLUE}================================================================================${NC}"
    echo -e "${BOLD}Resource Impact Summary${NC}"
    echo -e "${BLUE}================================================================================${NC}"
    echo -e "${BOLD}Duration: ${CYAN}${duration}${NC} seconds"
    echo ""
    echo -e "${BOLD}CPU Usage:${NC}"
    echo -e "   Average: ${CYAN}${cpu_avg}%${NC}"
    echo -e "   Maximum: ${CYAN}${cpu_max}%${NC}"
    echo -e "   Minimum: ${CYAN}${cpu_min}%${NC}"
    echo ""
    echo -e "${BOLD}Memory Usage:${NC}"
    echo -e "   Average: ${CYAN}${mem_avg} MB${NC}"
    echo -e "   Maximum: ${CYAN}${mem_max} MB${NC}"
    echo -e "   Minimum: ${CYAN}${mem_min} MB${NC}"
    echo ""
    echo -e "${BOLD}Disk I/O Volume:${NC}"
    echo -e "   Total Read:  ${CYAN}${block_read} MB${NC}"
    echo -e "   Total Write: ${CYAN}${block_write} MB${NC}"
    echo ""
    echo -e "${BOLD}Network Volume:${NC}"
    echo -e "   Total Received: ${CYAN}${net_in} MB${NC}"
    echo -e "   Total Sent:     ${CYAN}${net_out} MB${NC}"
    echo ""
    echo -e "${BLUE}================================================================================${NC}"
    echo ""
}

# Check and display results
check_results() {
    echo -e "${BLUE}================================================================================${NC}"
    echo -e "${BOLD}Conversion Results${NC}"
    echo -e "${BLUE}================================================================================${NC}"
    echo ""
    
    SVG_COUNT=$(find "$SVG_OUTPUT_DIR" -name "*.svg" -type f 2>/dev/null | wc -l)
    echo -e "${BOLD}Total SVG files: ${CYAN}$SVG_COUNT${NC}"
    
    if [ "$SVG_COUNT" -gt 0 ]; then
        echo ""
        echo -e "${YELLOW}All generated SVG files:${NC}"
        echo ""
        
        find "$SVG_OUTPUT_DIR" -name "*.svg" -type f -printf "%T@ %s %f\n" 2>/dev/null | \
        sort -rn | \
        awk '{
            size_mb = $2 / 1024 / 1024;
            printf "   %3d. %-65s (%.2f MB)\n", NR, $3, size_mb;
        }'
        
        echo ""
        echo -e "${GREEN}All files are ready in: $SVG_OUTPUT_DIR${NC}"
    else
        echo -e "${YELLOW}No SVG files found in output directory${NC}"
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
    
    echo -e "${YELLOW}Starting PNG to SVG conversion container...${NC}"
    echo ""
    
    NETWORK_ARG=""
    if [ "$MODE" != "multi-tenant" ]; then
        if docker network inspect sclera_bridge >/dev/null 2>&1; then
            NETWORK_ARG="--network sclera_bridge"
        fi
    fi

    # Build container arguments
    CONTAINER_ARGS=()
    CONTAINER_ARGS+=(--mode "$MODE")
    CONTAINER_ARGS+=(--engine "$CONVERSION_ENGINE")
    if [ "$AUTO_DETECT_ENGINE" = "true" ]; then
        CONTAINER_ARGS+=(--auto-detect)
    fi
    if [ "$ENABLE_SVGO" = "false" ]; then
        CONTAINER_ARGS+=(--disable-svgo)
    fi
    if [ -n "$VDMS_FILTER" ]; then
        CONTAINER_ARGS+=(--vdms "$VDMS_FILTER")
    fi
    CONTAINER_ARGS+=("${PASSTHROUGH_ARGS[@]}")

    # Start container in background
    docker run -d --rm \
        $NETWORK_ARG \
        --cpus "4.0" \
        --memory "4g" \
        --name "$CONTAINER_NAME" \
        -e MODE="$MODE" \
        -e MULTI_TENANT_BASEURL="$MULTI_TENANT_BASEURL" \
        -e API_ENDPOINT="$API_ENDPOINT" \
        -e ENABLE_SVGO="$ENABLE_SVGO" \
        -v "$PNG_INPUT_DIR":/home/sclera/images/png_floors \
        -v "$SVG_OUTPUT_DIR":/home/sclera/images/svg_floors \
        -v "$SVG_OUTPUT_DIR":/app/vdms_svg_outputs \
        --add-host=host.docker.internal:host-gateway \
        -e BASE_URL="$BASE_URL" \
        -e CONVERSION_ENGINE="$CONVERSION_ENGINE" \
        -e AUTO_DETECT_ENGINE="$AUTO_DETECT_ENGINE" \
        -e VDMS_FILTER="$VDMS_FILTER" \
        "$IMAGE_NAME" \
        "${CONTAINER_ARGS[@]}" > /dev/null
    
    echo -e "${GREEN}Container started: $CONTAINER_NAME${NC}"
    echo -e "${YELLOW}Monitoring resources in real-time...${NC}"
    echo ""
    
    # Get container ID
    CONTAINER_ID=$(docker ps --filter "name=$CONTAINER_NAME" --format "{{.ID}}")
    
    if [ -z "$CONTAINER_ID" ]; then
        echo -e "${RED}Failed to get container ID${NC}"
        exit 1
    fi
    
    # Start monitoring in background (silent data collection)
    monitor_container "$CONTAINER_ID" &
    MONITOR_PID=$!
    
    # Small delay to let monitoring start
    sleep 1
    
    # Show container logs with resource tracking after each file
    echo -e "${BLUE}────────────────────────────────────────────────────────────────────────────────${NC}"
    echo -e "${YELLOW}Conversion Progress:${NC}"
    echo -e "${BLUE}────────────────────────────────────────────────────────────────────────────────${NC}"
    echo ""
    
    # Follow logs and show resources after each SUCCESS line
    docker logs -f "$CONTAINER_ID" 2>&1 | while IFS= read -r line; do
        echo "$line"
        
        # Check if this line indicates a file conversion completed
        if echo "$line" | grep -q "SUCCESS:"; then
            # Get current resource usage
            local resources=$(get_current_resources "$CONTAINER_ID")
            local cpu=$(echo "$resources" | cut -d',' -f1)
            local mem=$(echo "$resources" | cut -d',' -f2)
            local read_mb=$(echo "$resources" | cut -d',' -f3)
            local write_mb=$(echo "$resources" | cut -d',' -f4)
            
            # Display resources after this file
            echo -e "   ${CYAN}[Resources]${NC} CPU: ${BOLD}${cpu}%${NC} | RAM: ${BOLD}${mem} MB${NC} | Disk Read: ${BOLD}${read_mb} MB${NC} | Disk Write: ${BOLD}${write_mb} MB${NC}"
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
        echo -e "${RED}Conversion failed!${NC}"
        exit $EXIT_CODE
    fi
    
    # Show resource summary
    show_resource_summary
    
    # Show results
    check_results
}

# Run main function
main "$@"

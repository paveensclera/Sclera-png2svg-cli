#!/bin/bash
# ==============================================================================
# PNG to SVG Processor - Multi-Configuration CPU & RAM Benchmark Script
# ==============================================================================
# Tests resource configurations from lowest to highest (or custom single config)
# Captures peak CPU, peak RAM, Disk I/O, duration, exit code, OOM events, and
# produces a comprehensive benchmark comparison table.
# ==============================================================================

set -eo pipefail

# Configuration Defaults
PNG_INPUT_DIR="${PNG_INPUT_DIR:-/home/sclera/images/png_floors}"
SVG_OUTPUT_DIR="${SVG_OUTPUT_DIR:-/home/sclera/images/svg_floors}"
IMAGE_NAME="${IMAGE_NAME:-png-processor:latest}"

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

# API Endpoint & Base URL Configuration
export BASE_URL="${BASE_URL:-http://host.docker.internal:8082/api/v1/migration}"
export ENABLE_SVGO="${ENABLE_SVGO:-true}"
export CONVERSION_ENGINE="${CONVERSION_ENGINE:-potrace}"
export AUTO_DETECT_ENGINE="${AUTO_DETECT_ENGINE:-false}"
export OCR_ENGINE="${OCR_ENGINE:-onnxruntime}"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
MAGENTA='\033[0;35m'
BOLD='\033[1m'
NC='\033[0m'

# Conversion helper: normalize Docker stats unit to MB
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

# Real-time resource collector running in background
monitor_container() {
    local container_id="$1"
    local stats_file="$2"
    local interval=1

    echo "timestamp,cpu_percent,memory_mb,block_read_mb,block_write_mb" > "$stats_file"

    while docker ps --filter "id=$container_id" --format "{{.ID}}" | grep -q "$container_id"; do
        local stats=$(docker stats --no-stream --format "{{.CPUPerc}},{{.MemUsage}},{{.BlockIO}}" "$container_id" 2>/dev/null || echo "0.00%,0B / 0B,0B / 0B")

        local cpu_raw=$(echo "$stats" | cut -d',' -f1 | sed 's/%//')
        local mem_raw=$(echo "$stats" | cut -d',' -f2 | cut -d'/' -f1 | xargs)
        local block_raw=$(echo "$stats" | cut -d',' -f3)

        local block_read_raw=$(echo "$block_raw" | cut -d'/' -f1 | xargs)
        local block_write_raw=$(echo "$block_raw" | cut -d'/' -f2 | xargs)

        local mem_mb=$(to_mb "$mem_raw")
        local block_read_mb=$(to_mb "$block_read_raw")
        local block_write_mb=$(to_mb "$block_write_raw")

        echo "$(date +%s),$cpu_raw,$mem_mb,$block_read_mb,$block_write_mb" >> "$stats_file"
        sleep $interval
    done
}

# Matrix of configurations to test: [CPUS]:[MEMORY]:[LABEL]
DEFAULT_CONFIGS=(
    "0.5:512m:Tier 1 (Ultra Low - High OOM Risk)"
    "1.0:1g:Tier 2 (Minimal CPU & RAM)"
    "1.5:2g:Tier 3 (Budget Server / Safe Low)"
    "2.0:3g:Tier 4 (Balanced Production)"
    "4.0:4g:Tier 5 (Default High-Perf)"
    "6.0:6g:Tier 6 (High Load / High Core)"
)

# Benchmark results storage
SUMMARY_RESULTS=()

run_single_benchmark() {
    local cpus="$1"
    local memory="$2"
    local label="$3"
    local test_run_id="$4"

    local test_out_dir="${SVG_OUTPUT_DIR}/benchmark_run_${test_run_id}"
    mkdir -p "$test_out_dir"
    chmod 777 "$test_out_dir" 2>/dev/null || true

    local stats_file="/tmp/benchmark_stats_${test_run_id}_$$.csv"
    local container_name="png-proc-bench-${test_run_id}-$(date +%s)"

    echo -e "${BLUE}================================================================================${NC}"
    echo -e "${BOLD}TEST RUN #${test_run_id}: ${CYAN}${label}${NC}"
    echo -e "${BOLD}Allocated Limits: ${YELLOW}CPUs: ${cpus} cores | RAM: ${memory}${NC}"
    echo -e "${BLUE}================================================================================${NC}"

    local start_ts=$(date +%s)

    # Start container in background
    local container_id
    container_id=$(docker run -d --rm \
        --cpus "${cpus}" \
        --memory "${memory}" \
        --shm-size 512m \
        --name "$container_name" \
        -v "$PNG_INPUT_DIR":/home/sclera/images/png_floors \
        -v "$test_out_dir":/home/sclera/images/svg_floors \
        -e ENABLE_SVGO="$ENABLE_SVGO" \
        -e BASE_URL="$BASE_URL" \
        -e CONVERSION_ENGINE="$CONVERSION_ENGINE" \
        -e AUTO_DETECT_ENGINE="$AUTO_DETECT_ENGINE" \
        -e OCR_ENGINE="$OCR_ENGINE" \
        "$IMAGE_NAME" "$@")

    # Start monitor in background
    monitor_container "$container_id" "$stats_file" &
    local monitor_pid=$!

    # Stream container logs in real time
    docker logs -f "$container_id" 2>&1 || true

    # Wait for container exit status
    local exit_code=0
    exit_code=$(docker inspect "$container_id" --format='{{.State.ExitCode}}' 2>/dev/null || echo "0")

    # Stop monitor
    kill "$monitor_pid" 2>/dev/null || true
    wait "$monitor_pid" 2>/dev/null || true

    local end_ts=$(date +%s)
    local duration=$((end_ts - start_ts))

    # Parse recorded metrics from stats file
    local metrics="0.00,0.00,0.00,0.00,0.00,0.00"
    if [ -f "$stats_file" ] && [ $(wc -l < "$stats_file") -gt 1 ]; then
        metrics=$(awk -F',' 'NR > 1 {
            cpu = $2 + 0;
            mem = $3 + 0;
            bread = $4 + 0;
            bwrite = $5 + 0;

            if (NR == 2 || cpu > cpu_max) cpu_max = cpu;
            if (NR == 2 || mem > mem_max) mem_max = mem;
            cpu_sum += cpu;
            mem_sum += mem;
            count++;
            if (bread > bread_max) bread_max = bread;
            if (bwrite > bwrite_max) bwrite_max = bwrite;
        } END {
            if (count > 0) {
                printf "%.1f,%.1f,%.1f,%.1f,%.1f,%.1f",
                    (cpu_sum/count), cpu_max, (mem_sum/count), mem_max, bread_max, bwrite_max;
            } else {
                printf "0.0,0.0,0.0,0.0,0.0,0.0";
            }
        }' "$stats_file")
    fi
    rm -f "$stats_file" 2>/dev/null || true

    local cpu_avg=$(echo "$metrics" | cut -d',' -f1)
    local cpu_max=$(echo "$metrics" | cut -d',' -f2)
    local mem_avg=$(echo "$metrics" | cut -d',' -f3)
    local mem_max=$(echo "$metrics" | cut -d',' -f4)
    local io_read=$(echo "$metrics" | cut -d',' -f5)
    local io_write=$(echo "$metrics" | cut -d',' -f6)

    # Count generated SVGs
    local svg_count=$(find "$test_out_dir" -name "*.svg" -type f 2>/dev/null | wc -l)

    # Determine status label
    local status_str="SUCCESS"
    if [ "$exit_code" -eq 137 ]; then
        status_str="OOM-KILLED (137)"
    elif [ "$exit_code" -ne 0 ]; then
        status_str="FAILED (Code $exit_code)"
    elif [ "$svg_count" -eq 0 ]; then
        status_str="NO-OUTPUT (0 SVGs)"
    fi

    echo ""
    echo -e "${BOLD}--- Test #${test_run_id} Results Summary ---${NC}"
    echo -e "   Status:          $( [ "$status_str" = "SUCCESS" ] && echo -e "${GREEN}${status_str}${NC}" || echo -e "${RED}${status_str}${NC}" )"
    echo -e "   Duration:        ${CYAN}${duration}s${NC}"
    echo -e "   CPU Avg / Peak:  ${CYAN}${cpu_avg}% / ${cpu_max}%${NC}"
    echo -e "   RAM Avg / Peak:  ${CYAN}${mem_avg} MB / ${mem_max} MB${NC} (Allocated: ${memory})"
    echo -e "   Disk I/O Peak:   Read: ${io_read} MB | Write: ${io_write} MB"
    echo -e "   SVGs Generated:  ${CYAN}${svg_count}${NC}"
    echo ""

    # Save to summary matrix: LABEL | CPUS | MEM | TIME | CPU_PEAK | RAM_PEAK | SVGS | STATUS
    SUMMARY_RESULTS+=("${label}|${cpus}|${memory}|${duration}s|${cpu_max}%|${mem_max}MB|${svg_count}|${status_str}")
}

print_header() {
    local host_cpus=$(nproc 2>/dev/null || echo "Unknown")
    local host_ram=$(free -h 2>/dev/null | awk '/^Mem:/ {print $2}' || echo "Unknown")
    local host_avail=$(free -h 2>/dev/null | awk '/^Mem:/ {print $7}' || echo "Unknown")

    echo -e "${MAGENTA}================================================================================${NC}"
    echo -e "${BOLD}         PNG to SVG Conversion - CPU & RAM Resource Benchmarking Tool            ${NC}"
    echo -e "${MAGENTA}================================================================================${NC}"
    echo -e "WSL Host Specs:    ${CYAN}${host_cpus} Cores | Total RAM: ${host_ram} (Available: ${host_avail})${NC}"
    echo -e "Input Directory:   ${CYAN}$PNG_INPUT_DIR${NC}"
    echo -e "Base Output Dir:   ${CYAN}$SVG_OUTPUT_DIR${NC}"
    echo -e "Conversion Engine: ${CYAN}$CONVERSION_ENGINE${NC} (Auto-detect: ${AUTO_DETECT_ENGINE})"
    echo -e "SVGO Optimization: ${CYAN}$ENABLE_SVGO${NC}"

    local png_count=$(find "$PNG_INPUT_DIR" -name "*.png" -type f 2>/dev/null | wc -l)
    echo -e "PNG Files Found:   ${BOLD}${YELLOW}${png_count} images${NC}"
    if [ "$png_count" -eq 0 ]; then
        echo -e "${RED}Error: No PNG files found in $PNG_INPUT_DIR to benchmark.${NC}"
        exit 1
    fi
    echo -e "${MAGENTA}================================================================================${NC}"
    echo ""
}

print_comparison_table() {
    echo ""
    echo -e "${MAGENTA}===================================================================================================================${NC}"
    echo -e "${BOLD}                                         FINAL BENCHMARK COMPARISON REPORT                                         ${NC}"
    echo -e "${MAGENTA}===================================================================================================================${NC}"
    printf "%-32s | %-6s | %-6s | %-8s | %-10s | %-10s | %-6s | %-18s\n" \
        "Configuration Tier" "CPUs" "RAM" "Time" "Peak CPU" "Peak RAM" "SVGs" "Status"
    echo "---------------------------------+--------+--------+----------+------------+------------+--------+-------------------"

    for row in "${SUMMARY_RESULTS[@]}"; do
        IFS='|' read -r r_label r_cpus r_mem r_time r_cpup r_memp r_svgs r_status <<< "$row"
        
        local color_status="$GREEN"
        if [[ "$r_status" =~ "OOM" || "$r_status" =~ "FAIL" ]]; then
            color_status="$RED"
        fi
        
        printf "%-32s | %-6s | %-6s | %-8s | %-10s | %-10s | %-6s | ${color_status}%-18s${NC}\n" \
            "$r_label" "$r_cpus" "$r_mem" "$r_time" "$r_cpup" "$r_memp" "$r_svgs" "$r_status"
    done
    echo -e "${MAGENTA}===================================================================================================================${NC}"
    echo ""
    echo -e "${BOLD}Recommended Resource Takeaways:${NC}"
    echo -e " 1. Look for the lowest RAM tier with status ${GREEN}SUCCESS${NC} (to find minimum viable memory)."
    echo -e " 2. Observe the ${CYAN}Peak RAM${NC} column to determine the safe memory buffer needed for your images."
    echo -e " 3. Balance ${CYAN}Time${NC} vs ${CYAN}CPUs${NC} to select the optimal CPU throttling configuration for your server."
    echo ""
}

# Main Execution
main() {
    print_header

    # Allow custom single configuration via CLI flags, e.g. ./test-resources.sh --custom 1.5 2g
    if [ "$1" == "--custom" ] && [ -n "$2" ] && [ -n "$3" ]; then
        echo -e "${YELLOW}Running single custom benchmark configuration: CPUs=$2, RAM=$3${NC}"
        echo ""
        run_single_benchmark "$2" "$3" "Custom Test Configuration" 1
    else
        echo -e "${YELLOW}Starting systematic tests from lowest to highest resource tiers...${NC}"
        echo -e "Total configurations to test: ${BOLD}${#DEFAULT_CONFIGS[@]}${NC}"
        echo ""

        local idx=1
        for cfg in "${DEFAULT_CONFIGS[@]}"; do
            IFS=':' read -r cpus memory label <<< "$cfg"
            run_single_benchmark "$cpus" "$memory" "$label" "$idx"
            idx=$((idx + 1))
        done
    fi

    print_comparison_table
}

main "$@"

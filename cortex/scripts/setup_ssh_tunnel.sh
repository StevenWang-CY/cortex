#!/usr/bin/env bash
# =============================================================================
# Cortex SSH Tunnel Setup
#
# Establishes an SSH tunnel to gwhiz1.cis.upenn.edu for remote LLM inference.
# Supports health checking, auto-reconnect, and background mode.
#
# Usage:
#   ./setup_ssh_tunnel.sh                # foreground mode
#   ./setup_ssh_tunnel.sh --background   # background with auto-reconnect
#   ./setup_ssh_tunnel.sh --check        # health check only
#   ./setup_ssh_tunnel.sh --stop         # stop background tunnel
# =============================================================================

set -euo pipefail

# Configuration (override via environment)
REMOTE_HOST="${CORTEX_LLM_REMOTE_HOST:-gwhiz1.cis.upenn.edu}"
REMOTE_PORT="${CORTEX_LLM_REMOTE_PORT:-8800}"
SSH_USER="${CORTEX_LLM_SSH_USER:-wangcy07}"
LOCAL_PORT="${CORTEX_SSH_LOCAL_PORT:-8800}"
HEALTH_ENDPOINT="http://localhost:${LOCAL_PORT}/v1/models"
PID_FILE="/tmp/cortex-ssh-tunnel.pid"
LOG_FILE="/tmp/cortex-ssh-tunnel.log"
RECONNECT_INTERVAL=10
MAX_RETRIES=0  # 0 = unlimited

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
NC='\033[0m' # No Color

log() {
    echo -e "[$(date '+%H:%M:%S')] $1"
}

log_ok() {
    log "${GREEN}OK${NC}: $1"
}

log_err() {
    log "${RED}ERROR${NC}: $1"
}

log_warn() {
    log "${YELLOW}WARN${NC}: $1"
}

# Check if tunnel process is running
is_tunnel_running() {
    if [ -f "$PID_FILE" ]; then
        local pid
        pid=$(cat "$PID_FILE")
        if kill -0 "$pid" 2>/dev/null; then
            return 0
        fi
        # Stale PID file
        rm -f "$PID_FILE"
    fi
    return 1
}

# Health check: verify LLM server is reachable through tunnel
health_check() {
    if ! command -v curl &>/dev/null; then
        log_warn "curl not installed, skipping health check"
        return 1
    fi

    if curl -s --connect-timeout 5 --max-time 10 "$HEALTH_ENDPOINT" >/dev/null 2>&1; then
        return 0
    fi
    return 1
}

# Start SSH tunnel
start_tunnel() {
    local bg_mode="${1:-false}"

    log "Connecting to ${SSH_USER}@${REMOTE_HOST}..."
    log "Forwarding localhost:${LOCAL_PORT} -> ${REMOTE_HOST}:${REMOTE_PORT}"

    if is_tunnel_running; then
        log_warn "Tunnel already running (pid=$(cat "$PID_FILE"))"
        if health_check; then
            log_ok "Tunnel is healthy"
            return 0
        fi
        log_warn "Tunnel process exists but health check failed, restarting..."
        stop_tunnel
    fi

    # Test SSH connectivity first
    if ! ssh -o ConnectTimeout=10 -o BatchMode=yes "${SSH_USER}@${REMOTE_HOST}" exit 2>/dev/null; then
        log_err "Cannot connect to ${SSH_USER}@${REMOTE_HOST}"
        log "  Ensure SSH keys are configured and the host is reachable."
        log "  Try: ssh ${SSH_USER}@${REMOTE_HOST}"
        return 1
    fi
    log_ok "SSH connection verified"

    if [ "$bg_mode" = "true" ]; then
        # Background mode with auto-reconnect
        ssh -f -N -L "${LOCAL_PORT}:localhost:${REMOTE_PORT}" \
            -o ServerAliveInterval=30 \
            -o ServerAliveCountMax=3 \
            -o ExitOnForwardFailure=yes \
            "${SSH_USER}@${REMOTE_HOST}"

        # Find and save PID
        sleep 1
        local pid
        pid=$(pgrep -f "ssh.*-L.*${LOCAL_PORT}:localhost:${REMOTE_PORT}" | tail -1 || true)
        if [ -n "$pid" ]; then
            echo "$pid" > "$PID_FILE"
            log_ok "Tunnel started in background (pid=${pid})"
        else
            log_err "Failed to start background tunnel"
            return 1
        fi
    else
        # Foreground mode
        log "Running in foreground. Press Ctrl+C to stop."
        ssh -N -L "${LOCAL_PORT}:localhost:${REMOTE_PORT}" \
            -o ServerAliveInterval=30 \
            -o ServerAliveCountMax=3 \
            -o ExitOnForwardFailure=yes \
            "${SSH_USER}@${REMOTE_HOST}"
    fi
}

# Stop SSH tunnel
stop_tunnel() {
    if is_tunnel_running; then
        local pid
        pid=$(cat "$PID_FILE")
        kill "$pid" 2>/dev/null || true
        rm -f "$PID_FILE"
        log_ok "Tunnel stopped (pid=${pid})"
    else
        # Try to find and kill any matching processes
        local pids
        pids=$(pgrep -f "ssh.*-L.*${LOCAL_PORT}:localhost:${REMOTE_PORT}" 2>/dev/null || true)
        if [ -n "$pids" ]; then
            echo "$pids" | xargs kill 2>/dev/null || true
            log_ok "Killed stale tunnel process(es)"
        else
            log "No tunnel process found"
        fi
    fi
}

# Background mode with auto-reconnect loop
run_background() {
    local retries=0

    log "Starting tunnel with auto-reconnect (interval=${RECONNECT_INTERVAL}s)..."

    while true; do
        start_tunnel true

        # Wait and health check
        sleep 3
        if health_check; then
            log_ok "LLM server reachable through tunnel"
            retries=0
        else
            log_warn "Health check failed — LLM server may not be running on remote host"
        fi

        # Monitor tunnel
        while is_tunnel_running; do
            sleep "$RECONNECT_INTERVAL"
            if ! is_tunnel_running; then
                log_warn "Tunnel process died, reconnecting..."
                break
            fi
        done

        retries=$((retries + 1))
        if [ "$MAX_RETRIES" -gt 0 ] && [ "$retries" -ge "$MAX_RETRIES" ]; then
            log_err "Max retries ($MAX_RETRIES) reached, giving up"
            return 1
        fi

        log "Reconnecting in ${RECONNECT_INTERVAL}s (attempt $retries)..."
        sleep "$RECONNECT_INTERVAL"
    done
}

# Show status
show_status() {
    echo "=== Cortex SSH Tunnel Status ==="
    echo "  Remote:   ${SSH_USER}@${REMOTE_HOST}:${REMOTE_PORT}"
    echo "  Local:    localhost:${LOCAL_PORT}"

    if is_tunnel_running; then
        echo -e "  Process:  ${GREEN}running${NC} (pid=$(cat "$PID_FILE"))"
    else
        echo -e "  Process:  ${RED}not running${NC}"
    fi

    if health_check; then
        echo -e "  Health:   ${GREEN}healthy${NC}"
    else
        echo -e "  Health:   ${RED}unreachable${NC}"
    fi
    echo "==============================="
}

# Main
case "${1:-}" in
    --background|-b)
        run_background
        ;;
    --check|-c)
        show_status
        ;;
    --stop|-s)
        stop_tunnel
        ;;
    --help|-h)
        echo "Usage: $0 [OPTION]"
        echo ""
        echo "Options:"
        echo "  (none)          Start tunnel in foreground"
        echo "  --background    Start with auto-reconnect"
        echo "  --check         Show tunnel status"
        echo "  --stop          Stop background tunnel"
        echo "  --help          Show this help"
        echo ""
        echo "Environment variables:"
        echo "  CORTEX_LLM_REMOTE_HOST  Remote host (default: gwhiz1.cis.upenn.edu)"
        echo "  CORTEX_LLM_REMOTE_PORT  Remote port (default: 8800)"
        echo "  CORTEX_LLM_SSH_USER     SSH user (default: wangcy07)"
        echo "  CORTEX_SSH_LOCAL_PORT    Local port (default: 8800)"
        ;;
    *)
        start_tunnel false
        ;;
esac

#!/bin/bash

# EasyADSB - Automated ADS-B Multi-Feeder Setup
# Version: 1.1.0
# Last Updated: 2025-11-29
# 
# One-command setup for 6 ADS-B flight tracking networks
# 
# What we do:
# - Auto-extract all keys and IDs (RadarBox, PiAware)
# - Guided interactive FR24 signup (shows ACTUAL questions!)
# - One-command setup with smart defaults
# - Unified dashboard with real-time stats
# - Easy reconfiguration without losing data
# 
# What we use:
# - ultrafeeder (github.com/sdr-enthusiasts/docker-adsb-ultrafeeder)
# - radarbox, piaware, fr24feed containers by sdr-enthusiasts
# 
# Credits: sdr-enthusiasts team for the amazing feeder images

set -e

# Colors
GREEN='\033[0;32m'
CYAN='\033[1;37m'  # Changed to white for readability
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Get script directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Change to script directory to ensure all paths work correctly
cd "$SCRIPT_DIR"

# Spinner animation function
spin() {
    local spinstr='⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏'
    local temp
    local text="$1"
    while true; do
        for i in $(seq 0 9); do
            temp="${spinstr:$i:1}"
            printf "\r%s - %s" "$temp" "$text"
            sleep 0.1
        done
    done
}

# Stop spinner
stop_spin() {
    if [ -n "$SPIN_PID" ]; then
        kill "$SPIN_PID" 2>/dev/null || true
        wait "$SPIN_PID" 2>/dev/null || true
        printf "\r"
        SPIN_PID=""
    fi
}

# Cleanup on exit or interrupt
cleanup() {
    stop_spin
    docker stop rbfeeder-temp piaware-temp fr24feed-temp 2>/dev/null || true
    docker rm rbfeeder-temp piaware-temp fr24feed-temp 2>/dev/null || true
}

# Handle Ctrl+C gracefully
handle_interrupt() {
    echo ""
    echo ""
    echo -e "${YELLOW}⚠ Setup cancelled by user${NC}"
    cleanup
    exit 130
}

trap cleanup EXIT
trap handle_interrupt INT TERM

clear
echo ""
echo "════════════════════════════════════════════════════════════════════════════════"
echo "              EasyADSB Setup v1.1.0 (15-20 mins)"
echo "════════════════════════════════════════════════════════════════════════════════"
echo ""

# Check for existing .env
if [ -f ".env" ]; then
    echo "Found existing .env file."
    echo ""
    echo "Options:"
    echo "  1) Restart services (keep config)"
    echo "  2) Reconfigure everything"
    echo "  3) Stop all services"
    echo "  4) View status & logs"
    echo "  5) Exit"
    echo ""
    read -p "Choice [1-5]: " choice
    
    case $choice in
        1)
            echo ""
            echo "════════════════════════════════════════════════════════════════════════════════"
            echo "  Restarting Services"
            echo "════════════════════════════════════════════════════════════════════════════════"
            echo ""
            
            # Regenerate dashboard config before restarting
            if [ -f ".env" ]; then
                echo -n "Regenerating dashboard config "
                
                # Load values safely (without executing ULTRAFEEDER_CONFIG)
                ADSBX_UUID=$(grep "^ADSBX_UUID=" .env | cut -d'=' -f2)
                MULTIFEEDER_UUID=$(grep "^MULTIFEEDER_UUID=" .env | cut -d'=' -f2)
                FR24KEY=$(grep "^FR24KEY=" .env | cut -d'=' -f2)
                RADARBOX_KEY=$(grep "^RADARBOX_KEY=" .env | cut -d'=' -f2)
                RADARBOX_SERIAL=$(grep "^RADARBOX_SERIAL=" .env | cut -d'=' -f2)
                PIAWARE_FEEDER_ID=$(grep "^PIAWARE_FEEDER_ID=" .env | cut -d'=' -f2)
                
                cat > dashboard-config.js << JSEOF
// Auto-generated configuration for EasyADSB Dashboard
// Generated: $(date)
window.FEEDER_CONFIG = {
    adsbxUUID: "${ADSBX_UUID}",
    adsbLolUUID: "${MULTIFEEDER_UUID}",
    fr24Key: "${FR24KEY}",
    radarboxKey: "${RADARBOX_KEY}",
    radarboxSerial: "${RADARBOX_SERIAL}",
    piawareID: "${PIAWARE_FEEDER_ID}"
};
JSEOF
                echo -e "${GREEN}✓${NC}"
            fi
            
            spin "Stopping services" &
            SPIN_PID=$!
            docker compose down > /dev/null 2>&1
            stop_spin
            echo -e "${GREEN}✓${NC} Stopping services"
            
            spin "Starting services" &
            SPIN_PID=$!
            docker compose up -d > /dev/null 2>&1
            stop_spin
            echo -e "${GREEN}✓${NC} Starting services"
            
            sleep 2
            
            # Check for RadarBox serial if empty in .env
            RADARBOX_SERIAL=$(grep "^RADARBOX_SERIAL=" .env | cut -d'=' -f2)
            if [ -z "$RADARBOX_SERIAL" ] || [ "$RADARBOX_SERIAL" = "" ]; then
                echo ""
                echo -n "Checking for RadarBox serial "
                sleep 3  # Give RadarBox time to start
                RB_SERIAL=$(docker compose logs radarbox 2>/dev/null | grep -i "station serial number:" | tail -1 | grep -oP 'station serial number:\s*\K[A-Z0-9]+' | tr -d '\r\n')
                
                if [ -n "$RB_SERIAL" ]; then
                    echo -e "${GREEN}✓${NC}"
                    echo "Found RadarBox serial: $RB_SERIAL"
                    
                    # Update .env file
                    if grep -q "^RADARBOX_SERIAL=" .env; then
                        sed -i "s/^RADARBOX_SERIAL=.*/RADARBOX_SERIAL=$RB_SERIAL/" .env
                    else
                        echo "RADARBOX_SERIAL=$RB_SERIAL" >> .env
                    fi
                    
                    # Regenerate dashboard config with new serial
                    ADSBX_UUID=$(grep "^ADSBX_UUID=" .env | cut -d'=' -f2)
                    MULTIFEEDER_UUID=$(grep "^MULTIFEEDER_UUID=" .env | cut -d'=' -f2)
                    FR24KEY=$(grep "^FR24KEY=" .env | cut -d'=' -f2)
                    RADARBOX_KEY=$(grep "^RADARBOX_KEY=" .env | cut -d'=' -f2)
                    PIAWARE_FEEDER_ID=$(grep "^PIAWARE_FEEDER_ID=" .env | cut -d'=' -f2)
                    
                    cat > dashboard-config.js << JSEOF
// Auto-generated configuration for EasyADSB Dashboard
// Generated: $(date)
window.FEEDER_CONFIG = {
    adsbxUUID: "${ADSBX_UUID}",
    adsbLolUUID: "${MULTIFEEDER_UUID}",
    fr24Key: "${FR24KEY}",
    radarboxKey: "${RADARBOX_KEY}",
    radarboxSerial: "${RB_SERIAL}",
    piawareID: "${PIAWARE_FEEDER_ID}"
};
JSEOF
                    echo "✓ Dashboard config updated with serial"
                else
                    echo -e "${YELLOW}⚠${NC}"
                    echo "Serial not found yet (check logs: docker compose logs radarbox | grep serial)"
                fi
            fi
            
            MY_IP=$(hostname -I | awk '{print $1}')
            
            echo ""
            echo "════════════════════════════════════════════════════════════════════════════════"
            echo -e "  ${GREEN}✓ Services Restarted${NC}"
            echo "════════════════════════════════════════════════════════════════════════════════"
            echo ""
            echo "  Dashboard:  http://$MY_IP:8081/"
            echo "  Live Map:   http://$MY_IP:8080/"
            echo ""
            docker compose ps
            echo ""
            exit 0
            ;;
        2)
            # Read existing values before backing up (use grep to avoid executing ULTRAFEEDER_CONFIG)
            if [ -f ".env" ]; then
                OLD_LAT=$(grep "^FEEDER_LAT=" .env | cut -d'=' -f2)
                OLD_LONG=$(grep "^FEEDER_LONG=" .env | cut -d'=' -f2)
                OLD_ALT=$(grep "^FEEDER_ALT_M=" .env | cut -d'=' -f2)
                OLD_TZ=$(grep "^FEEDER_TZ=" .env | cut -d'=' -f2)
                OLD_NAME=$(grep "^FEEDER_NAME=" .env | cut -d'=' -f2)
                OLD_ADSBX=$(grep "^ADSBX_UUID=" .env | cut -d'=' -f2)
                OLD_MULTI=$(grep "^MULTIFEEDER_UUID=" .env | cut -d'=' -f2)
                OLD_RB=$(grep "^RADARBOX_KEY=" .env | cut -d'=' -f2)
                OLD_FR24=$(grep "^FR24KEY=" .env | cut -d'=' -f2)
                OLD_PA=$(grep "^PIAWARE_FEEDER_ID=" .env | cut -d'=' -f2)
            fi
            
            TIMESTAMP=$(date +%Y%m%d_%H%M%S)
            cp .env .env.backup.$TIMESTAMP 2>/dev/null || true
            echo "Backup created: .env.backup.$TIMESTAMP"
            echo ""
            ;;
        3)
            echo ""
            echo "════════════════════════════════════════════════════════════════════════════════"
            echo "  Stopping All Services"
            echo "════════════════════════════════════════════════════════════════════════════════"
            echo ""
            docker compose down
            echo ""
            echo -e "${GREEN}✓ All services stopped${NC}"
            echo ""
            echo -e "To start again, run: ${CYAN}./setup.sh${NC}"
            echo "(Choose option 1 to restart with existing config)"
            echo ""
            exit 0
            ;;
        4)
            # Status and Logs viewer
            while true; do
                clear
                echo "════════════════════════════════════════════════════════════════════════════════"
                echo "  EasyADSB Status & Logs"
                echo "════════════════════════════════════════════════════════════════════════════════"
                echo ""
                
                # Load config safely (use grep to avoid executing ULTRAFEEDER_CONFIG)
                if [ -f ".env" ]; then
                    ADSBX_UUID=$(grep "^ADSBX_UUID=" .env | cut -d'=' -f2)
                    MULTIFEEDER_UUID=$(grep "^MULTIFEEDER_UUID=" .env | cut -d'=' -f2)
                    FR24KEY=$(grep "^FR24KEY=" .env | cut -d'=' -f2)
                    RADARBOX_KEY=$(grep "^RADARBOX_KEY=" .env | cut -d'=' -f2)
                    RADARBOX_SERIAL=$(grep "^RADARBOX_SERIAL=" .env | cut -d'=' -f2)
                    PIAWARE_FEEDER_ID=$(grep "^PIAWARE_FEEDER_ID=" .env | cut -d'=' -f2)
                fi
                
                # Show service status
                echo -e "${GREEN}📊 Service Status:${NC}"
                echo ""
                docker compose ps 2>&1 | grep -v "version.*obsolete"
                echo ""
                
                # Show your IDs/Keys
                echo "════════════════════════════════════════════════════════════════════════════════"
                echo -e "${GREEN}🔑 Your Station IDs:${NC}"
                echo ""
                echo "  ADSBexchange: $ADSBX_UUID"
                echo "  ADSB.lol:     $MULTIFEEDER_UUID"
                echo "  RadarBox:     $RADARBOX_KEY"
                [ -n "$RADARBOX_SERIAL" ] && echo "  RB Serial:    $RADARBOX_SERIAL"
                echo "  FR24:         $FR24KEY"
                echo "  PiAware:      $PIAWARE_FEEDER_ID"
                echo ""
                
                # Log options
                echo "════════════════════════════════════════════════════════════════════════════════"
                echo -e "${GREEN}📋 Log Options:${NC}"
                echo ""
                echo "  1) View recent logs (all services)"
                echo "  2) View ultrafeeder logs"
                echo "  3) View radarbox logs"
                echo "  4) View flightradar24 logs"
                echo "  5) View piaware logs"
                echo "  6) Check for errors only"
                echo "  7) Live logs (follow mode)"
                echo "  8) Restart a service"
                echo "  0) Back to main menu"
                echo ""
                echo -e "${CYAN}💡 Logs show last 50 lines (static). Use option 7 for live streaming.${NC}"
                echo ""
                read -p "Choice [0-8]: " log_choice
                
                case $log_choice in
                    1) 
                        clear
                        echo "Recent logs (all services) - Last 50 lines:"
                        echo "════════════════════════════════════════════════════════════════════════════════"
                        docker compose logs --tail=50 2>&1 | grep -v "version.*obsolete"
                        echo ""
                        read -p "Press Enter to continue..."
                        ;;
                    2) 
                        clear
                        echo "Ultrafeeder logs - Last 50 lines:"
                        echo "════════════════════════════════════════════════════════════════════════════════"
                        docker compose logs --tail=50 ultrafeeder 2>&1 | grep -v "version.*obsolete"
                        echo ""
                        read -p "Press Enter to continue..."
                        ;;
                    3) 
                        clear
                        echo "RadarBox logs - Last 50 lines:"
                        echo "════════════════════════════════════════════════════════════════════════════════"
                        docker compose logs --tail=50 radarbox 2>&1 | grep -v "version.*obsolete"
                        echo ""
                        read -p "Press Enter to continue..."
                        ;;
                    4) 
                        clear
                        echo "FlightRadar24 logs - Last 50 lines:"
                        echo "════════════════════════════════════════════════════════════════════════════════"
                        docker compose logs --tail=50 flightradar24 2>&1 | grep -v "version.*obsolete"
                        echo ""
                        read -p "Press Enter to continue..."
                        ;;
                    5) 
                        clear
                        echo "PiAware logs - Last 50 lines:"
                        echo "════════════════════════════════════════════════════════════════════════════════"
                        docker compose logs --tail=50 piaware 2>&1 | grep -v "version.*obsolete"
                        echo ""
                        read -p "Press Enter to continue..."
                        ;;
                    6) 
                        clear
                        echo "Checking for errors in the last 100 lines..."
                        echo "════════════════════════════════════════════════════════════════════════════════"
                        docker compose logs --tail=100 2>&1 | grep -v "version.*obsolete" | grep -iE "error|fail|warn" || echo "✓ No errors found!"
                        echo ""
                        read -p "Press Enter to continue..."
                        ;;
                    7)
                        clear
                        echo "Live logs (Ctrl+C to stop)"
                        echo "════════════════════════════════════════════════════════════════════════════════"
                        echo ""
                        echo "Which service?"
                        echo "  1) All services"
                        echo "  2) Ultrafeeder only"
                        echo "  3) RadarBox only"
                        echo "  4) FR24 only"
                        echo "  5) PiAware only"
                        echo ""
                        read -p "Choice [1-5]: " live_choice
                        clear
                        case $live_choice in
                            1) docker compose logs -f 2>&1 | grep -v "version.*obsolete" ;;
                            2) docker compose logs -f ultrafeeder 2>&1 | grep -v "version.*obsolete" ;;
                            3) docker compose logs -f radarbox 2>&1 | grep -v "version.*obsolete" ;;
                            4) docker compose logs -f flightradar24 2>&1 | grep -v "version.*obsolete" ;;
                            5) docker compose logs -f piaware 2>&1 | grep -v "version.*obsolete" ;;
                            *) ;;
                        esac
                        ;;
                    8)
                        clear
                        echo "════════════════════════════════════════════════════════════════════════════════"
                        echo "  Restart a Service"
                        echo "════════════════════════════════════════════════════════════════════════════════"
                        echo ""
                        echo "  1) Dashboard only (web UI)"
                        echo "  2) Ultrafeeder"
                        echo "  3) RadarBox"
                        echo "  4) FlightRadar24"
                        echo "  5) PiAware"
                        echo "  6) All services"
                        echo "  0) Cancel"
                        echo ""
                        read -p "Which service? [0-6]: " svc_choice
                        
                        case $svc_choice in
                            1) 
                                echo ""
                                # Regenerate dashboard config
                                if [ -f ".env" ]; then
                                    echo -n "Regenerating dashboard config "
                                    
                                    # Load values safely (without executing ULTRAFEEDER_CONFIG)
                                    ADSBX_UUID=$(grep "^ADSBX_UUID=" .env | cut -d'=' -f2)
                                    MULTIFEEDER_UUID=$(grep "^MULTIFEEDER_UUID=" .env | cut -d'=' -f2)
                                    FR24KEY=$(grep "^FR24KEY=" .env | cut -d'=' -f2)
                                    RADARBOX_KEY=$(grep "^RADARBOX_KEY=" .env | cut -d'=' -f2)
                                    RADARBOX_SERIAL=$(grep "^RADARBOX_SERIAL=" .env | cut -d'=' -f2)
                                    PIAWARE_FEEDER_ID=$(grep "^PIAWARE_FEEDER_ID=" .env | cut -d'=' -f2)
                                    
                                    cat > dashboard-config.js << JSEOF
// Auto-generated configuration for EasyADSB Dashboard
// Generated: $(date)
window.FEEDER_CONFIG = {
    adsbxUUID: "${ADSBX_UUID}",
    adsbLolUUID: "${MULTIFEEDER_UUID}",
    fr24Key: "${FR24KEY}",
    radarboxKey: "${RADARBOX_KEY}",
    radarboxSerial: "${RADARBOX_SERIAL}",
    piawareID: "${PIAWARE_FEEDER_ID}"
};
JSEOF
                                    echo -e "${GREEN}✓${NC}"
                                fi
                                
                                spin "Restarting dashboard" &
                                SPIN_PID=$!
                                docker compose restart dashboard > /dev/null 2>&1
                                stop_spin
                                echo -e "${GREEN}✓${NC} Restarting dashboard"
                                echo ""
                                MY_IP=$(hostname -I | awk '{print $1}')
                                echo "Dashboard restarted! View at: http://$MY_IP:8081/"
                                echo ""
                                ;;
                            2) 
                                echo ""
                                spin "Restarting ultrafeeder" &
                                SPIN_PID=$!
                                docker compose restart ultrafeeder > /dev/null 2>&1
                                stop_spin
                                echo -e "${GREEN}✓${NC} Restarting ultrafeeder"
                                ;;
                            3)
                                echo ""
                                spin "Restarting radarbox" &
                                SPIN_PID=$!
                                docker compose restart radarbox > /dev/null 2>&1
                                stop_spin
                                echo -e "${GREEN}✓${NC} Restarting radarbox"
                                ;;
                            4)
                                echo ""
                                spin "Restarting flightradar24" &
                                SPIN_PID=$!
                                docker compose restart flightradar24 > /dev/null 2>&1
                                stop_spin
                                echo -e "${GREEN}✓${NC} Restarting flightradar24"
                                ;;
                            5)
                                echo ""
                                spin "Restarting piaware" &
                                SPIN_PID=$!
                                docker compose restart piaware > /dev/null 2>&1
                                stop_spin
                                echo -e "${GREEN}✓${NC} Restarting piaware"
                                ;;
                            6)
                                echo ""
                                spin "Restarting all services" &
                                SPIN_PID=$!
                                docker compose restart > /dev/null 2>&1
                                stop_spin
                                echo -e "${GREEN}✓${NC} Restarting all services"
                                ;;
                            0|*)
                                ;;
                        esac
                        echo ""
                        read -p "Press Enter to continue..."
                        ;;
                    0) 
                        break
                        ;;
                    *) 
                        ;;
                esac
            done
            exec "$0"
            ;;
        5)
            echo ""
            echo "Exiting."
            exit 0
            ;;
        *)
            echo "Invalid choice. Exiting."
            exit 1
            ;;
    esac
fi

# Check Docker
echo -n "Checking Docker... "
if command -v docker &> /dev/null; then
    echo -e "${GREEN}✓${NC}"
else
    echo -e "${YELLOW}✗${NC}"
    read -p "Docker not found. Install now? (y/n): " -n 1 -r
    echo ""
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        echo "Installing Docker..."
        curl -fsSL https://get.docker.com | sudo bash > /dev/null 2>&1
        sudo usermod -aG docker $USER
        echo -e "${GREEN}✓ Docker installed${NC}"
        echo ""
        echo "Please log out and back in, then run this script again."
        exit 0
    else
        echo "Docker is required. Exiting."
        exit 1
    fi
fi

# Check RTL-SDR
echo -n "Checking RTL-SDR... "
if lsusb 2>/dev/null | grep -iq "realtek"; then
    echo -e "${GREEN}✓${NC}"
else
    echo -e "${YELLOW}✗${NC}"
    read -p "RTL-SDR not detected. Continue anyway? (y/n): " -n 1 -r
    echo ""
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        exit 1
    fi
fi

# Check for dashboard.html
echo -n "Checking dashboard.html... "
if [ -f "dashboard.html" ]; then
    echo -e "${GREEN}✓${NC}"
else
    echo -e "${YELLOW}⚠${NC}"
    echo ""
    echo "  Dashboard HTML file not found in script directory."
    echo "  The web interface (http://YOUR-IP:8081) won't work without it."
    echo ""
    echo "  To fix: Download dashboard.html and place it in: $SCRIPT_DIR"
    echo ""
    read -p "Continue without dashboard? (y/n): " -n 1 -r
    echo ""
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        exit 0
    fi
fi

# All checks passed - confirm to proceed
echo ""
echo "════════════════════════════════════════════════════════════════════════════════"
echo "  ✓ All Prerequisites Met!"
echo "════════════════════════════════════════════════════════════════════════════════"
echo ""
echo "  Ready to configure your ADS-B multi-feeder setup."
echo "  This will take 15-20 minutes and will:"
echo ""
echo "    • Create configuration files (.env)"
echo "    • Set up 5 Docker containers"
echo "    • Auto-generate feed credentials"
echo "    • Start all services"
echo ""
read -p "Continue with setup? (y/n): " -n 1 -r
echo ""
if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    # User chose not to continue - show management menu instead
    clear
    echo ""
    echo "════════════════════════════════════════════════════════════════════════════════"
    echo "  EasyADSB Management Menu"
    echo "════════════════════════════════════════════════════════════════════════════════"
    echo ""
    echo "  1) Restart all services"
    echo "  2) View logs (all services)"
    echo "  3) View logs (individual service)"
    echo "  4) Update Docker images"
    echo "  5) View current configuration"
    echo "  6) Reconfigure everything (fresh setup)"
    echo "  7) Update EasyADSB (pull from GitHub)"
    echo "  8) Uninstall EasyADSB"
    echo "  9) Exit"
    echo ""
    read -p "Choice [1-9]: " menu_choice
    
    case $menu_choice in
        1)
            echo ""
            echo "════════════════════════════════════════════════════════════════════════════════"
            echo "  Restarting Services"
            echo "════════════════════════════════════════════════════════════════════════════════"
            docker compose restart
            echo ""
            echo "✓ Services restarted"
            echo "  Dashboard: http://$(hostname -I | awk '{print $1}'):8081"
            exit 0
            ;;
        2)
            docker compose logs --tail=50 --follow
            exit 0
            ;;
        3)
            echo ""
            echo "Select service:"
            echo "  1) ultrafeeder"
            echo "  2) flightradar24"
            echo "  3) radarbox"
            echo "  4) piaware"
            echo "  5) dashboard"
            read -p "Choice: " svc_choice
            case $svc_choice in
                1) docker compose logs ultrafeeder --tail=50 --follow ;;
                2) docker compose logs flightradar24 --tail=50 --follow ;;
                3) docker compose logs radarbox --tail=50 --follow ;;
                4) docker compose logs piaware --tail=50 --follow ;;
                5) docker compose logs dashboard --tail=50 --follow ;;
            esac
            exit 0
            ;;
        4)
            echo ""
            echo "════════════════════════════════════════════════════════════════════════════════"
            echo "  Updating Docker Images"
            echo "════════════════════════════════════════════════════════════════════════════════"
            docker compose pull
            docker compose up -d
            echo "✓ Images updated and services restarted"
            exit 0
            ;;
        5)
            echo ""
            echo "════════════════════════════════════════════════════════════════════════════════"
            echo "  Current Configuration"
            echo "════════════════════════════════════════════════════════════════════════════════"
            cat .env
            exit 0
            ;;
        6)
            echo ""
            echo "Starting fresh configuration..."
            echo ""
            # Continue to setup below
            ;;
        7)
            # Update EasyADSB from GitHub
            echo ""
            echo "════════════════════════════════════════════════════════════════════════════════"
            echo "  Update EasyADSB"
            echo "════════════════════════════════════════════════════════════════════════════════"
            echo ""
            
            if [ ! -d ".git" ]; then
                echo -e "${RED}✗${NC} Not a git repository"
                echo "  This directory was not cloned from GitHub."
                echo "  Manual update required - download latest files from:"
                echo "  https://github.com/datboip/EasyADSB"
                exit 1
            fi
            
            echo "Checking for updates..."
            git fetch origin main
            
            LOCAL=$(git rev-parse @)
            REMOTE=$(git rev-parse @{u})
            
            if [ $LOCAL = $REMOTE ]; then
                echo ""
                echo "✓ Already up to date!"
                exit 0
            fi
            
            echo ""
            echo "Updates available!"
            git log --oneline HEAD..origin/main | head -5
            echo ""
            read -p "Pull updates and restart? (y/n): " -n 1 -r
            echo ""
            
            if [[ $REPLY =~ ^[Yy]$ ]]; then
                # Backup current .env
                if [ -f ".env" ]; then
                    cp .env .env.backup.$(date +%Y%m%d_%H%M%S)
                    echo "✓ Backed up .env"
                fi
                
                # Pull updates
                echo "Pulling updates..."
                git pull origin main
                echo ""
                
                # Check if setup.sh changed
                if git diff HEAD@{1} HEAD --name-only | grep -q "setup.sh"; then
                    echo -e "${YELLOW}⚠${NC} setup.sh was updated"
                    echo "  Run ./setup.sh again to apply changes"
                fi
                
                # Check if .env.example has new fields
                if git diff HEAD@{1} HEAD --name-only | grep -q ".env.example"; then
                    echo -e "${YELLOW}⚠${NC} New configuration options available"
                    echo "  Check .env.example for new fields"
                fi
                
                # Regenerate dashboard config
                if [ -f ".env" ]; then
                    echo ""
                    echo "Regenerating dashboard config..."
                    source .env
                    cat > dashboard-config.js << JSEOF
// Auto-generated configuration for EasyADSB Dashboard
// Generated: $(date)
window.FEEDER_CONFIG = {
    adsbxUUID: "${ADSBX_UUID}",
    adsbLolUUID: "${MULTIFEEDER_UUID}",
    fr24Key: "${FR24KEY}",
    radarboxKey: "${RADARBOX_KEY}",
    radarboxSerial: "${RADARBOX_SERIAL}",
    piawareID: "${PIAWARE_FEEDER_ID}"
};
JSEOF
                    echo "✓ Dashboard config updated"
                fi
                
                # Restart services
                echo ""
                echo "Restarting services..."
                docker compose pull
                docker compose up -d
                echo ""
                echo "✓ Update complete!"
                echo "  Dashboard: http://$(hostname -I | awk '{print $1}'):8081"
            fi
            exit 0
            ;;
        8)
            # Uninstall EasyADSB
            echo ""
            echo "════════════════════════════════════════════════════════════════════════════════"
            echo "  Uninstall EasyADSB"
            echo "════════════════════════════════════════════════════════════════════════════════"
            echo ""
            echo -e "${RED}⚠ WARNING:${NC} This will remove all EasyADSB containers and data!"
            echo ""
            read -p "Are you sure you want to uninstall? (yes/no): " confirm
            
            if [ "$confirm" != "yes" ]; then
                echo "Cancelled."
                exit 0
            fi
            
            echo ""
            echo "Stopping and removing containers..."
            docker compose down
            echo "✓ Containers removed"
            
            echo ""
            read -p "Remove data volumes? (/opt/adsb) (y/n): " -n 1 -r
            echo ""
            if [[ $REPLY =~ ^[Yy]$ ]]; then
                sudo rm -rf /opt/adsb
                echo "✓ Data volumes removed"
            fi
            
            echo ""
            read -p "Remove configuration files? (.env, dashboard-config.js) (y/n): " -n 1 -r
            echo ""
            if [[ $REPLY =~ ^[Yy]$ ]]; then
                rm -f .env .env.backup.* dashboard-config.js
                echo "✓ Configuration files removed"
            fi
            
            echo ""
            echo "✓ Uninstall complete!"
            echo ""
            echo "To remove EasyADSB completely:"
            echo "  cd .. && rm -rf easyadsb"
            exit 0
            ;;
        9)
            exit 0
            ;;
        *)
            echo "Invalid choice. Exiting."
            exit 1
            ;;
    esac
fi

echo ""
echo "════════════════════════════════════════════════════════════════════════════════"
echo "  Location"
echo "════════════════════════════════════════════════════════════════════════════════"
echo ""

# Check if we have old values
if [ -n "$OLD_LAT" ] && [ -n "$OLD_LONG" ]; then
    echo "Found existing location:"
    echo "  Latitude:  $OLD_LAT"
    echo "  Longitude: $OLD_LONG"
    echo "  Altitude:  ${OLD_ALT}m"
    echo "  Timezone:  $OLD_TZ"
    echo "  Name:      $OLD_NAME"
    echo ""
    read -p "Keep this configuration? (y/n): " -n 1 -r
    echo ""
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        FEEDER_LAT=$OLD_LAT
        FEEDER_LONG=$OLD_LONG
        FEEDER_ALT_M=$OLD_ALT
        FEEDER_TZ=$OLD_TZ
        FEEDER_NAME=$OLD_NAME
        echo -e "${GREEN}✓ Keeping existing location${NC}"
        REUSE_LOCATION=true
    else
        REUSE_LOCATION=false
    fi
else
    REUSE_LOCATION=false
fi

if [ "$REUSE_LOCATION" != "true" ]; then
    read -p "Latitude (e.g., 40.6892): " FEEDER_LAT
    read -p "Longitude (e.g., -74.0445): " FEEDER_LONG
    read -p "Altitude in meters (e.g., 10): " FEEDER_ALT_M
    FEEDER_ALT_M=${FEEDER_ALT_M:-10}

    echo ""
    echo "Select timezone:"
    echo "  1) America/New_York (US Eastern)"
    echo "  2) America/Chicago (US Central)"
    echo "  3) America/Denver (US Mountain)"
    echo "  4) America/Los_Angeles (US Pacific)"
    echo "  5) Europe/London (UK)"
    echo "  6) Europe/Paris (Central Europe)"
    echo "  7) Asia/Tokyo (Japan)"
    echo "  8) Australia/Sydney (Australia)"
    echo "  9) Other (enter manually)"
    read -p "Choice [1-9]: " tz_choice

    case $tz_choice in
        1) FEEDER_TZ="America/New_York" ;;
        2) FEEDER_TZ="America/Chicago" ;;
        3) FEEDER_TZ="America/Denver" ;;
        4) FEEDER_TZ="America/Los_Angeles" ;;
        5) FEEDER_TZ="Europe/London" ;;
        6) FEEDER_TZ="Europe/Paris" ;;
        7) FEEDER_TZ="Asia/Tokyo" ;;
        8) FEEDER_TZ="Australia/Sydney" ;;
        9) read -p "Enter timezone (e.g., America/New_York): " FEEDER_TZ ;;
        *) FEEDER_TZ="America/New_York" ;;
    esac

    read -p "Feeder name (e.g., MyFeeder): " FEEDER_NAME
    FEEDER_NAME=${FEEDER_NAME:-MyFeeder}

    echo ""
    echo -e "${GREEN}✓ Location configured${NC}"
fi

# Ask about existing keys
echo ""
read -p "Have you set up ADS-B feeding before? (y/n): " -n 1 -r
echo ""
if [[ $REPLY =~ ^[Yy]$ ]]; then
    HAS_EXISTING_SETUP=true
else
    HAS_EXISTING_SETUP=false
fi

# Generate UUIDs
echo ""
echo -n "Generating UUIDs... "
MULTIFEEDER_UUID=$(cat /proc/sys/kernel/random/uuid)
ADSBX_UUID=$(cat /proc/sys/kernel/random/uuid)
echo -e "${GREEN}✓${NC}"

# RadarBox Key
echo ""
echo "════════════════════════════════════════════════════════════════════════════════"
echo "  RadarBox"
echo "════════════════════════════════════════════════════════════════════════════════"
echo ""

# Check if we have an old key
if [ -n "$OLD_RB" ] && [ "$OLD_RB" != "YOUR-RADARBOX-KEY" ]; then
    echo "Found existing RadarBox key: $OLD_RB"
    read -p "Keep this key? (y/n): " -n 1 -r
    echo ""
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        RADARBOX_KEY=$OLD_RB
        echo -e "${GREEN}✓ Keeping existing RadarBox key${NC}"
        HAS_RB_KEY=true
    else
        HAS_RB_KEY=false
    fi
elif [[ $HAS_EXISTING_SETUP == true ]]; then
    read -p "Have existing RadarBox key? (y/n): " -n 1 -r
    echo ""
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        read -p "Enter your RadarBox sharing key: " RADARBOX_KEY
        HAS_RB_KEY=true
        # Also ask for serial if they have it
        echo ""
        read -p "Do you also have your RadarBox serial? (y/n): " -n 1 -r
        echo ""
        if [[ $REPLY =~ ^[Yy]$ ]]; then
            read -p "Enter your RadarBox serial (e.g., EXTRPI123456): " RADARBOX_SERIAL
            if [ -z "$RADARBOX_SERIAL" ]; then
                RADARBOX_SERIAL=""
                echo "  (Serial will be auto-detected on first run)"
            fi
        else
            RADARBOX_SERIAL=""
            echo "  (Serial will be auto-detected on first run)"
        fi
    else
        HAS_RB_KEY=false
    fi
else
    HAS_RB_KEY=false
fi

if [[ ${HAS_RB_KEY:-false} == false ]]; then
    echo ""
    echo "RadarBox requires a sharing key."
    echo ""
    echo "Options:"
    echo "  1) Try auto-generation (recommended - needs RTL-SDR with aircraft)"
    echo "  2) Enter key manually"
    echo "  3) Skip for now (add to .env later)"
    echo ""
    read -p "Choice [1-3]: " rb_choice
    
    case $rb_choice in
        1|"")
            echo ""
            echo "════════════════════════════════════════════════════════════════════════════════"
            echo "  RadarBox Key Generation"
            echo "════════════════════════════════════════════════════════════════════════════════"
            echo ""
            echo "Generating your RadarBox credentials..."
            echo -e "${CYAN}💡 Full logs: /tmp/rbfeeder.log.backup${NC}"
            echo ""
            
            rm -f /tmp/rbfeeder.log
            
            # Run container silently, show clean status
            (
                docker run --rm --name rbfeeder-temp \
                  --device /dev/bus/usb:/dev/bus/usb \
                  -e LAT=$FEEDER_LAT \
                  -e LONG=$FEEDER_LONG \
                  -e ALT=$FEEDER_ALT_M \
                  -e BEASTHOST=127.0.0.1 \
                  ghcr.io/sdr-enthusiasts/docker-radarbox:latest > /tmp/rbfeeder.log 2>&1
            ) &
            
            RB_PID=$!
            
            # Monitor for key and serial with clean status updates
            spin "⏳ Waiting for key" &
            SPIN_PID=$!
            
            KEY_FOUND=false
            SERIAL_FOUND=false
            
            for i in {1..120}; do
                if [ "$KEY_FOUND" = false ] && grep -q "Your new key is" /tmp/rbfeeder.log 2>/dev/null; then
                    stop_spin
                    RADARBOX_KEY=$(grep -i "Your new key is" /tmp/rbfeeder.log | tail -1 | grep -oP 'Your new key is \K[a-f0-9]{32}' | tr -d '\r\n')
                    if [ -n "$RADARBOX_KEY" ]; then
                        echo -e "${GREEN}✓${NC} Key: $RADARBOX_KEY"
                        spin "⏳ Waiting for serial" &
                        SPIN_PID=$!
                        KEY_FOUND=true
                    fi
                fi
                
                if [ "$KEY_FOUND" = true ] && [ "$SERIAL_FOUND" = false ] && grep -q "station serial number:" /tmp/rbfeeder.log 2>/dev/null; then
                    stop_spin
                    RADARBOX_SERIAL=$(grep -i "station serial number:" /tmp/rbfeeder.log | tail -1 | grep -oP 'station serial number:\s*\K[A-Z0-9]+' | tr -d '\r\n')
                    if [ -n "$RADARBOX_SERIAL" ]; then
                        echo -e "${GREEN}✓${NC} Serial: $RADARBOX_SERIAL"
                        SERIAL_FOUND=true
                        docker stop rbfeeder-temp > /dev/null 2>&1 || true
                        break
                    fi
                fi
                
                sleep 1
            done
            
            stop_spin
            echo ""
            
            # Ensure container is stopped
            docker stop rbfeeder-temp > /dev/null 2>&1 || true
            wait $RB_PID 2>/dev/null || true
            sleep 1
            
            # Final extraction if not found during monitoring
            if [ -z "$RADARBOX_KEY" ]; then
                RADARBOX_KEY=$(grep -i "Your new key is\|Your key is:\|sharing key:" /tmp/rbfeeder.log 2>/dev/null | grep -oP '[a-f0-9]{32}' | tail -1 | tr -d '\r\n')
            fi
            
            if [ -z "$RADARBOX_SERIAL" ]; then
                RADARBOX_SERIAL=$(grep -i "station serial number:" /tmp/rbfeeder.log 2>/dev/null | grep -oP 'station serial number:\s*\K[A-Z0-9]+' | tr -d '\r\n')
            fi
            
            echo ""
            if [ -n "$RADARBOX_KEY" ] && [ ${#RADARBOX_KEY} -eq 32 ]; then
                if [ "$KEY_FOUND" = false ]; then
                    echo -e "${GREEN}✓ Key: $RADARBOX_KEY${NC}"
                fi
                if [ -n "$RADARBOX_SERIAL" ] && [ "$SERIAL_FOUND" = false ]; then
                    echo -e "${GREEN}✓ Serial: $RADARBOX_SERIAL${NC}"
                fi
                echo -e "${GREEN}✓ RadarBox credentials saved${NC}"
            else
                echo -e "${YELLOW}⚠ Could not auto-extract key.${NC}"
                echo "Check log: cat /tmp/rbfeeder.log | grep -i 'key'"
                echo ""
                read -t 10 -p "Enter key manually (or wait 10s to skip): " RADARBOX_KEY || true
                if [ -z "$RADARBOX_KEY" ]; then
                    RADARBOX_KEY="YOUR-RADARBOX-KEY"
                    RADARBOX_SERIAL=""
                fi
            fi
            
            cp /tmp/rbfeeder.log /tmp/rbfeeder.log.backup 2>/dev/null || true
            ;;
        2)
            read -p "Enter your RadarBox sharing key: " RADARBOX_KEY
            if [ -z "$RADARBOX_KEY" ]; then
                RADARBOX_KEY="YOUR-RADARBOX-KEY"
                RADARBOX_SERIAL=""
            else
                # Ask for serial too if they have it
                echo ""
                read -p "Do you also have your RadarBox serial? (y/n): " -n 1 -r
                echo ""
                if [[ $REPLY =~ ^[Yy]$ ]]; then
                    read -p "Enter your RadarBox serial (e.g., EXTRPI123456): " RADARBOX_SERIAL
                    if [ -z "$RADARBOX_SERIAL" ]; then
                        RADARBOX_SERIAL=""
                        echo "  (Serial will be auto-detected on first run)"
                    fi
                else
                    RADARBOX_SERIAL=""
                    echo "  (Serial will be auto-detected on first run)"
                fi
            fi
            ;;
        3)
            RADARBOX_KEY="YOUR-RADARBOX-KEY"
            RADARBOX_SERIAL=""
            echo -e "${YELLOW}⚠ Skipping RadarBox. You can add the key to .env later.${NC}"
            echo "  Visit: https://www.radarbox.com/raspberry-pi"
            ;;
        *)
            RADARBOX_KEY="YOUR-RADARBOX-KEY"
            RADARBOX_SERIAL=""
            ;;
    esac
fi
echo ""

# FlightRadar24 Key
echo ""
echo "════════════════════════════════════════════════════════════════════════════════"
echo "  FlightRadar24"
echo "════════════════════════════════════════════════════════════════════════════════"
echo ""

# Check if we have an old key
if [ -n "$OLD_FR24" ] && [ "$OLD_FR24" != "YOUR-FR24-KEY" ]; then
    echo "Found existing FR24 key: $OLD_FR24"
    read -p "Keep this key? (y/n): " -n 1 -r
    echo ""
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        FR24KEY=$OLD_FR24
        echo -e "${GREEN}✓ Keeping existing FR24 key${NC}"
        HAS_FR24_KEY=true
    else
        HAS_FR24_KEY=false
    fi
elif [[ $HAS_EXISTING_SETUP == true ]]; then
    read -p "Have existing FR24 key? (y/n): " -n 1 -r
    echo ""
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        read -p "Enter your FR24 sharing key: " FR24KEY
        HAS_FR24_KEY=true
    else
        HAS_FR24_KEY=false
    fi
else
    HAS_FR24_KEY=false
fi

if [[ ${HAS_FR24_KEY:-false} == false ]]; then
    # Convert altitude to feet for FR24 (they use feet, we store meters)
    FEEDER_ALT_FT=$(awk "BEGIN {printf \"%.0f\", $FEEDER_ALT_M * 3.28084}")
    
    echo "FlightRadar24 requires email signup."
    echo ""
    echo "Options:"
    echo "  1) Sign up with FR24 (recommended)"
    echo "  2) Enter key manually (if you already have one)"
    echo "  3) Skip for now (add key to .env later)"
    echo ""
    read -p "Choice [1-3]: " fr24_choice
    
    case $fr24_choice in
        1|"")
            clear
            echo "════════════════════════════════════════════════════════════════════════════════"
            echo "           FlightRadar24 Interactive Signup"
            echo "════════════════════════════════════════════════════════════════════════════════"
            echo ""
            echo "You'll be asked several questions. Here's what to answer:"
            echo ""
            echo "  1. Email:             [Your real email]"
            echo "  2. Sharing key:       ⚠️  IMPORTANT - Read below!"
            echo "                        • Have existing key? → Enter it here"
            echo "                        • New to FR24? → Press ENTER (creates new)"
            echo "  3. MLAT:              Type: yes"
            echo "  4. Latitude:          Type: $FEEDER_LAT"
            echo "  5. Longitude:         Type: $FEEDER_LONG"
            echo "  6. Altitude (feet):   Type: $FEEDER_ALT_FT"
            echo "  7. Confirm settings:  Type: yes"
            echo "  8. Receiver type:     Type: 1  (DVBT Stick = RTL-SDR dongle)"
            echo "  9. Dump1090 args:     [Press ENTER - leave empty]"
            echo " 10. RAW port 30002:    Type: yes"
            echo " 11. Basestation 30003: Type: yes"
            echo ""
            echo -e "${YELLOW}⚠️  WARNING: Leaving sharing key blank creates a NEW account!${NC}"
            echo -e "${YELLOW}   If you have an existing FR24 key, enter it at step 2!${NC}"
            echo ""
            echo -e "${CYAN}💡 The script will automatically extract your key when signup completes${NC}"
            echo ""
            read -p "Press Enter when ready to start..."
            echo ""
            
            # Run interactive signup in background, capture output
            rm -f /tmp/fr24_signup.log
            docker run -it --name fr24feed-temp \
              --network host \
              --device /dev/bus/usb:/dev/bus/usb \
              -e BEASTHOST=127.0.0.1 \
              --entrypoint /usr/bin/fr24feed \
              ghcr.io/sdr-enthusiasts/docker-flightradar24:latest \
              --signup 2>&1 | tee /tmp/fr24_signup.log &
            
            FR24_PID=$!
            
            # Monitor for completion in background
            (
                while kill -0 $FR24_PID 2>/dev/null; do
                    if grep -q "Saving settings to /etc/fr24feed.ini...OK" /tmp/fr24_signup.log 2>/dev/null; then
                        sleep 2  # Give it a moment to finish writing
                        docker stop fr24feed-temp >/dev/null 2>&1
                        break
                    fi
                    sleep 1
                done
            ) &
            
            MONITOR_PID=$!
            
            # Wait for container to finish (either naturally or killed by monitor)
            wait $FR24_PID 2>/dev/null || true
            wait $MONITOR_PID 2>/dev/null || true
            
            # Cleanup
            docker rm -f fr24feed-temp >/dev/null 2>&1 || true
            
            echo ""
            echo "════════════════════════════════════════════════════════════════════════════════"
            echo ""
            
            # Try to auto-extract the key from the output
            FR24KEY=$(grep "Your sharing key" /tmp/fr24_signup.log | grep -oP '\([a-f0-9]{16}\)' | tr -d '()' 2>/dev/null || echo "")
            
            if [ -n "$FR24KEY" ] && [ ${#FR24KEY} -eq 16 ]; then
                echo -e "${GREEN}✓ FR24 key auto-extracted: $FR24KEY${NC}"
                read -p "Is this correct? (y/n): " -n 1 -r
                echo ""
                if [[ ! $REPLY =~ ^[Yy]$ ]]; then
                    read -p "Enter the correct FR24 sharing key: " FR24KEY
                fi
            else
                echo "Look for this line above:"
                echo "  'Your sharing key (XXXXXXXX) has been configured...'"
                echo ""
                read -p "Enter the FR24 sharing key from above: " FR24KEY
            fi
            
            if [ -z "$FR24KEY" ] || [ "$FR24KEY" = "signup" ]; then
                FR24KEY="YOUR-FR24-KEY"
                echo -e "${YELLOW}⚠ No key entered. Add it to .env later or check:${NC}"
                echo "  https://www.flightradar24.com/share-your-data"
            else
                # Clean up the key if it has the fr24key= prefix
                FR24KEY=$(echo "$FR24KEY" | sed 's/fr24key=//' | sed 's/"//g' | tr -d '\r\n ')
                echo -e "${GREEN}✓ FR24 key saved${NC}"
                echo ""
                echo "Next steps:"
                echo "  1. Check your email for confirmation from FR24"
                echo "  2. After services start, visit: https://www.flightradar24.com/account/data-sharing"
                echo "  3. Login with your email to see your station stats"
            fi
            
            rm -f /tmp/fr24feed.log
            ;;
        2)
            echo ""
            read -p "Enter your FR24 sharing key: " FR24KEY
            if [ -z "$FR24KEY" ]; then
                FR24KEY="YOUR-FR24-KEY"
                echo -e "${YELLOW}⚠ No key entered.${NC}"
            else
                FR24KEY=$(echo "$FR24KEY" | sed 's/fr24key=//' | sed 's/"//g' | tr -d '\r\n ')
                echo -e "${GREEN}✓ FR24 key saved${NC}"
            fi
            ;;
        3|*)
            FR24KEY="YOUR-FR24-KEY"
            echo -e "${YELLOW}⚠ Skipping FR24. Visit to sign up later:${NC}"
            echo "  https://www.flightradar24.com/share-your-data"
            ;;
    esac
fi

# PiAware ID
echo ""
echo "════════════════════════════════════════════════════════════════════════════════"
echo "  PiAware (FlightAware)"
echo "════════════════════════════════════════════════════════════════════════════════"
echo ""

# Check if we have an old ID
if [ -n "$OLD_PA" ] && [ "$OLD_PA" != "YOUR-PIAWARE-FEEDER-ID" ]; then
    echo "Found existing PiAware ID: $OLD_PA"
    read -p "Keep this ID? (y/n): " -n 1 -r
    echo ""
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        PIAWARE_FEEDER_ID=$OLD_PA
        echo -e "${GREEN}✓ Keeping existing PiAware ID${NC}"
        HAS_PA_ID=true
    else
        # User wants new ID - clear the variable
        PIAWARE_FEEDER_ID=""
        HAS_PA_ID=false
    fi
elif [[ $HAS_EXISTING_SETUP == true ]]; then
    read -p "Have existing PiAware Feeder ID? (y/n): " -n 1 -r
    echo ""
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        read -p "Enter your PiAware Feeder ID: " PIAWARE_FEEDER_ID
        HAS_PA_ID=true
    else
        PIAWARE_FEEDER_ID=""
        HAS_PA_ID=false
    fi
else
    PIAWARE_FEEDER_ID=""
    HAS_PA_ID=false
fi

if [[ ${HAS_PA_ID:-false} == false ]]; then
    echo ""
    echo "Options:"
    echo "  1) Try auto-generation (recommended - needs RTL-SDR with aircraft)"
    echo "  2) Enter ID manually (if you already have one)"
    echo "  3) Skip for now (get ID from logs after starting)"
    echo ""
    read -p "Choice [1-3]: " pa_choice
    
    case $pa_choice in
        1|"")
            echo ""
            echo "════════════════════════════════════════════════════════════════════════════════"
            echo "  PiAware ID Auto-Generation"
            echo "════════════════════════════════════════════════════════════════════════════════"
            echo ""
            
            # Quick RTL-SDR check
            SKIP_PIAWARE=false
            if ! lsusb | grep -iE "rtl|realtek" > /dev/null 2>&1; then
                echo -e "${YELLOW}⚠ Warning: RTL-SDR not detected via lsusb${NC}"
                echo "This may cause the container to fail."
                echo ""
                read -p "Continue anyway? (y/n): " -n 1 -r
                echo ""
                if [[ ! $REPLY =~ ^[Yy]$ ]]; then
                    SKIP_PIAWARE=true
                fi
            fi
            
            if [ "$SKIP_PIAWARE" = true ]; then
                PIAWARE_FEEDER_ID="YOUR-PIAWARE-FEEDER-ID"
                echo -e "${YELLOW}⚠ Skipped. Get ID from logs after starting.${NC}"
            else
                # Actually generate the ID
                echo "Generating your PiAware Feeder ID..."
                echo ""
                
                # First, check if there's already a running container we can use
                if docker ps -a | grep -q piaware-temp 2>/dev/null; then
                    # Check if it has an ID in its logs
                    EXISTING_ID=$(docker logs piaware-temp 2>&1 | grep -i "my feeder ID is" | tail -1 | grep -oP 'my feeder ID is \K[a-f0-9-]+' | tr -d '\r\n' || echo "")
                    if [ -n "$EXISTING_ID" ] && [ ${#EXISTING_ID} -gt 10 ]; then
                        echo -e "${CYAN}Found existing PiAware container with ID: $EXISTING_ID${NC}"
                        read -p "Use this ID? (y/n): " -n 1 -r
                        echo ""
                        if [[ $REPLY =~ ^[Yy]$ ]]; then
                            PIAWARE_FEEDER_ID="$EXISTING_ID"
                            echo -e "${GREEN}✓ Using existing ID${NC}"
                            # Clean up the old container
                            docker rm -f piaware-temp >/dev/null 2>&1 || true
                            # Skip generation - ID is set, will show claim message at end
                        else
                            echo "Removing old container and generating new ID..."
                            docker rm -f piaware-temp >/dev/null 2>&1 || true
                        fi
                    else
                        docker rm -f piaware-temp >/dev/null 2>&1 || true
                    fi
                fi
                
                # Only generate if we don't have an ID yet
                if [ -z "$PIAWARE_FEEDER_ID" ] || [ "$PIAWARE_FEEDER_ID" = "YOUR-PIAWARE-FEEDER-ID" ]; then
                
                rm -f /tmp/piaware.log
                touch /tmp/piaware.log
                
                # Run container in background with output to file
                docker run --rm --name piaware-temp \
                  --device /dev/bus/usb:/dev/bus/usb \
                  -e LAT=$FEEDER_LAT \
                  -e LONG=$FEEDER_LONG \
                  -e ALT=$FEEDER_ALT_M \
                  -e BEASTHOST=127.0.0.1 \
                  ghcr.io/sdr-enthusiasts/docker-piaware:latest > /tmp/piaware.log 2>&1 &
                
                PA_PID=$!
                
                # Temporarily disable exit-on-error for monitoring loop
                set +e
                
                # Monitor for ID with spinner
                spin "⏳ Waiting for ID" &
                SPIN_PID=$!
                
                ID_FOUND=false
                MAX_WAIT=90
                for i in $(seq 1 $MAX_WAIT); do
                    # Check if ID appeared in log
                    if [ "$ID_FOUND" = false ]; then
                        if grep -qi "my feeder ID is" /tmp/piaware.log 2>/dev/null; then
                            PIAWARE_FEEDER_ID=$(grep -i "my feeder ID is" /tmp/piaware.log 2>/dev/null | tail -1 | grep -oP 'my feeder ID is \K[a-f0-9-]+' | tr -d '\r\n')
                            if [ -n "$PIAWARE_FEEDER_ID" ] && [ ${#PIAWARE_FEEDER_ID} -gt 10 ]; then
                                ID_FOUND=true
                                break
                            fi
                        fi
                    fi
                    
                    # Check if background process exited
                    if ! kill -0 $PA_PID 2>/dev/null; then
                        # Process died, check one more time for ID
                        sleep 1
                        PIAWARE_FEEDER_ID=$(grep -i "my feeder ID is" /tmp/piaware.log 2>/dev/null | tail -1 | grep -oP 'my feeder ID is \K[a-f0-9-]+' | tr -d '\r\n')
                        if [ -n "$PIAWARE_FEEDER_ID" ] && [ ${#PIAWARE_FEEDER_ID} -gt 10 ]; then
                            ID_FOUND=true
                        fi
                        break
                    fi
                    
                    sleep 1
                done
                
                stop_spin
                
                # Force stop container (don't wait for it)
                docker stop piaware-temp >/dev/null 2>&1 &
                
                # Kill background docker process if still running (don't wait)
                kill $PA_PID 2>/dev/null &
                
                # Give it 2 seconds to stop, then move on
                sleep 2
                
                # Final cleanup (background so we don't hang)
                (docker rm -f piaware-temp >/dev/null 2>&1) &
                
                echo ""
                if [ -n "$PIAWARE_FEEDER_ID" ] && [ ${#PIAWARE_FEEDER_ID} -gt 10 ]; then
                    echo -e "${GREEN}✓ ID: $PIAWARE_FEEDER_ID${NC}"
                    echo -e "${GREEN}✓ PiAware credentials saved${NC}"
                else
                    echo -e "${YELLOW}⚠ Could not auto-extract ID within ${MAX_WAIT}s.${NC}"
                    echo ""
                    echo "The container may still be generating..."
                    echo "Check the log manually:"
                    echo "  cat /tmp/piaware.log | grep 'feeder ID'"
                    echo ""
                    echo "Or check running container:"
                    echo "  docker logs piaware-temp 2>&1 | grep 'feeder ID'"
                    echo ""
                    read -t 15 -p "Enter ID manually (or wait 15s to skip): " MANUAL_ID || true
                    if [ -n "$MANUAL_ID" ]; then
                        PIAWARE_FEEDER_ID="$MANUAL_ID"
                        echo -e "${GREEN}✓ ID saved${NC}"
                    else
                        PIAWARE_FEEDER_ID="YOUR-PIAWARE-FEEDER-ID"
                        echo ""
                        echo -e "${YELLOW}⚠ Skipped. Check docker logs after starting:${NC}"
                        echo "  docker compose logs piaware | grep 'feeder ID'"
                    fi
                fi
                
                cp /tmp/piaware.log /tmp/piaware.log.backup 2>/dev/null || true
                
                fi  # End of if [ -z "$PIAWARE_FEEDER_ID" ]
                
                # Show final ID confirmation (whether from generation or existing container)
                if [ -n "$PIAWARE_FEEDER_ID" ] && [ "$PIAWARE_FEEDER_ID" != "YOUR-PIAWARE-FEEDER-ID" ] && [ ${#PIAWARE_FEEDER_ID} -gt 10 ]; then
                    echo ""
                    echo "IMPORTANT: Claim your feeder at:"
                    echo "  https://flightaware.com/adsb/piaware/claim"
                fi
                
                # Re-enable exit-on-error NOW that we're completely done
                set -e
            fi
            ;;
        2)
            echo ""
            read -p "Enter your PiAware Feeder ID: " PIAWARE_FEEDER_ID
            if [ -z "$PIAWARE_FEEDER_ID" ]; then
                PIAWARE_FEEDER_ID="YOUR-PIAWARE-FEEDER-ID"
                echo -e "${YELLOW}⚠ No ID entered.${NC}"
            else
                echo -e "${GREEN}✓ PiAware ID saved${NC}"
                echo ""
                echo "Remember to claim at: https://flightaware.com/adsb/piaware/claim"
            fi
            ;;
        3|*)
            PIAWARE_FEEDER_ID="YOUR-PIAWARE-FEEDER-ID"
            echo -e "${YELLOW}⚠ Skipping PiAware ID. Check logs after starting:${NC}"
            echo "  docker compose logs piaware | grep 'feeder ID'"
            echo "  Then claim at: https://flightaware.com/adsb/piaware/claim"
            ;;
    esac
fi
echo ""

# Create Config
echo ""
echo -n "Creating configuration... "

cat > .env << EOF
# EasyADSB Configuration
# Generated on $(date)
# https://github.com/datboip/easyadsb

FEEDER_TZ=$FEEDER_TZ
FEEDER_LAT=$FEEDER_LAT
FEEDER_LONG=$FEEDER_LONG
FEEDER_ALT_M=$FEEDER_ALT_M
FEEDER_NAME=$FEEDER_NAME

ADSB_SDR_SERIAL=
ADSB_SDR_PPM=0

MULTIFEEDER_UUID=$MULTIFEEDER_UUID
ADSBX_UUID=$ADSBX_UUID
FR24KEY=$FR24KEY
RADARBOX_KEY=$RADARBOX_KEY
RADARBOX_SERIAL=$RADARBOX_SERIAL
PIAWARE_FEEDER_ID=$PIAWARE_FEEDER_ID

ULTRAFEEDER_CONFIG=adsb,feed.flightradar24.com,30004,beast_reduce_plus_out,uuid=${FR24KEY};adsb,feed.adsbexchange.com,30004,beast_reduce_plus_out,uuid=${ADSBX_UUID};mlat,in.adsb.lol,31090,uuid=${MULTIFEEDER_UUID}
EOF

echo -e "${GREEN}✓${NC}"

# Generate dashboard config
echo -n "Generating dashboard config... "
cat > dashboard-config.js << JSEOF
// Auto-generated from .env - Dashboard will use these for direct links
window.FEEDER_CONFIG = {
    adsbxUUID: "${ADSBX_UUID}",
    adsblolUUID: "${MULTIFEEDER_UUID}",
    fr24Key: "${FR24KEY}",
    radarboxKey: "${RADARBOX_KEY}",
    radarboxSerial: "${RADARBOX_SERIAL}",
    piawareID: "${PIAWARE_FEEDER_ID}"
};
JSEOF
echo -e "${GREEN}✓${NC}"

# Start Services
echo ""
read -p "Start all feeders now? (y/n): " -n 1 -r
echo ""
if [[ $REPLY =~ ^[Yy]$ ]]; then
    # Check for existing containers
    EXISTING_CONTAINERS=$(docker ps -a --filter "name=ultrafeeder\|adsb-dashboard\|radarbox\|piaware\|flightradar24" --format "{{.Names}}" 2>/dev/null)
    
    if [ ! -z "$EXISTING_CONTAINERS" ]; then
        echo ""
        echo -e "${YELLOW}⚠${NC} Found existing EasyADSB containers:"
        echo "$EXISTING_CONTAINERS" | sed 's/^/  - /'
        echo ""
        read -p "Stop and remove these containers? (y/n): " -n 1 -r
        echo ""
        if [[ $REPLY =~ ^[Yy]$ ]]; then
            spin "Cleaning up old containers" &
            SPIN_PID=$!
            docker compose down 2>/dev/null
            stop_spin
            echo -e "${GREEN}✓${NC} Old containers removed"
        else
            echo -e "${YELLOW}Note:${NC} Existing containers may conflict with new setup"
        fi
    fi
    
    spin "Pulling latest images" &
    SPIN_PID=$!
    docker compose pull
    stop_spin
    echo -e "${GREEN}✓${NC} Images pulled"
    echo ""
    spin "Starting services" &
    SPIN_PID=$!
    docker compose up -d
    stop_spin
    echo -e "${GREEN}✓${NC} Services started"
    
    # Verify all services are running
    sleep 3
    echo ""
    echo "Service Status:"
    docker compose ps
    echo ""
    
    sleep 2
    
    # Check for RadarBox serial if not already configured
    if [ -z "$RADARBOX_SERIAL" ] || [ "$RADARBOX_SERIAL" = "" ]; then
        echo ""
        echo -n "Checking for RadarBox serial "
        sleep 3  # Give RadarBox time to start
        RB_SERIAL=$(docker compose logs radarbox 2>/dev/null | grep -i "station serial number:" | tail -1 | grep -oP 'station serial number:\s*\K[A-Z0-9]+' | tr -d '\r\n')
        
        if [ -n "$RB_SERIAL" ]; then
            echo -e "${GREEN}✓${NC}"
            echo "Found RadarBox serial: $RB_SERIAL"
            
            # Update .env file
            if grep -q "^RADARBOX_SERIAL=" .env; then
                sed -i "s/^RADARBOX_SERIAL=.*/RADARBOX_SERIAL=$RB_SERIAL/" .env
            else
                echo "RADARBOX_SERIAL=$RB_SERIAL" >> .env
            fi
            
            # Regenerate dashboard config with new serial (use grep to avoid ULTRAFEEDER_CONFIG execution)
            ADSBX_UUID=$(grep "^ADSBX_UUID=" .env | cut -d'=' -f2)
            MULTIFEEDER_UUID=$(grep "^MULTIFEEDER_UUID=" .env | cut -d'=' -f2)
            FR24KEY=$(grep "^FR24KEY=" .env | cut -d'=' -f2)
            RADARBOX_KEY=$(grep "^RADARBOX_KEY=" .env | cut -d'=' -f2)
            PIAWARE_FEEDER_ID=$(grep "^PIAWARE_FEEDER_ID=" .env | cut -d'=' -f2)
            
            cat > dashboard-config.js << JSEOF
// Auto-generated configuration for EasyADSB Dashboard
// Generated: $(date)
window.FEEDER_CONFIG = {
    adsbxUUID: "${ADSBX_UUID}",
    adsbLolUUID: "${MULTIFEEDER_UUID}",
    fr24Key: "${FR24KEY}",
    radarboxKey: "${RADARBOX_KEY}",
    radarboxSerial: "${RB_SERIAL}",
    piawareID: "${PIAWARE_FEEDER_ID}"
};
JSEOF
            echo "✓ Dashboard config updated with serial"
        else
            echo -e "${YELLOW}⚠${NC}"
            echo "Serial not found yet (this is normal on first run)"
            echo "Check logs later: docker compose logs radarbox | grep serial"
        fi
    fi
    
    MY_IP=$(hostname -I | awk '{print $1}')
    
    echo ""
    echo "════════════════════════════════════════════════════════════════════════════════"
    echo -e "  ${GREEN}✓ SETUP COMPLETE!${NC}"
    echo "════════════════════════════════════════════════════════════════════════════════"
    echo ""
    echo "  Dashboard:  http://$MY_IP:8081/"
    echo "  Live Map:   http://$MY_IP:8080/"
    echo ""
    echo "  Verify your feeds:"
    echo "    • https://www.adsbexchange.com/myip/"
    echo "    • https://adsb.lol/"
    echo "    • https://flightaware.com/adsb/stats/"
    echo ""
    echo "  📊 Check if everything is working:"
    echo "    docker compose ps"
    echo ""
    echo "  🔧 Troubleshooting commands (if needed):"
    echo "    docker compose logs -f          # View live logs"
    echo "    docker compose restart          # Restart all services"
    echo "    docker compose down             # Stop everything"
    echo "    docker compose up -d            # Start everything"
    echo ""
    echo "  💡 Tip: Run ./setup.sh anytime to reconfigure"
    echo ""
    echo "  Happy plane spotting! ✈️"
    echo ""
else
    echo ""
    echo -e "${GREEN}✓ Configuration saved to .env${NC}"
    echo ""
    echo "Start services later with:"
    echo "  docker compose up -d"
    echo ""
fi

# Final cleanup
stop_spin 2>/dev/null || true

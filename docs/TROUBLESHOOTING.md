# Troubleshooting

## Dashboard Not Loading

```bash
docker compose ps dashboard
docker compose logs dashboard
docker compose restart dashboard
sudo netstat -tulpn | grep 8081
```

## No Aircraft Showing

```bash
# Check RTL-SDR
lsusb | grep RTL

# Check ultrafeeder
docker compose logs ultrafeeder | tail -50

# Kill competing processes
sudo killall rtl_*
docker compose restart ultrafeeder
```

## Services Not Connecting

```bash
# Check internet
ping -c 4 8.8.8.8

# Check feeds
docker compose logs ultrafeeder | grep -i "feed"

# Verify config
cat .env | grep -v "^#"
```

## RadarBox Serial Not Showing

```bash
docker compose logs radarbox | grep -i "serial"
# Then run setup.sh -> Restart Services to auto-detect
```

## Dashboard Shows Wrong Location

```bash
nano .env
# Update FEEDER_LAT, FEEDER_LONG, FEEDER_ALT_M
docker compose restart
# Clear browser cache and reload
```

## Common Commands

```bash
# Restart everything
docker compose restart

# Stop/start
docker compose down
docker compose up -d

# View logs
docker compose logs -f
docker compose logs -f ultrafeeder

# Update container images
docker compose pull && docker compose up -d

# Restart one service
docker compose restart radarbox
```

## Setup Script Menu

Run `./setup.sh` anytime:

```
1) Restart services (keep config)
2) Reconfigure everything
3) Stop all services
4) View status & logs
5) Backup / Restore
6) Update EasyADSB (pull from GitHub)
7) Uninstall EasyADSB
8) Exit
```

## Uninstall

```bash
./setup.sh  # Choose option 7
```

Options: containers only, containers + logs, containers + all data, or complete removal.

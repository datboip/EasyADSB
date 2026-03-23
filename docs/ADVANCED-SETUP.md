# Advanced Setup

For users who want manual control or already have service credentials.

## Manual Docker Setup

### 1. Create Your `.env` File

```bash
cp .env.example .env
nano .env
```

Required variables:

```bash
FEEDER_TZ=America/New_York
FEEDER_LAT=40.7128
FEEDER_LONG=-74.0060
FEEDER_ALT_M=10
FEEDER_NAME=MyStation

MULTIFEEDER_UUID=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
ADSBX_UUID=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
FR24KEY=xxxxxxxxxxxxxxxxxx
RADARBOX_KEY=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
RADARBOX_SERIAL=EXTRPIXXXXXX
PIAWARE_FEEDER_ID=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
```

### 2. Generate Service Credentials

**ADSBexchange / ADSB.lol:**
```bash
uuidgen
```

**FlightRadar24:**
```bash
docker run --rm -it --entrypoint /bin/bash \
  ghcr.io/sdr-enthusiasts/docker-flightradar24:latest \
  -c "fr24feed --signup"
```

**RadarBox:**
```bash
docker run --rm -it \
  -e BEASTHOST=127.0.0.1 \
  -e UAT_RECEIVER_HOST=127.0.0.1 \
  ghcr.io/sdr-enthusiasts/docker-radarbox:latest
# Watch logs for key and serial
```

**FlightAware:**
```bash
docker compose up -d piaware
docker compose logs piaware | grep "feeder-id"
```

### 3. Create Dashboard Config

```bash
cat > dashboard-config.js << 'EOF'
window.FEEDER_CONFIG = {
    adsbxUUID: "your-adsbx-uuid",
    adsbLolUUID: "your-adsblol-uuid",
    fr24Key: "your-fr24-key",
    radarboxKey: "your-radarbox-key",
    radarboxSerial: "your-radarbox-serial",
    piawareID: "your-piaware-id"
};
EOF
```

### 4. Start Services

```bash
docker compose up -d
docker compose ps
```

## Using Existing Credentials

If you already have accounts:

1. Run `./setup.sh` and choose "Enter manually" for each service
2. Paste your existing UUIDs/keys
3. Or edit `.env` and `dashboard-config.js` directly, then `docker compose restart`

## Flight Logger

Enable with the `logging` profile:

```bash
docker compose --profile logging up -d
```

### Storage Estimates

| Sample Rate | Per Day | 14 Days | 30 Days |
|-------------|---------|---------|---------|
| 5 sec | ~200MB | 2.8GB | 6GB |
| 10 sec | ~100MB | 1.4GB | 3GB |
| 30 sec | ~35MB | 500MB | 1GB |

### Configuration

Sample rate and retention are configurable from the dashboard or via API:

```bash
curl -X POST http://pi:8082/api/settings \
  -H 'Content-Type: application/json' \
  -d '{"interval": 10, "retention_days": 14}'
```

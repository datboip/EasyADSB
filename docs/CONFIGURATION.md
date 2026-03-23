# Configuration

## Port Mappings

| Port | Service | Description |
|------|---------|-------------|
| 8080 | ultrafeeder | tar1090 local map |
| 8081 | dashboard | EasyADSB dashboard |
| 8082 | logger | Flight logger API |
| 30005 | ultrafeeder | Beast output |
| 30105 | ultrafeeder | MLAT output |

## Containers

| Container | Purpose |
|-----------|---------|
| ultrafeeder | ADS-B decoder, aggregator, tar1090, feeds ADSBx/ADSB.lol/FR24 |
| radarbox | RadarBox feeder |
| piaware | FlightAware feeder |
| flightradar24 | FlightRadar24 feeder |
| dashboard | Nginx serving the web dashboard |
| logger | Flight logging API + SQLite database |

## Data Flow

```
RTL-SDR → ultrafeeder (decode) → ADSBexchange
                                → ADSB.lol + MLAT
                                → FlightRadar24
                                → tar1090 (local map)

         ultrafeeder (Beast) → radarbox
                             → piaware
                             → flightradar24
```

## Changing Location

```bash
nano .env
```

```bash
FEEDER_LAT=40.7128
FEEDER_LONG=-74.0060
FEEDER_ALT_M=10
FEEDER_TZ=America/New_York
```

```bash
docker compose restart
```

## Custom SDR Settings

```bash
# In .env
ADSB_SDR_SERIAL=00001234
ADSB_SDR_PPM=0
```

Find your serial with `rtl_test`.

## Adding Feeds

Add to `ULTRAFEEDER_CONFIG` in `.env`:

```bash
ULTRAFEEDER_CONFIG=adsb,feed.example.com,30004,beast_reduce_plus_out,uuid=YOUR-UUID
```

## Dashboard Config

The dashboard reads `dashboard-config.js`. Edit directly or regenerate via `./setup.sh`.

## Claiming Your Stations

**FlightAware:** https://flightaware.com/adsb/piaware/claim — enter your PiAware ID from the dashboard.

**RadarBox:** https://radarbox.com — My Stations — enter your serial from the dashboard.

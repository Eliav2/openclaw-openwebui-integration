#!/usr/bin/env bash
# The small project the screenshot scenes work on: a weather app with a week of
# history, built fresh at capture time inside the agent's workspace, so the
# tool calls in the screenshots run against real files and a real git log.
#
#   bash scripts/screenshots/sample_project.sh [workspace dir]
set -euo pipefail

ws="${1:-$HOME/.openclaw/workspace}"
dir="$ws/forecast"
rm -rf "$dir"
mkdir -p "$dir/src/forecast"
cd "$dir"
git init -q -b main
git config user.name "Sam"
git config user.email "sam@forecast.invalid"

commit() {  # commit <days ago> <message>
  local when
  when="$(date -u -d "$1 days ago 10:15" +%Y-%m-%dT%H:%M:%S)"
  git add -A
  GIT_AUTHOR_DATE="$when" GIT_COMMITTER_DATE="$when" git commit -q -m "$2"
}

cat > README.md <<'EOF'
# Forecast

A small weather app: current conditions, a seven-day outlook and an hourly
strip, backed by a cached forecast API.

Runs as one container (see Dockerfile) with a 1 GB volume for the cache.
EOF
cat > src/forecast/__init__.py <<'EOF'
"""Forecast: weather for the places you care about."""
EOF
cat > src/forecast/api.py <<'EOF'
def daily(place):
    return {"place": place, "days": []}
EOF
commit 20 "Initial forecast app"

cat > src/forecast/units.py <<'EOF'
def wind(speed_ms, unit):
    return speed_ms * 3.6 if unit == "kmh" else speed_ms * 2.237
EOF
commit 12 "Add unit settings"

cat > src/forecast/hourly.py <<'EOF'
def hourly(place, hours=24):
    return [{"hour": h, "temp": None, "rain": None} for h in range(hours)]
EOF
commit 6 "Add hourly forecast strip to the day screen"

cat > src/forecast/cache.py <<'EOF'
import time

_TTL = 600
_store = {}


def get(key, fetch):
    hit = _store.get(key)
    if hit and time.time() - hit[0] < _TTL:
        return hit[1]
    value = fetch()
    _store[key] = (time.time(), value)
    return value
EOF
commit 4 "Cache forecasts for ten minutes"

sed -i 's/speed_ms \* 2.237/speed_ms * 2.237 if unit == "mph" else speed_ms/' src/forecast/units.py
commit 3 "Fix wind speed ignoring the km/h setting"

cat >> README.md <<'EOF'

## Setup

    pip install -e .
    forecast serve --port 8000
EOF
commit 1 "Docs: setup section"

cat > CHANGELOG.md <<'EOF'
# Changelog

## Unreleased

- Hourly strip on the day screen: temperature and rain chance per hour.
- Forecasts are cached for ten minutes.
- Wind speed now follows the km/h / mph setting.
- README: setup section.

## 0.3.0

- Unit settings (Celsius/Fahrenheit, km/h/mph).
EOF
commit 0 "Changelog for this week"

# The station import the Background scene leaves running (ELI-163): outside
# the repo, so the git scenes do not see it.
mkdir -p "$ws/bin"
cat > "$ws/bin/import-stations" <<'EOF'
#!/bin/sh
# Imports one region's station history (about fifteen minutes).
# --check <region>: whether the region exists, at once (ELI-232's failed row).
if [ "$1" = --check ]; then
  echo "checking $2"
  echo "$2: no such region (north, south, coast, mountain)" >&2
  exit 2
fi
sleep 900
echo "$1 stations imported"
EOF
chmod +x "$ws/bin/import-stations"

# The forecast API's log and its cache volume, for the README tiles (a 502 to
# investigate, a cache to clean up): outside the repo, so the git scenes do not
# see them. Ten days of hourly snapshots nobody pruned; sparse files, so their
# size is apparent only and costs no disk.
mkdir -p "$ws/forecast-cache" "$ws/logs"
for d in 9 8 7 6 5 4 3 2 1 0; do
  snap="$ws/forecast-cache/hourly-$(date -u -d "$d days ago" +%Y-%m-%d).db"
  truncate -s "$((90 + d * 3))M" "$snap"
  touch -d "$((d * 24 + 1)) hours ago" "$snap"
done
truncate -s 14M "$ws/forecast-cache/stations.db"
at() { date -u -d "$1 minutes ago" +"%Y-%m-%d %H:%M:%S"; }
cat > "$ws/logs/forecast-api.log" <<EOF
$(at 14) INFO  GET /forecast/berlin 200 38ms
$(at 13) INFO  refresh hourly snapshot for 412 places
$(at 13) ERROR refresh failed: OSError: [Errno 28] No space left on device: 'forecast-cache/hourly-new.db'
$(at 12) ERROR GET /forecast/lisbon 500 cache write failed
$(at 11) WARN  upstream health check failed, proxy returning 502
$(at 10) ERROR GET /forecast/oslo 500 cache write failed
EOF

echo "sample project ready: $dir ($(git rev-list --count HEAD) commits)"

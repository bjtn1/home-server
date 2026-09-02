#!/bin/bash
LOG_FILE="/var/www/update-log.txt"
KUMA_PUSH_URL="https://kuma.bjtn.xyz/api/push/wWBZZGuQDU"

sudo truncate -s 0 "$LOG_FILE"
echo "=== Update started: $(date '+%Y-%m-%d %H:%M:%S') ===" | sudo tee -a "$LOG_FILE"
sudo apt update 2>&1 | sudo tee -a "$LOG_FILE"
sudo apt upgrade -y 2>&1 | sudo tee -a "$LOG_FILE"
echo "=== Update complete: $(date '+%Y-%m-%d %H:%M:%S') ===" | sudo tee -a "$LOG_FILE"

echo "=== Docker update started: $(date '+%Y-%m-%d %H:%M:%S') ===" | sudo tee -a "$LOG_FILE"
for dir in /home/bjtn/docker/*/; do
    if [ -f "$dir/docker-compose.yml" ]; then
        echo "--- Updating $dir ---" | sudo tee -a "$LOG_FILE"
        docker compose -f "$dir/docker-compose.yml" pull 2>&1 | sudo tee -a "$LOG_FILE"
        docker compose -f "$dir/docker-compose.yml" up -d --remove-orphans 2>&1 | sudo tee -a "$LOG_FILE"
    fi
done
docker image prune -f 2>&1 | sudo tee -a "$LOG_FILE"
echo "=== Docker update complete: $(date '+%Y-%m-%d %H:%M:%S') ===" | sudo tee -a "$LOG_FILE"

echo "{\"last_update\": \"$(date '+%Y-%m-%d %H:%M:%S')\"}" | sudo tee /var/www/update-status.json > /dev/null

curl -fsS -m 10 "$KUMA_PUSH_URL?status=up&msg=OK" >/dev/null 2>&1

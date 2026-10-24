#!/bin/sh
# Шейпим egress контейнера bot-api, чтобы отдача файлов в Telegram не забивала
# аплинк. Значение берём из rate-файла (пишет /speed в боте), иначе из
# UPLOAD_RATE_MBIT. 0 / пусто = без лимита. Требует cap NET_ADMIN (см. compose).
# Живая правка: /speed пишет файл и рестартит этот контейнер → entrypoint
# перечитывает. Отдельно от host-шейпера LXC (с ним не конфликтует).
RATE=""
if [ -r /ratedata/bandwidth_mbit.txt ]; then
  RATE=$(tr -dc '0-9' < /ratedata/bandwidth_mbit.txt)
fi
[ -z "$RATE" ] && RATE="$UPLOAD_RATE_MBIT"

if [ -n "$RATE" ] && [ "$RATE" != "0" ]; then
  if tc qdisc replace dev eth0 root tbf rate "${RATE}mbit" burst 1mbit latency 400ms; then
    echo "Egress shaped to ${RATE} Mbit/s"
  else
    echo "WARNING: failed to apply tc shaping (NET_ADMIN capability missing?)"
  fi
fi
exec /docker-entrypoint.sh "$@"

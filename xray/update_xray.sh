#!/usr/bin/env bash
# Пересобрать конфиг xray из VPN-подписки: сервис меняет серверы и ключи (sid, pbk).
# Запускается раз в сутки таймером xray-update.timer или руками: sudo ./update_xray.sh
#
# Порядок как у безопасного деплоя: собрать → проверить → подменить →
# убедиться, что туннель работает → иначе откатить на прошлый конфиг.
set -euo pipefail

REPO=$(cd "$(dirname "$0")/.." && pwd)
XRAY_DIR=${XRAY_DIR:-$REPO/xray}   # config.json и subscription.url, оба в .gitignore
CONFIG=$XRAY_DIR/config.json
SUB_FILE=$XRAY_DIR/subscription.url
PROXY=http://127.0.0.1:10809
XRAY_UID=65532  # xray в официальном образе работает не от root

compose() { docker compose -f "$REPO/compose.yaml" "$@"; }

tunnel_ok() {
  curl -fsS -x "$PROXY" -m 20 -o /dev/null https://www.gstatic.com/generate_204
}

new=$(mktemp "$XRAY_DIR/config.json.new.XXXX")
trap 'rm -f "$new"' EXIT
chown "$XRAY_UID:$XRAY_UID" "$new"
chmod 600 "$new"

python3 "$REPO/xray/gen_config.py" "$(cat "$SUB_FILE")" > "$new"

if cmp -s "$new" "$CONFIG"; then
  echo "подписка не изменилась"
  exit 0
fi

# xray проверяет конфиг сам, ничего не запуская (-test)
docker run --rm -v "$new:/etc/xray/config.json:ro" ghcr.io/xtls/xray-core:latest \
  run -test -c /etc/xray/config.json > /dev/null

cp -p "$CONFIG" "$CONFIG.bak"
cat "$new" > "$CONFIG"  # cat > сохраняет владельца и права файла
compose restart xray

sleep 15  # дать observatory проверить серверы
if tunnel_ok; then
  echo "конфиг обновлён, туннель работает"
else
  echo "после обновления туннель не работает — откатываю" >&2
  cat "$CONFIG.bak" > "$CONFIG"
  compose restart xray
  exit 1
fi

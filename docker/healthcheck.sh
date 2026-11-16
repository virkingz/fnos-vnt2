#!/bin/sh
# 容器健康检查：探测管理页 /api/health。
#
# 端口优先级：环境变量 PANEL_PORT -> /data/ports.env 里的 PANEL_PORT -> 19099。
# 之所以不直接把端口写进 Dockerfile 的 HEALTHCHECK，是因为 Dockerfile 里的
# ${VAR} 会在「构建期」被替换成固定值，用户改端口后探活就会打到错的端口。
set -u

PORT="${PANEL_PORT:-}"
if [ -z "$PORT" ] && [ -f /data/ports.env ]; then
    PORT="$(sed -n 's/^PANEL_PORT=\([0-9][0-9]*\).*/\1/p' /data/ports.env | head -n 1)"
fi
[ -n "$PORT" ] || PORT=19099

curl -sf --max-time 4 "http://127.0.0.1:${PORT}/api/health" >/dev/null 2>&1

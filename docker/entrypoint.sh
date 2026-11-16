#!/bin/bash
# vnt-nas Docker 入口脚本
#
# 设计原则：不修改任何原有 shell 代码，只在外部复用 lib.sh 的现成能力
# （目录初始化、端口读写、令牌读写、核心下载、进程停止）。
#
# 与 fpk 版的差异：fpk 由飞牛注入 TRIM_* 环境变量并调用 cmd/main；
# 容器里没有飞牛，因此本脚本自行设定这批变量，然后直接拉起 shell_main.py。
#
# 环境变量（全部可选，括号内为默认值）：
#   PANEL_PORT         (19099) 管理页对外端口
#   VNT_WEB_PORT       (19199) vnt2_web 组网面板端口
#   WEB_TOKEN          (随机)  访问令牌，至少 16 位
#   MIRROR             (空)    下载镜像前缀，如 https://ghfast.top
#   PROXY              (空)    网络代理，如 socks5h://127.0.0.1:1080
#   LISTEN_ALL         (1)     1=组网面板监听全部网卡，0=仅回环
#   AUTO_DOWNLOAD_CORE (1)     1=启动时若缺核心则自动下载，0=交给网页手动下载
#   TZ                 (UTC)   时区，如 Asia/Shanghai

set -u

# 应用根目录：容器里固定为 /app（Dockerfile 的 WORKDIR）。
# 允许用 VNT_APPDEST 覆盖，便于在容器外做验证。
APP_DIR="${VNT_APPDEST:-/app}"
DATA_DIR="${VNT_PKGVAR:-/data}"

# 先记下用户显式传入的端口：lib.sh 的 vnt_load_ports 会用配置文件覆盖同名变量，
# 因此要在载入配置之后再回填，保证「环境变量优先于文件」。
_ENV_PANEL_PORT="${PANEL_PORT:-}"
_ENV_VNT_WEB_PORT="${VNT_WEB_PORT:-}"

# ---------------------------------------------------------------------------
# 0. 停止信号处理
#    必须在任何耗时操作（如下载核心）之前注册，否则下载途中被 docker stop
#    会直接 SIGKILL，来不及清理核心进程。
# ---------------------------------------------------------------------------
SHELL_PID_FILE=""
shutting_down=0

on_stop() {
    [ "$shutting_down" = "1" ] && return 0
    shutting_down=1
    trap '' TERM INT
    echo "[vnt] 收到停止信号，正在退出..."

    # 先停后端。注意 shell_main.py 没注册信号处理器，收到 TERM 会直接退出，
    # 不会走它的 stop_core 分支，因此下面必须自己兜底停核心。
    _pid=""
    if [ -n "$SHELL_PID_FILE" ] && [ -f "$SHELL_PID_FILE" ]; then
        _pid="$(head -n 1 "$SHELL_PID_FILE" 2>/dev/null | tr -d '[:space:]')"
        [ -n "$_pid" ] && kill -TERM "$_pid" 2>/dev/null
    fi

    # 等后端真正退出（最多 5 秒），不是等 pid 文件消失
    _i=0
    while [ -n "$_pid" ] && kill -0 "$_pid" 2>/dev/null && [ $_i -lt 5 ]; do
        sleep 1; _i=$((_i + 1))
    done

    # 核心是 start_new_session 起的独立进程组，不会被后端退出带走，必须显式停
    type vnt_stop_core >/dev/null 2>&1 && vnt_stop_core 2>/dev/null

    rm -f "${SHELL_PID_FILE:-/dev/null}" 2>/dev/null
    echo "[vnt] 已停止"
    exit 0
}

trap on_stop TERM INT

# ---------------------------------------------------------------------------
# 1. 还原飞牛原本会注入的环境变量
# ---------------------------------------------------------------------------
export TRIM_APPDEST="$APP_DIR"
export TRIM_PKGVAR="$DATA_DIR"
export TRIM_PKGETC="${DATA_DIR}/etc"
export TRIM_PKGTMP="/tmp"
export TRIM_APPNAME="vnt-nas"
export TRIM_TEMP_LOGFILE="${TRIM_PKGTMP}/error.log"

# shell_server.py / shell_http.py 读的是 VNT_* 这套
export VNT_APPDEST="$TRIM_APPDEST"
export VNT_PKGVAR="$TRIM_PKGVAR"
export VNT_PKGETC="$TRIM_PKGETC"
export VNT_PKGTMP="$TRIM_PKGTMP"
export VNT_APPNAME="$TRIM_APPNAME"

# 管理页 TCP 的绑定地址：容器内必须绑 0.0.0.0，否则端口映射到宿主机后
# 从容器外访问不到（容器内的 127.0.0.1 只属于容器自身）。
# 默认值（127.0.0.1）是给飞牛部署用的——那边走 Unix socket，无需对外。
export PANEL_BIND="${PANEL_BIND:-0.0.0.0}"

# ---------------------------------------------------------------------------
# 2. 载入原有函数库
# ---------------------------------------------------------------------------
# shellcheck disable=SC1091
. "${APP_DIR}/shell/lib.sh"

vnt_paths
vnt_ensure_dirs
vnt_load_ports

# 环境变量优先于 ports.env：显式传了就用传入值，否则沿用文件里的既有配置
[ -n "${_ENV_PANEL_PORT}" ] && PANEL_PORT="${_ENV_PANEL_PORT}"
[ -n "${_ENV_VNT_WEB_PORT}" ] && VNT_WEB_PORT="${_ENV_VNT_WEB_PORT}"

# 两端口不能相同，冲突时让组网面板端口回退默认值
if [ "${PANEL_PORT}" = "${VNT_WEB_PORT}" ]; then
    echo "[vnt] 警告：两个端口相同，组网面板端口回退为 ${VNT_WEB_PORT_DEFAULT}"
    VNT_WEB_PORT="${VNT_WEB_PORT_DEFAULT}"
fi

echo "[vnt] 数据目录: ${DATA_DIR}"
echo "[vnt] 管理页端口: ${PANEL_PORT}  |  组网面板端口: ${VNT_WEB_PORT}"

vnt_save_ports

# ---------------------------------------------------------------------------
# 3. 各项设置写入（显式设定了才写，未设定则沿用文件，便于网页内改完持久化）
# ---------------------------------------------------------------------------
# 访问令牌：给了就用给定的；没给则确保存在（vnt2_web 要求 >= 16 位）
if [ -n "${WEB_TOKEN:-}" ]; then
    vnt_save_token "${WEB_TOKEN}"
fi
[ -n "$(vnt_get_token)" ] || vnt_gen_token
echo "[vnt] 访问令牌: $(vnt_token_hint)"

# 下载镜像前缀
if [ -n "${MIRROR:-}" ]; then
    vnt_save_mirror "${MIRROR}"
    echo "[vnt] 镜像前缀: ${MIRROR}"
fi

# 网络代理
if [ -n "${PROXY:-}" ]; then
    vnt_save_proxy "${PROXY}"
    echo "[vnt] 网络代理: $(vnt_get_proxy)"
fi

# 组网面板监听范围
# 只在显式设定时写入，否则保留用户在「访问设置」里选过的值（默认全部网卡）
if [ -n "${LISTEN_ALL:-}" ]; then
    if [ "${LISTEN_ALL}" = "0" ]; then
        printf '127.0.0.1' > "${TRIM_PKGVAR}/bind"
        echo "[vnt] 组网面板监听: 仅回环"
    else
        printf '0.0.0.0' > "${TRIM_PKGVAR}/bind"
        echo "[vnt] 组网面板监听: 全部网卡"
    fi
fi

# ---------------------------------------------------------------------------
# 4. 可选：启动时自动下载核心
#    核心不进镜像（约 8 MB，且 vnt 发版频繁），首次启动按架构拉取。
# ---------------------------------------------------------------------------
CORE_BIN="$(vnt_core_bin)"
if [ ! -f "$CORE_BIN" ] && [ "${AUTO_DOWNLOAD_CORE:-1}" != "0" ]; then
    echo "[vnt] 未发现核心，尝试自动下载（架构: $(uname -m)）..."
    VER="$(vnt_latest_version 2>/dev/null || true)"
    if [ -n "$VER" ]; then
        echo "[vnt] 最新版本: v${VER}"
        if vnt_install_core "$VER"; then
            echo "[vnt] 核心安装完成: v${VER}"
        else
            echo "[vnt] 核心自动下载失败（退出码 $?），可稍后在管理页手动下载或上传"
        fi
    else
        echo "[vnt] 无法获取最新版本号（网络受限？），可在管理页「访问设置」填镜像前缀后重试"
    fi
fi

if [ -f "$CORE_BIN" ]; then
    echo "[vnt] 当前核心版本: $(vnt_installed_version)"
else
    echo "[vnt] 核心尚未安装，请打开管理页下载"
fi

# ---------------------------------------------------------------------------
# 5. 拉起 shell 后端
# ---------------------------------------------------------------------------
PYTHON_BIN="$(vnt_shell_python)"
SHELL_PY="${APP_DIR}/shell/shell_main.py"
SHELL_PID_FILE="$(vnt_shell_pid)"
SHELL_LOG="$(vnt_shell_log)"

if [ ! -f "$SHELL_PY" ]; then
    echo "[vnt] 缺少后端入口: $SHELL_PY" >&2
    exit 1
fi

echo "[vnt] 启动管理页后端: ${PYTHON_BIN} ${SHELL_PY}"
cd "${APP_DIR}/shell" || exit 1

"$PYTHON_BIN" "$SHELL_PY" >> "$SHELL_LOG" 2>&1 &
child=$!
printf '%s' "$child" > "$SHELL_PID_FILE"

# 等待端口就绪（最多 30 秒），让容器日志直接反映成败
i=0
while [ $i -lt 30 ]; do
    if ! kill -0 "$child" 2>/dev/null; then
        echo "[vnt] 后端进程已退出，日志尾部如下："
        tail -n 40 "$SHELL_LOG" 2>/dev/null
        rm -f "$SHELL_PID_FILE"
        exit 1
    fi
    if command -v curl >/dev/null 2>&1 \
       && curl -sf --max-time 2 "http://127.0.0.1:${PANEL_PORT}/api/health" >/dev/null 2>&1; then
        echo "[vnt] 管理页就绪: http://<主机>:${PANEL_PORT}"
        echo "[vnt] 组网面板:  http://<主机>:${VNT_WEB_PORT}"
        break
    fi
    sleep 1; i=$((i + 1))
done

# 前台等待：子进程退出即容器退出
wait "$child"
code=$?
rm -f "$SHELL_PID_FILE"
echo "[vnt] 后端已退出（code=${code}）"
vnt_stop_core 2>/dev/null

exit "$code"

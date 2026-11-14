#!/bin/bash
# vnt-nas 公共函数库：路径、日志、架构映射、下载、校验、核心版本管理。
# 由 cmd/* 生命周期脚本和管理页后端 source。

set -u

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
VNT_REPO="vnt-dev/vnt"
VNT_GITHUB_API="https://api.github.com/repos/${VNT_REPO}/releases/latest"
VNT_GITHUB_DL="https://github.com/${VNT_REPO}/releases/download"
# 端口模型：
#   PANEL_PORT   —— 用户配置的端口，提供管理页
#   VNT_WEB_PORT —— vnt2_web 自己的端口，直接对外提供组网面板
#                   （桌面第二个图标即指向此端口）
PANEL_PORT_DEFAULT=19099
VNT_WEB_PORT_DEFAULT=19199   # vnt2_web 端口（组网面板）
SHELL_PORT_DEFAULT=19988     # 兼容旧配置：管理页内部回环监听

# ---------------------------------------------------------------------------
# 路径（全部基于 TRIM_* 环境变量，禁止硬编码）
# ---------------------------------------------------------------------------
vnt_paths() {
    : "${TRIM_APPDEST:=/var/apps/vnt-nas/target}"
    : "${TRIM_PKGVAR:=/var/apps/vnt-nas/var}"
    : "${TRIM_PKGETC:=/var/apps/vnt-nas/etc}"
    : "${TRIM_PKGTMP:=/var/apps/vnt-nas/tmp}"
    : "${TRIM_TEMP_LOGFILE:=${TRIM_PKGTMP}/error.log}"
    : "${TRIM_APPNAME:=vnt-nas}"
}

vnt_var_dir()    { echo "${TRIM_PKGVAR}"; }
vnt_bin_dir()    { echo "${TRIM_PKGVAR}/bin"; }          # 核心二进制（升级不覆盖）
vnt_conf_dir()   { echo "${TRIM_PKGVAR}/vnt_config"; }   # vnt2_web 工作目录与 TOML
vnt_log_dir()    { echo "${TRIM_PKGVAR}/log"; }
vnt_cache_dir()  { echo "${TRIM_PKGVAR}/cache"; }        # 更新缓存与备份
vnt_pid_file()   { echo "${TRIM_PKGVAR}/core.pid"; }
vnt_shell_pid()  { echo "${TRIM_PKGVAR}/shell.pid"; }
vnt_port_file()  { echo "${TRIM_PKGVAR}/ports.env"; }    # 端口等运行期参数

vnt_main_log()   { echo "$(vnt_log_dir)/main.log"; }
vnt_update_log() { echo "$(vnt_log_dir)/update.log"; }
vnt_shell_log()  { echo "$(vnt_log_dir)/shell.log"; }

vnt_core_bin()   { echo "$(vnt_bin_dir)/vnt2_web"; }
vnt_core_ver_file() { echo "$(vnt_bin_dir)/.vnt_version"; }

# ---------------------------------------------------------------------------
# 日志
# ---------------------------------------------------------------------------
vnt_log() {
    local f
    f="$(vnt_main_log)"
    mkdir -p "$(dirname "$f")" 2>/dev/null
    vnt_trim_log "$f"
    echo "$(date '+%Y-%m-%d %H:%M:%S') [$$] $*" >> "$f"
}

vnt_ulog() {
    local f
    f="$(vnt_update_log)"
    mkdir -p "$(dirname "$f")" 2>/dev/null
    vnt_trim_log "$f"
    echo "$(date '+%Y-%m-%d %H:%M:%S') [$$] $*" >> "$f"
}

# 日志上限：超过 VNT_LOG_MAX_BYTES 时原地截断，保留最近 VNT_LOG_KEEP_BYTES。
# 用「原地截断」而非「改名轮转」：shell.log 由后端进程以 >> 持有 fd 追加，
# 改名会让它继续写旧 inode，轮转失效；而 O_APPEND 下原地截断是安全的。
VNT_LOG_MAX_BYTES="${VNT_LOG_MAX_BYTES:-524288}"    # 512 KB
VNT_LOG_KEEP_BYTES="${VNT_LOG_KEEP_BYTES:-262144}"  # 256 KB

vnt_trim_log() {
    local f="$1" size tmp
    [ -f "$f" ] || return 0
    size="$(wc -c < "$f" 2>/dev/null | tr -d ' ')" || return 0
    [ -n "$size" ] || return 0
    [ "$size" -gt "$VNT_LOG_MAX_BYTES" ] 2>/dev/null || return 0
    tmp="${f}.trim"
    # 保留尾部 N 字节，再丢掉首行（可能是被截断的半行）
    { printf '[log trimmed: 仅保留最近 %d KB]\n' "$((VNT_LOG_KEEP_BYTES / 1024))"
      tail -c "$VNT_LOG_KEEP_BYTES" "$f" 2>/dev/null | tail -n +2; } > "$tmp" 2>/dev/null || {
        rm -f "$tmp"; return 0; }
    # 原地回写（保持 inode）：O_APPEND 的写入方不会因此错位
    cat "$tmp" > "$f" 2>/dev/null
    rm -f "$tmp"
}

# 失败：写用户可见错误并退出
vnt_fail() {
    vnt_log "ERROR: $*"
    echo "$*" > "${TRIM_TEMP_LOGFILE}" 2>/dev/null || true
    exit 1
}

# ---------------------------------------------------------------------------
# 初始化目录
# ---------------------------------------------------------------------------
vnt_ensure_dirs() {
    mkdir -p "$(vnt_bin_dir)" "$(vnt_conf_dir)" "$(vnt_log_dir)" "$(vnt_cache_dir)"
    chmod 700 "$(vnt_var_dir)" 2>/dev/null || true
}

# ---------------------------------------------------------------------------
# 架构映射：fnOS 架构 -> vnt release asset 目标三元组
# ---------------------------------------------------------------------------
vnt_asset_name() {
    local arch="${1:-$(uname -m)}"
    local triple
    case "$arch" in
        x86_64|amd64|x64)          triple="x86_64-unknown-linux-musl" ;;
        aarch64|arm64)             triple="aarch64-unknown-linux-musl" ;;
        armv7l|armv7|armhf)        triple="armv7-unknown-linux-musleabihf" ;;
        armv6l|arm)                triple="arm-unknown-linux-musleabihf" ;;
        i386|i686)                 triple="" ;;
        *)                         triple="" ;;
    esac
    [ -n "$triple" ] || return 1
    echo "vnt2-${triple}"
    return 0
}

# ---------------------------------------------------------------------------
# 端口
# ---------------------------------------------------------------------------
vnt_load_ports() {
    if [ -f "$(vnt_port_file)" ]; then
        # shellcheck disable=SC1090
        . "$(vnt_port_file)"
    fi
    # 兼容早期版本：旧配置里的 SHELL_PORT 即现在的 PANEL_PORT
    if [ -z "${PANEL_PORT:-}" ] && [ -n "${SHELL_PORT:-}" ]; then
        PANEL_PORT="${SHELL_PORT}"
    fi
    : "${PANEL_PORT:=${PANEL_PORT_DEFAULT}}"
    : "${VNT_WEB_PORT:=${VNT_WEB_PORT_DEFAULT}}"
}

vnt_save_ports() {
    local f
    f="$(vnt_port_file)"
    {
        echo "PANEL_PORT=${PANEL_PORT:-$PANEL_PORT_DEFAULT}"
        echo "VNT_WEB_PORT=${VNT_WEB_PORT:-$VNT_WEB_PORT_DEFAULT}"
    } > "$f"
    chmod 600 "$f"
}

# ---------------------------------------------------------------------------
# 镜像前缀：支持 ghproxy 之类的加速前缀，留空即直连 GitHub
# ---------------------------------------------------------------------------
vnt_mirror_prefix() {
    local f="$(vnt_conf_dir)/mirror"
    if [ -f "$f" ]; then
        head -n 1 "$f" | tr -d '\r\n' | sed 's:/*$::'
    fi
    return 0
}

vnt_url() {
    local prefix
    prefix="$(vnt_mirror_prefix)"
    if [ -n "$prefix" ]; then
        echo "${prefix}/$1"
    else
        echo "$1"
    fi
}

# 查询最新版本号（不带 v 前缀）。优先 GitHub API，失败则回退到 releases/latest 重定向。
vnt_latest_version() {
    local tmp body code
    tmp="$(mktemp)"
    code="$(vnt_curl -sSL --max-time 20 -o "$tmp" -w '%{http_code}' \
        -H 'Accept: application/vnd.github+json' \
        -H 'User-Agent: vnt-nas-fpk' \
        "$(vnt_url "$VNT_GITHUB_API")" 2>/dev/null)" || code=000
    if [ "$code" = "200" ] && [ -s "$tmp" ]; then
        body="$(cat "$tmp")"
        local tag
        tag="$(printf '%s' "$body" | sed -n 's/.*"tag_name"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -n 1)"
        rm -f "$tmp"
        if [ -n "$tag" ]; then
            echo "${tag#v}"
            return 0
        fi
    fi
    rm -f "$tmp"
    # 回退：解析 releases/latest 的重定向地址
    local eff
    eff="$(vnt_curl -sSI --max-time 20 -o /dev/null -w '%{url_effective}' \
        "$(vnt_url "https://github.com/${VNT_REPO}/releases/latest")" 2>/dev/null)" || return 1
    local tag2="${eff##*/}"
    [ -n "$tag2" ] && [ "$tag2" != "latest" ] && echo "${tag2#v}" && return 0
    return 1
}

# 当前已装核心版本
vnt_installed_version() {
    local f
    f="$(vnt_core_ver_file)"
    if [ -f "$f" ]; then
        head -n 1 "$f" | tr -d '\r\n'
    else
        echo ""
    fi
}

# ---------------------------------------------------------------------------
# 网络代理：与镜像前缀是两种机制，可同时使用
#   镜像前缀 —— 改写请求 URL（URL 层）
#   代理     —— 请求经网络隧道（网络层），支持 socks5 / http
# ---------------------------------------------------------------------------
vnt_proxy_file() { echo "$(vnt_conf_dir)/proxy"; }

vnt_get_proxy() {
    local f
    f="$(vnt_proxy_file)"
    if [ -f "$f" ]; then
        head -n 1 "$f" | tr -d '\r\n' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//'
    fi
    return 0
}

vnt_save_proxy() {
    local p
    p="$(printf '%s' "$1" | tr -d '\r\n' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
    # 省略协议时按 http 处理
    if [ -n "$p" ]; then
        case "$p" in
        *://*) ;;
        *) p="http://${p}" ;;
        esac
    fi
    printf '%s' "$p" > "$(vnt_proxy_file)"
    chmod 600 "$(vnt_proxy_file)" 2>/dev/null || true
    return 0
}

# 输出 curl 的代理参数（未配置时为空），供各下载点复用
vnt_curl_proxy_args() {
    local p
    p="$(vnt_get_proxy)"
    if [ -n "$p" ]; then
        printf '%s' "--proxy ${p}"
    fi
    return 0
}

# 统一的 curl 调用：自动附加代理参数
vnt_curl() {
    local proxy_args
    proxy_args="$(vnt_curl_proxy_args)"
    if [ -n "$proxy_args" ]; then
        # shellcheck disable=SC2086
        curl $proxy_args "$@"
    else
        curl "$@"
    fi
    return $?
}

# ---------------------------------------------------------------------------
# 下载并解包核心到 bin 目录（原子替换 + 备份）
# 用法：vnt_install_core <version> [asset_triple]
# ---------------------------------------------------------------------------
vnt_install_core() {
    local ver="$1"
    local asset stem zipfile work
    stem="$(vnt_asset_name)" || return 2
    asset="${stem}-v${ver}.zip"
    zipfile="$(vnt_cache_dir)/${asset}.download"
    work="$(vnt_cache_dir)/unpack"
    local url
    url="$(vnt_url "${VNT_GITHUB_DL}/v${ver}/${asset}")"

    vnt_ulog "install core ${ver} asset=${asset}"
    mkdir -p "$(vnt_cache_dir)" "$(vnt_bin_dir)"
    rm -rf "$work"; mkdir -p "$work"

    vnt_ulog "download ${url}"
    if ! vnt_curl -sSL --max-time 600 --retry 3 --retry-delay 2 -o "$zipfile" "$url"; then
        vnt_ulog "download failed"
        return 3
    fi
    if [ ! -s "$zipfile" ]; then
        vnt_ulog "downloaded file empty"
        return 3
    fi

    # 解包（unzip 优先，其次 python3）
    if command -v unzip >/dev/null 2>&1; then
        unzip -oq "$zipfile" -d "$work" || { vnt_ulog "unzip failed"; return 4; }
    elif command -v python3 >/dev/null 2>&1; then
        python3 -c "import zipfile,sys;zipfile.ZipFile(sys.argv[1]).extractall(sys.argv[2])" \
            "$zipfile" "$work" || { vnt_ulog "py unzip failed"; return 4; }
    else
        vnt_ulog "no unzip tool"
        return 4
    fi

    local newbin
    newbin="$(find "$work" -type f -name 'vnt2_web' | head -n 1)"
    if [ -z "$newbin" ] || [ ! -s "$newbin" ]; then
        vnt_ulog "vnt2_web not found in archive"
        return 5
    fi
    chmod 0755 "$newbin"

    # 备份旧二进制，原子替换
    local cur
    cur="$(vnt_core_bin)"
    if [ -f "$cur" ]; then
        cp -f "$cur" "$(vnt_cache_dir)/vnt2_web.bak" 2>/dev/null || true
    fi
    if ! cp -f "$newbin" "${cur}.new" 2>/dev/null; then
        vnt_ulog "copy new binary failed"
        return 6
    fi
    chmod 0755 "${cur}.new"
    mv -f "${cur}.new" "$cur" || { vnt_ulog "atomic replace failed"; return 6; }

    echo "$ver" > "$(vnt_core_ver_file)"
    # 记录来源，便于排查
    {
        echo "version=$ver"
        echo "asset=$asset"
        echo "url=$url"
        echo "installed_at=$(date '+%Y-%m-%d %H:%M:%S')"
    } > "$(vnt_bin_dir)/.vnt_source"

    rm -f "$zipfile"
    rm -rf "$work"
    vnt_ulog "core ${ver} installed ok"
    return 0
}

# 回滚到备份
vnt_rollback_core() {
    local bak="$(vnt_cache_dir)/vnt2_web.bak"
    [ -f "$bak" ] || return 1
    cp -f "$bak" "$(vnt_core_bin)" && chmod 0755 "$(vnt_core_bin)"
    vnt_ulog "rolled back to previous core"
    return 0
}

# ---------------------------------------------------------------------------
# 进程管理
# ---------------------------------------------------------------------------
vnt_core_running() {
    local pf="$(vnt_pid_file)"
    [ -f "$pf" ] || return 1
    local pid
    pid="$(head -n 1 "$pf" | tr -d '[:space:]')"
    [ -n "$pid" ] || return 1
    kill -0 "$pid" 2>/dev/null
}

vnt_shell_running() {
    local pf="$(vnt_shell_pid)"
    [ -f "$pf" ] || return 1
    local pid
    pid="$(head -n 1 "$pf" | tr -d '[:space:]')"
    [ -n "$pid" ] || return 1
    kill -0 "$pid" 2>/dev/null
}

# ---------------------------------------------------------------------------
# Python 运行时探测：优先 fnOS 的 python312 应用，其次系统 python3
# ---------------------------------------------------------------------------
vnt_shell_python() {
    local c
    for c in \
        /var/apps/python312/target/bin/python3 \
        /var/apps/python312/target/bin/python \
        /usr/local/bin/python3 \
        /usr/bin/python3 \
        python3 python
    do
        if command -v "$c" >/dev/null 2>&1; then
            echo "$c"
            return 0
        fi
    done
    echo "python3"
    return 0
}

# 管理页端口是否已监听（有服务在响应）
vnt_shell_port_up() {
    vnt_load_ports
    local port="${PANEL_PORT:-$PANEL_PORT_DEFAULT}"
    if command -v curl >/dev/null 2>&1; then
        curl -sf --max-time 2 "http://127.0.0.1:${port}/api/health" >/dev/null 2>&1 && return 0
    fi
    vnt_port_in_use "$port"
}

# 端口是否被任何进程监听（不要求是本应用的）
vnt_port_in_use() {
    local port="${1:-}"
    [ -n "$port" ] || return 1
    if command -v ss >/dev/null 2>&1; then
        ss -tln 2>/dev/null | grep -qE ":${port}[[:space:]]" && return 0
    fi
    if command -v netstat >/dev/null 2>&1; then
        netstat -an 2>/dev/null | grep -E "LISTEN" | grep -qE "[:.]${port}[[:space:]]" && return 0
    fi
    return 1
}

# ---------------------------------------------------------------------------
# 访问令牌：vnt2_web 的 Web/API 令牌，至少 16 位
# ---------------------------------------------------------------------------
vnt_token_file() { echo "$(vnt_bin_dir)/webtoken"; }

# 读取令牌；不存在则返回空
vnt_get_token() {
    local f
    f="$(vnt_token_file)"
    if [ -f "$f" ]; then
        head -n 1 "$f" | tr -d '\r\n'
    fi
    return 0
}

# 生成随机令牌（16-32 位）。
# 注意：默认直接写入文件、不向 stdout 输出，避免令牌泄漏到安装日志。
# 需要取值时传 -p。
vnt_gen_token() {
    local echo_it=0
    [ "${1:-}" = "-p" ] && echo_it=1
    local t=""
    if command -v openssl >/dev/null 2>&1; then
        t="$(openssl rand -hex 16 2>/dev/null)"
    fi
    if [ -z "$t" ] && [ -r /dev/urandom ]; then
        t="$(head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n')"
    fi
    if [ -z "$t" ]; then
        t="vnt$(date +%s)${RANDOM}${RANDOM}"
    fi
    t="$(printf '%s' "$t" | cut -c1-32)"
    _vnt_write_token "$t"
    if [ "$echo_it" = "1" ]; then
        printf '%s' "$t"
    fi
    return 0
}

# 保存令牌：长度不足 16 时自动补齐（vnt2_web 要求）
vnt_save_token() {
    local t
    t="$(printf '%s' "$1" | tr -d '\r\n' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
    if [ ${#t} -lt 16 ]; then
        # 用时间戳与随机数补齐，不调用 vnt_gen_token 以免递归
        local pad
        pad="$(date +%s%N 2>/dev/null || date +%s)${RANDOM}${RANDOM}"
        while [ ${#t} -lt 16 ]; do
            t="${t}${pad}"
        done
        t="$(printf '%s' "$t" | cut -c1-32)"
    fi
    _vnt_write_token "$t"
    return 0
}

# 内部：仅写入文件
_vnt_write_token() {
    local f
    f="$(vnt_token_file)"
    printf '%s' "$1" > "$f"
    chmod 600 "$f" 2>/dev/null || true
    return 0
}

# 用于日志的脱敏提示
vnt_token_hint() {
    local t
    t="$(vnt_get_token)"
    if [ -z "$t" ]; then
        echo "未设置"
    elif [ ${#t} -le 8 ]; then
        echo "已设置"
    else
        echo "已设置(${#t}位)"
    fi
    return 0
}

# ---------------------------------------------------------------------------
# 镜像前缀
# ---------------------------------------------------------------------------
vnt_save_mirror() {
    local m
    m="$(printf '%s' "$1" | tr -d '\r\n' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//;s:/*$::')"
    printf '%s' "$m" > "$(vnt_conf_dir)/mirror"
    return 0
}

# ---------------------------------------------------------------------------
# 生成 vnt2_web 的 TOML 配置
# 输入 ack 顺序：network_code server password device_mode ip auto_sync_subnet
# ---------------------------------------------------------------------------
vnt_render_config() {
    local network_code="${1:-}"
    local server="${2:-}"
    local password="${3:-}"
    local device_mode="${4:-tun}"
    local vip="${5:-}"
    local auto_sync="${6:-false}"

    local cfg="$(vnt_conf_dir)/vnt_config.toml"
    local name="${device_name:-vnt-nas}"

    {
        echo "# 由 vnt-nas 管理页生成，请勿手工编辑（会被覆盖）"
        echo "config_name = \"vnt-nas\""
        [ -n "$network_code" ] && echo "network_code = \"${network_code}\""
        if [ -n "$server" ]; then
            echo "server = [\"${server}\"]"
        fi
        [ -n "$password" ] && echo "password = \"${password}\""
        echo "device_mode = \"${device_mode}\""
        echo "device_name = \"${name}\""
        [ -n "$vip" ] && echo "ip = \"${vip}\""
        echo "auto_sync_subnet = ${auto_sync}"
        echo "no_punch = false"
        echo "no_broadcast = false"
        echo "compress = true"
        echo "fec = false"
    } > "$cfg"
    chmod 600 "$cfg"
    echo "$cfg"
    return 0
}

# 按端口清理占用进程（兜底手段，避免「说停了但端口还开着」）
vnt_kill_by_port() {
    local port="$1"
    [ -n "$port" ] || return 0

    if command -v fuser >/dev/null 2>&1; then
        fuser -k -TERM "${port}/tcp" >/dev/null 2>&1 || true
        sleep 1
        fuser -k -KILL "${port}/tcp" >/dev/null 2>&1 || true
        return 0
    fi

    if command -v lsof >/dev/null 2>&1; then
        local pids
        pids="$(lsof -t -i:"${port}" 2>/dev/null || true)"
        if [ -n "$pids" ]; then
            # shellcheck disable=SC2086
            kill -TERM $pids 2>/dev/null || true
            sleep 1
            # shellcheck disable=SC2086
            kill -KILL $pids 2>/dev/null || true
        fi
        return 0
    fi

    # Windows / 无 fuser-lsof 时的兜底
    if command -v netstat >/dev/null 2>&1 && command -v taskkill >/dev/null 2>&1; then
        local pids
        pids="$(netstat -ano 2>/dev/null \
            | grep -E "LISTENING" \
            | grep -E "[:.]${port}[[:space:]]" \
            | awk '{print $NF}' | sort -u || true)"
        local p
        for p in $pids; do
            [ -n "$p" ] && [ "$p" != "0" ] && taskkill //F //PID "$p" >/dev/null 2>&1 || true
        done
    fi
    return 0
}

# 停核心
vnt_stop_core() {
    local pf="$(vnt_pid_file)"
    vnt_core_running || { rm -f "$pf"; return 0; }
    local pid
    pid="$(head -n 1 "$pf" | tr -d '[:space:]')"
    vnt_log "stopping core pid=${pid}"
    kill -TERM "$pid" 2>/dev/null || true
    local i=0
    while vnt_core_running && [ $i -lt 15 ]; do
        sleep 1; i=$((i + 1))
    done
    if vnt_core_running; then
        kill -KILL "$pid" 2>/dev/null || true
        sleep 1
    fi
    rm -f "$pf"
    return 0
}

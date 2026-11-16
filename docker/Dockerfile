# vnt-nas —— VNT 组网（vnt2_web 壳）的 Docker 镜像
#
# 与 fpk 版共用同一套 app/shell 代码，只是把「飞牛注入 TRIM_* 环境变量」
# 换成容器内自行设定，因此不需要修改任何原有源码。
#
# 构建（在仓库根目录执行）：
#   docker build -f docker/Dockerfile -t vnt-nas:latest .
#
# 多架构：vnt2_web 核心是运行时按 uname -m 下载的静态 musl 二进制，
# 壳本身是纯 Python，因此同一个 Dockerfile 可直接构建 amd64 / arm64 / armv7。
FROM alpine:3.20

LABEL org.opencontainers.image.title="vnt-nas" \
      org.opencontainers.image.description="VNT 组网（vnt2_web 壳）：令牌托管、核心在线更新、组网面板反代" \
      org.opencontainers.image.source="https://github.com/virkingz/fnos-vnt2" \
      org.opencontainers.image.licenses="MIT"

# bash  : 原有 lib.sh / 入口脚本依赖 bash 语法
# curl  : 下载核心（且 SOCKS 代理只能靠 curl）
# unzip : 解包 vnt2_web 官方 zip
# openssl: 生成访问令牌
# tini  : 作为 PID 1 回收僵尸进程并正确转发信号
RUN apk add --no-cache \
        python3 \
        bash \
        curl \
        unzip \
        openssl \
        ca-certificates \
        tzdata \
        tini \
    && ln -sf /usr/bin/python3 /usr/local/bin/python3

# 与飞牛上一致的应用根目录（TRIM_APPDEST 对应值）
WORKDIR /app

# 应用本体：shell（后端 + 管理页）与 ui（图标等）
COPY app/shell /app/shell
COPY app/ui    /app/ui

# 入口脚本与健康检查脚本
COPY docker/entrypoint.sh    /app/docker/entrypoint.sh
COPY docker/healthcheck.sh   /app/docker/healthcheck.sh

# 统一换行符并给脚本加执行位（Windows 上编辑常带 CRLF）
RUN sed -i 's/\r$//' /app/docker/entrypoint.sh /app/docker/healthcheck.sh /app/shell/lib.sh \
    && chmod 755 /app/docker/entrypoint.sh /app/docker/healthcheck.sh /app/shell/lib.sh

# 清掉可能随源码带进来的字节码缓存（找不到目录时返回非零，不视为失败）
RUN find /app/shell -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null; true

# 运行期数据（核心二进制、令牌、组网配置、日志）都落在 /data，便于卷持久化
#
# 注意：这里故意不设 PANEL_PORT / VNT_WEB_PORT / LISTEN_ALL / MIRROR / PROXY。
# 一旦写进 ENV，变量就「永远存在」，入口脚本会认为用户每次都在显式指定，
# 从而把在管理页里改过的设置（端口、监听范围、镜像、代理）在重启时打回默认值。
# 这些项的默认值都由 entrypoint.sh 内的函数提供，只有用户真正传入才生效。
ENV VNT_PKGVAR=/data \
    AUTO_DOWNLOAD_CORE=1 \
    TZ=Asia/Shanghai \
    PYTHONUNBUFFERED=1

RUN mkdir -p /data

VOLUME ["/data"]

# 19099 管理页 / 19199 组网面板（皆可由环境变量改）
EXPOSE 19099 19199

# 健康检查走独立脚本：运行时读 ports.env，改端口后依然探得准
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD /app/docker/healthcheck.sh

ENTRYPOINT ["/sbin/tini", "--", "/app/docker/entrypoint.sh"]

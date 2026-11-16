# Docker 部署

把本项目（飞牛 fnOS 的 `vnt2_web` 封装壳）跑在 Docker 里，**不改动任何原有源码**。

镜像里装的是同一套 `app/shell` 代码，只是把「飞牛注入 `TRIM_*` 环境变量并调用 `cmd/main`」
换成容器内自行设定这批变量、直接拉起 `shell_main.py`。因此：

- 管理页、核心在线更新、令牌自动登录、SOCKS5/HTTP 代理、日志截断等功能**完全一致**
- 组网面板同样经 `/core/` 反代并注入令牌，打开即用
- 上游代码更新后，直接同步 `app/shell` 即可，Docker 部分无需改动

---

## 快速开始

### 方式一：用现成镜像（无需自己构建）

CI 已把多架构镜像推到 GHCR，直接拉即可：

```bash
docker pull ghcr.io/virkingz/fnos-vnt2:latest
```

`docker-compose.yml` 里把 `build:` 一段注释掉，改用 `image:`：

```yaml
services:
  vnt-nas:
    image: ghcr.io/virkingz/fnos-vnt2:latest
    # build:
    #   context: ..
    #   dockerfile: docker/Dockerfile
```

镜像同时支持 `linux/amd64`、`linux/arm64`、`linux/arm/v7`，
`docker pull` 会按当前机器架构自动选对，不用手动指定。

### 方式二：本地构建

```bash
# compose（推荐）
cd docker
docker compose up -d

# 或 docker run
docker build -f docker/Dockerfile -t vnt-nas:latest .
docker run -d --name vnt-nas \
  --restart unless-stopped \
  --cap-add=NET_ADMIN \
  --device /dev/net/tun:/dev/net/tun \
  -p 19099:19099 -p 19199:19199 \
  -v ./vnt-nas-data:/data \
  -e TZ=Asia/Shanghai \
  vnt-nas:latest
```

启动后访问：

| 地址 | 用途 |
|------|------|
| `http://<主机>:19099` | 管理页：核心更新 / 访问设置 / 组网面板 / 日志 |
| `http://<主机>:19199` | 组网面板直连（排查用；日常从管理页进即可） |

首次启动会自动下载对应架构的 `vnt2_web` 核心（约 8 MB）。若下载慢或失败，
见下方「下载加速」。

---

## 关于权限（重要）

组网要创建 TUN 虚拟网卡，所以容器需要：

- `--cap-add=NET_ADMIN`
- `--device /dev/net/tun:/dev/net/tun`

**不想给这些权限**：打开管理页 →「组网面板」→ 把网卡模式设为 `no`
（只做端口映射、不建网卡），此时可以从 compose 里删掉 `cap_add` 与 `devices` 两段。
功能受限，但能回避特权要求。

---

## 环境变量

全部可选，不设则用括号内默认值。

| 变量 | 默认 | 说明 |
|------|------|------|
| `PANEL_PORT` | `19099` | 管理页端口 |
| `VNT_WEB_PORT` | `19199` | vnt2_web 组网面板端口 |
| `WEB_TOKEN` | 自动随机 | 访问令牌（≥16 位）。不设则首次启动生成，存于 `/data/bin/webtoken` |
| `MIRROR` | 不设 | 下载镜像前缀，如 `https://ghfast.top` |
| `PROXY` | 不设 | 网络代理，如 `socks5h://127.0.0.1:1080` |
| `LISTEN_ALL` | 不设 | `1`=组网面板监听全部网卡；`0`=仅回环。不设则沿用管理页里选过的值 |
| `AUTO_DOWNLOAD_CORE` | `1` | 首次启动自动下载核心；`0` 则交给管理页手动下载 |
| `TZ` | `Asia/Shanghai` | 时区 |

> **为什么好几项「默认」是不设？**
> 镜像里刻意不把这些写进 `ENV`。一旦写进去，变量就永远存在，容器会误以为你每次都在
> 显式指定，于是把你在管理页里改过的设置（端口、监听范围、镜像、代理）**在每次重启时打回默认值**。
> 因此默认值只在程序内部兜底，**只有你真正通过环境变量传入时才覆盖**。

### 端口优先级的约定

`PANEL_PORT` / `VNT_WEB_PORT` 的取值规则：**环境变量 > `/data/ports.env` > 默认值**。

- 传了环境变量 → 覆盖 `ports.env` 并持久化
- 没传 → 沿用 `ports.env` 里已有的值（所以你在管理页改过端口后，重启不会被重置）

两个端口设成相同时，组网面板端口会自动回退为 `19199` 并在日志里提示。

---

## 数据持久化

所有运行期数据都在 `/data`（对应数据卷 `./vnt-nas-data`）：

```
/data/
├── bin/
│   ├── vnt2_web        # 核心二进制（在线更新，不属于镜像）
│   ├── webtoken        # 访问令牌（0600）
│   └── .vnt_version    # 当前核心版本
├── vnt_config/         # 组网配置（*.toml），由 vnt2_web 自己管理
├── cache/              # 下载缓存 + 回滚备份
├── log/                # main / update / shell / vnt2_web 日志
└── ports.env           # 端口记录
```

**删除容器不会丢数据**；重建容器时挂回同一个卷即可，
已下载的核心与已配置的组网都会保留。

---

## 下载加速

容器内访问 GitHub 可能受限。两种独立手段，可单独或同时使用：

**1. 镜像前缀（URL 层）**

```yaml
environment:
  MIRROR: "https://ghfast.top"
```

**2. 网络代理（网络层）**

```yaml
environment:
  # 注意：容器里的 127.0.0.1 指的是容器自己。
  # 宿主机上的代理要用 host.docker.internal 或宿主机局域网 IP。
  PROXY: "socks5h://192.168.1.10:1080"
```

两者也可启动后在管理页「访问设置」里填写，界面提供「测试连通性」。

---

## 常用操作

```bash
# 查看日志（启动过程、下载进度都在这里）
docker logs -f vnt-nas

# 只看管理页后端日志
docker exec vnt-nas tail -n 100 /data/log/main.log

# 看核心（vnt2_web）日志
docker exec vnt-nas tail -n 100 /data/log/vnt2_web.log

# 健康状态
docker inspect --format '{{.State.Health.Status}}' vnt-nas

# 停止／启动
docker compose -f docker/docker-compose.yml down
docker compose -f docker/docker-compose.yml up -d
```

---

## 自动构建（GitHub Actions）

`.github/workflows/docker.yml` 会自动构建多架构镜像并推送到 **GHCR**，
使用内置 `GITHUB_TOKEN`，**不需要配置任何密钥**。

### 触发时机

| 事件 | 生成的标签 |
|------|-----------|
| push 到 `main` | `main`、`latest`、`sha-xxxxxxx` |
| 打 tag `v1.2.3` | `1.2.3`、`1.2`、`sha-xxxxxxx`（**不带 `latest`**） |
| PR（改到 `app/**`、`docker/**` 或本 workflow） | 只验证能否构建，**不推送** |
| 手动（Actions 页面 → Run workflow） | 同 push 到 `main` |

> `latest` 只在**默认分支**（`main`）推送时生成，因此正式发版建议：
> 先在 `main` 上确认构建通过，再打 `v*` 标签获得版本号标签。

### 产出的镜像

```
ghcr.io/virkingz/fnos-vnt2:latest     # 默认分支最新
ghcr.io/virkingz/fnos-vnt2:main
ghcr.io/virkingz/fnos-vnt2:1.2.3      # 打 tag 时
ghcr.io/virkingz/fnos-vnt2:1.2
ghcr.io/virkingz/fnos-vnt2:sha-a1b2c3d
```

一份 manifest 同时包含 `linux/amd64`、`linux/arm64`、`linux/arm/v7`，
`docker pull` 会自动选对架构。

### 关于镜像可见性

本项目的 GHCR 包已设为 **Public**，可以直接匿名拉取，无需 `docker login`。

> 如果想改回私有（例如不想被公开拉取）：
> GitHub → 你的头像 → Your packages → `fnos-vnt2` → Package settings →
> 拉到 Danger Zone → Change visibility。
> 改为私有后，拉取前需先 `docker login ghcr.io`。

### 改平台或改仓库

- **只保留 x86_64 / arm64**（arm/v7 走 QEMU 模拟较慢）：
  编辑 workflow 顶部的 `PLATFORMS`，删掉 `,linux/arm/v7`。
- **推送到 Docker Hub**：见下一节。

---

## 推送到 Docker Hub（可选）

如果更希望推到 Docker Hub，改 `docker.yml` 的这三处：

1. 加登录步骤的密钥（在仓库 Settings → Secrets 添加 `DOCKERHUB_USERNAME`、`DOCKERHUB_TOKEN`）：

```yaml
      - name: 登录 Docker Hub
        if: github.event_name != 'pull_request'
        uses: docker/login-action@v3
        with:
          username: ${{ secrets.DOCKERHUB_USERNAME }}
          password: ${{ secrets.DOCKERHUB_TOKEN }}
```

2. 把 `env.REGISTRY` 去掉，并把 metadata 的 images 改为：

```yaml
          images: ${{ secrets.DOCKERHUB_USERNAME }}/vnt-nas
```

3. `permissions` 里的 `packages: write` 可保留（不再用得上，但无害）。

推送后地址为 `<用户名>/vnt-nas:latest`。Docker Hub 免费账号的
公开仓库不限量，私有仓库有数量限制。

---

## 本地构建多架构

不依赖 CI 也可以本地构建：

```bash
docker buildx create --use --name vntbuilder    # 只需一次
docker buildx build -f docker/Dockerfile \
  --platform linux/amd64,linux/arm64,linux/arm/v7 \
  -t <你的仓库>/vnt-nas:latest --push .
```

覆盖 x86_64、ARM64、ARMv7，无需为每种架构单独维护。

---

## 排障

| 现象 | 处理 |
|------|------|
| 日志停在「未发现核心」 | 网络受限。设 `MIRROR` 或 `PROXY` 后重启，或在管理页手动下载/上传核心 |
| 核心启动失败 | `docker exec vnt-nas tail /data/log/vnt2_web.log`；管理页也会给出原因 |
| 组网不通、点对网无效 | 确认容器有 `NET_ADMIN` 与 `/dev/net/tun`；或把网卡模式设为 `no` 试端口映射 |
| 健康检查一直 unhealthy | 看 `docker logs`；确认 `PANEL_PORT` 与 `-p` 映射的容器端口一致 |
| 局域网打不开 19199 | 管理页「访问设置」→ 确认监听范围是「全部网卡」（对应 `LISTEN_ALL=1`） |
| 宿主机代理连不上 | 容器内不能用 `127.0.0.1` 指宿主机，改用 `host.docker.internal` 或宿主局域网 IP |

---

## 与 fpk 版的关系

| | fpk 版 | Docker 版 |
|---|--------|-----------|
| 运行环境 | 飞牛 fnOS 应用中心 | 任意支持 Docker 的 Linux |
| 启动方式 | 飞牛调用 `cmd/main` | 容器入口脚本 |
| 环境变量 | 飞牛注入 `TRIM_*` | 入口脚本自行设定 |
| 应用代码 | `app/shell` | **同一份 `app/shell`** |
| 数据目录 | `/vol{n}/@appdata/vnt-nas` | `/data` 数据卷 |

两者可以并存，互不影响（只要端口不冲突）。

# 开发说明

## 环境要求

- Python 3.8+（仅标准库；只有生成图标需要 Pillow）
- Bash
- 打包用 `bash scripts/pack.sh`，**不依赖任何外部工具**

## 目录结构

```
.
├── manifest                  # 应用元数据（飞牛要求，key = value）
├── ICON.PNG / ICON_256.PNG   # 应用图标（64×64 / 256×256）
├── app/                      # → 安装后的 target/（TRIM_APPDEST）
│   ├── ui/config             # 桌面入口定义（飞牛要求）
│   ├── ui/images/            # 入口图标
│   └── shell/                # 壳本体
│       ├── lib.sh            # 公共函数：路径/日志/架构映射/下载/令牌
│       ├── shell_main.py     # 服务入口（Unix socket + TCP 双监听）
│       ├── shell_server.py   # 业务逻辑：进程托管、更新器、令牌、代理
│       ├── shell_http.py     # HTTP 路由、静态资源、自检
│       └── web/              # 管理页前端（原生 HTML/CSS/JS，无构建步骤）
├── cmd/                      # 生命周期脚本（飞牛要求）
├── config/                   # privilege（root）+ resource
├── wizard/                   # 安装/配置/升级/卸载向导
└── scripts/                  # 开发脚本与测试
```

## 各文件职责

| 文件 | 职责 |
|------|------|
| `lib.sh` | 被 `cmd/*` 引用。路径、日志、架构映射、curl 封装（含代理）、令牌读写 |
| `shell_main.py` | 进程入口。起 Unix socket（网关）与 TCP（管理页端口）双监听，拉起核心 |
| `shell_server.py` | 核心能力：端口/令牌/代理/镜像/监听范围配置、`vnt2_web` 进程托管、下载与回滚 |
| `shell_http.py` | HTTP 层：路由、静态资源、面板自检、multipart 上传解析 |
| `web/app.js` | 管理页交互。注意：**不得引用未声明变量**（见下方注意事项） |

## 打包

```bash
bash scripts/pack.sh            # 产出 vnt-nas.fpk
bash scripts/pack.sh 1.0.1      # 指定版本
```

打包器自行组装 tar.gz，并确保：

- `cmd/*` 权限为 **755**（官方 fnpack 在 Windows 上会打成 666）
- 行尾统一转 LF
- `manifest` 的 `checksum` 与 `app.tgz` 的 MD5 一致

### 关于官方 fnpack

`scripts/build.sh` 使用官方 `fnpack`。它生成的包结构合法，但在 Windows 上
会把脚本权限打成 `666`，飞牛可能因不可执行而失败。**推荐用 `pack.sh`。**

## 测试

```bash
for t in scripts/test_*.py; do python "$t"; done
```

| 测试 | 覆盖内容 |
|------|----------|
| `test_update.py` | 更新机制：版本查询、下载、校验、原子替换、失败回滚、架构映射 |
| `test_rewrite.py` | 路径改写工具函数（保留守护，当前未用于面板代理） |
| `test_mount.py` | 挂载点识别、端口模型 |
| `test_upload.py` | 版本号解析、zip/ELF 提取、multipart、上传安装 |
| `test_proxy.py` | 代理读写、协议识别、本机地址绕过代理、端到端 |
| `test_embed.py` | 桌面入口、双端口模型、端口边界、令牌自动注入、前端契约 |

全部测试不联网（用本地 HTTP 服务模拟），可在 CI 中直接运行。

## 开发注意事项

这些是踩过的坑，改代码时请留意：

### 1. vnt2_web 不能做子路径反向代理

它的前端资源用 **RustEmbed 编译在 ELF 二进制内部**，JS 里的 `/api/...`
是拼接用的常量片段。若挂在子路径下改写响应，会连**机器码一起改坏**
（实测命中 232 处 ELF 数据）。因此面板必须用它**自己的端口**访问。

### 2. 前端不得引用未声明变量

曾因删掉 `var PAGE` 声明却留下赋值语句，导致 JS 抛错中断、
面板区域一片空白。`test_embed.py` 会做静态检查，使用前会先剥离字符串与注释。

### 2b. 不做内嵌，改为跳转 + 独立图标

管理页**不内嵌**组网面板。原因是双重的：vnt2_web 的前端资源编译在 ELF 内、
只能根路径访问（见第 1 条），而且管理页经网关访问时是 HTTPS，
内嵌 HTTP 面板会被浏览器按混合内容策略拦截（表现为空白）。

因此桌面提供两个独立图标：
- `vnt-nas.main`：`type: iframe`，走统一网关，打开管理页
- `vnt-nas.panel`：`type: url` + `port: ${wizard_panel_port}`，
  用浏览器新标签直接打开 vnt2_web，不受 iframe 与混合内容限制

`${字段名}` 可引用**任意**向导字段（不只是 `wizard_port`）。
真实仓库佐证：`fn-rtl8125`、`FN_WOL`、`Fndesk` 等均使用此形式，
其中 `Fndesk` 引用的就是自定义字段 `wizard_app_music`。

### 3. 访问本机地址必须绕过代理

配置了 HTTP/SOCKS 代理后，访问 `127.0.0.1:<核心端口>` 若也走代理，
会出现 `Empty reply from server`。所有出网函数都需经 `is_local_url()` 判断。

### 4. 令牌可由 URL 传入

vnt2_web 支持 `/?token=xxx`，前端会读取、存入 `localStorage` 并自动登录，
随后把地址栏的令牌抹掉。因此跳转打开用带令牌的地址；
界面上只展示不含令牌的版本。

### 5. Python 的 urllib 不支持 SOCKS

配置代理后所有出网请求统一走 `curl`（原生支持 socks5/socks5h）。
未配置代理时仍用 urllib，不额外产生进程。

## 发布

推送到 `main` 会自动跑测试与打包；打 `v*` 标签会自动创建 Release 并附上 fpk：

```bash
git tag v1.0.0
git push origin v1.0.0
```

发布前请更新 `manifest` 的 `version` 与 `changelog`，以及 `CHANGELOG.md`。

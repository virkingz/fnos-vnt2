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
| `test_panel_proxy.py` | 面板反代：base/令牌/API 钩子注入、HTML 资源改写、网关挂载点、上游不可用错误页 |

全部测试不联网（用本地 HTTP 服务模拟），可在 CI 中直接运行。

## 开发注意事项

这些是踩过的坑，改代码时请留意：

### 1. 不要改磁盘上的二进制

vnt2_web 的前端资源用 **RustEmbed 编译在 ELF 二进制内部**，
其中的 `/api/...` 在字符串常量池里**首尾相连、没有引号**。
因此**绝不能对磁盘上的二进制做字符串替换**——会连机器码一起改坏。

> 注意区分：这只约束「改二进制」。**HTTP 响应里的文本可以安全改写**，
> 因为那已是解压后的纯 HTML/JS，与 ELF 无关。当前实现只改写 HTML 中的
> `/assets/...`（JS 改由 API 钩子接管，见 2b）。

### 2. 前端不得引用未声明变量

曾因删掉 `var PAGE` 声明却留下赋值语句，导致 JS 抛错中断、
面板区域一片空白。`test_embed.py` 会做静态检查，使用前会先剥离字符串与注释。

### 2b. 内嵌组网面板：必须注入 `<base>` + API 钩子

组网面板内嵌在管理页的 `/core/` 子路径下。踩过的坑按重要性排列：

**① `<base>` 是必需的（否则必然白屏）**

vnt2_web 用 Vue Router 的 `createWebHistory()`，它在未显式传 base 时
会读取 `<base href>`：

```js
function normalizeBase(base){
  if(!base){ const el=document.querySelector('base')
             base=(el&&el.getAttribute('href'))||'/' } ... }
```

不注入时 base 取 `/`，而实际路径是 `/app/vnt-nas/core/`，
路由表里没有这一条 → 渲染空白。直连（路径恰为 `/`）则正常。
**症状特征：资源全部 200、直连能开、代理白屏。**

**② 绝不能设 `globalThis.__VNT_DESKTOP__`**

该标志为真时前端会把令牌置空（`At ? "" : ...`），它只适用于
「宿主代发全部 API」的场景。我们并未接管 API 时设它，
会导致所有请求 401 → 前端登出 → 白屏。

**③ 用官方 API 钩子代替改写 JS**

前端所有请求都经 `De()` 发出，它开头即判断：

```js
if (globalThis.__VNT_API_REQUEST__) {
  const l = await globalThis.__VNT_API_REQUEST__({method, path, body})
  if (l.code !== 0) throw ...; return l.data }
```

定义该钩子后，前端不再自己 fetch，由宿主拼出 `<挂载点>/core + path`
并附令牌。**因此 JS 无需任何改写**——minify 后的代码做字符串替换
风险高且易遗漏。现在只改写 HTML 里的 `/assets/...`。

**④ 令牌按「源」隔离**

令牌存 localStorage，直连端口与经代理内嵌是**不同的源**，
所以必须注入脚本把令牌写进当前源的 localStorage。

### 3. 访问本机地址必须绕过代理

配置了 HTTP/SOCKS 代理后，访问 `127.0.0.1:<核心端口>` 若也走代理，
会出现 `Empty reply from server`。所有出网函数都需经 `is_local_url()` 判断。

⚠ 新增出网函数时容易漏掉这点：`http_raw()`（面板反代用）曾因此
连 `127.0.0.1` 都失败，修法是显式 `ProxyHandler({})` 禁用环境代理。

### 4. Python 的 urllib 不支持 SOCKS

配置代理后所有出网请求统一走 `curl`（原生支持 socks5/socks5h）。
未配置代理时仍用 urllib，不额外产生进程。

## 发布

推送到 `main` 会自动跑测试与打包；打 `v*` 标签会自动创建 Release 并附上 fpk：

```bash
git tag v1.0.0
git push origin v1.0.0
```

发布前请更新 `manifest` 的 `version` 与 `changelog`，以及 `CHANGELOG.md`。

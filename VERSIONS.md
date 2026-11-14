# 版本与方案记录

本文记录本项目在「组网面板如何打开」上的方案演变，以及各版本的回溯方式。

---

## 如何回到某个版本

所有版本都保留了 git 标签，随时可切换：

```bash
# 查看所有可选版本
git tag -l

# 回到「面板图标直连端口」版（当前版本）
git checkout v-current-port-direct

# 回到「双协议代理内嵌」半成品（已暂存，不在标签里）
git stash list
git stash apply stash@{0}

# 回到被覆盖的「/go 跳转免登录」版
git checkout backup/before-revert

# 回到最初的发布版
git checkout v1.0.0
```

> 切换后重新打包：`bash scripts/pack.sh`（若被沙箱拦截，见 DEVELOPMENT.md 的手工打包步骤）

---

## 方案演变（时间顺序）

### 版本 A：`v1.0.0` — 最初发布版

- 桌面两个图标（管理页 + 组网面板）
- 管理页内嵌面板（iframe）
- 组网面板直连 `19199` 端口

### 版本 B：`31e5727` — 撤销内嵌，改独立图标

用户要求「不内嵌，加一个跳转打开面板」。撤销了当时的双协议代理方案。

- 桌面两个图标
- 管理页**不内嵌**，只保留跳转按钮
- 面板图标直连 `19199`（`port: ${wizard_panel_port}`，`url: /`）

**已知取舍**：桌面图标打开会停在登录页要求粘贴令牌
（静态配置拿不到运行时生成的令牌）。

### 版本 C：`744bbff` — 修复登录问题（标签 `backup/before-revert`）

针对版本 B 的取舍做的修复：

- 面板图标改指向管理页 `/go` 路径
- 后端拼出带令牌地址后 302 过去 → 图标也免登录
- 新增 `test_panel_go.py` 端到端验证

### 版本 D：三端口内嵌代理（半成品，已 stash）

用户改主意，要求「恢复双端口内嵌 + 3 个端口全可配」。
实现到一半用户又要求回退，代码存于 `git stash`：

- 重建 `shell_tls.py`（同一端口支持 HTTP 与 HTTPS，按握手首字节判别）
- 三个端口全部可配：管理页 / 面板代理 / 核心内部
- **未完成**：前端未恢复内嵌，向导未收第三端口

### 版本 E：`320efb3` — 当前版本（标签 `v-current-port-direct`）

一次性回退到版本 B 的状态。

- 桌面**两个图标**
- 管理页**不内嵌**，跳转按钮免登录（走 `/api/panel`）
- 面板图标直连端口，需输令牌（版本 B 的既定行为）

---

## 关于「封装成原生应用形式」的调研结论

用户希望组网面板能像飞牛原生应用一样，通过统一网关
（`https://NAS:5666/app/xxx`）打开。**实测结论：当前不可行**，原因如下。

### 障碍一：统一网关只认 Unix Socket

官方文档明确：

> 统一网关入口由 `gatewayPrefix` 和 `gatewaySocket` 决定。
> **`protocol` 和 `port` 会被忽略，不参与统一网关路由。**

网关只会把请求转发到应用自己创建的 socket（如 `app.sock`），
不会转发到应用监听的 TCP 端口。因此「网关直连 19199」这条路不存在。

### 障碍二：vnt2_web 的前端用绝对路径

从二进制中提取其内嵌的 `index.html`（RustEmbed 编译进 ELF）：

```html
<script type="module" crossorigin src="/assets/index-kNrnhhWo.js"></script>
<link rel="stylesheet" crossorigin href="/assets/index-Cgs9Rwnv.css">
```

- 资源引用是**绝对路径** `/assets/...`
- **未声明 `<base>`**

若经 `/app/vnt-nas/panel/` 访问，浏览器会去根路径 `/assets/...` 取资源 → 404 → 白屏。

### 障碍三：JS 里的 API 路径无法改写

二进制中的 `/api/...` 是首尾相连打包在数据段的：

```
b'/api/version/api/runtime/api/info/api/peers/api/ro'
```

相邻字符串仅隔一个字节。**按字节改写会破坏机器码**
（历史实测：命中 232 处 ELF 数据）。因此「反代 + 改写响应」不可行。

### 可行的折中（若一定要走网关）

在 `app.sock` 上自建代理：

- 收到 `/app/vnt-nas/panel/*` 时转发到 `127.0.0.1:19199`
- 在返回的 HTML 里注入 `<base href="/app/vnt-nas/panel/">`
- 改写 HTML 中的 `/assets/` 前缀（仅 3 处，安全）

**局限**：JS 内的 `/api/...` 仍改不了，内嵌后 API 会打到根路径，
**功能不完整**。因此不建议采用。

### 推荐做法

维持「独立端口 + 独立窗口」的形态。若要免登录，
用管理页的「打开组网面板」按钮（走 `/api/panel`，自动带令牌）。

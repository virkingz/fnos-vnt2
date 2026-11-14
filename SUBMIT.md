# 上架飞牛应用中心

本文档用于向飞牛官方应用中心提交本应用，内含**提交入口**、**申请材料**与**自检结果**。

---

## 一、提交入口

官方开发者后台上线前，通过以下任一方式提交（二者都可用，建议同时进行）：

### 方式 A：飞书申请表（推荐，最快）

填写飞牛官方「应用上架申请表」：

> https://trim-nas.feishu.cn/share/base/form/shrcn5m4MsyeqvEh6c4tm97pQKb

提交后等待官方联系。官方说明原文：「请求上架飞牛应用中心，请填写申请表，提交后，请静候。」

### 方式 B：开发者先锋交流群

1. 从 [飞牛官网](https://fnnas.com/) 右上角「关注飞牛」→ `微信群`，扫码加入任意飞牛粉丝群
2. 在群内联系 **飞牛社区主理人**，备注「开发者」
3. 申请加入 **应用中心开发者先锋交流群**
4. 按工作人员指引提交应用信息、应用包与测试材料

### 参考文档

- 上架流程：https://developer.fnnas.com/docs/quick-started/publish-application/
- Manifest 字段：https://developer.fnnas.com/docs/core-concepts/manifest/
- 开发者站首页：https://developer.fnnas.com/

---

## 二、申请材料

| 材料 | 状态 | 位置 / 说明 |
|------|------|-------------|
| `.fpk` 安装包 | 已有 | GitHub Release 的 `vnt-nas.fpk`（约 111 KB） |
| 应用图标 | 已有 | 仓库根目录 `ICON.PNG`（64×64）、`ICON_256.PNG`（256×256） |
| 截图 | **待补** | 需真实界面截图，见下方第 4 节 |
| 应用描述 | 已有 | `manifest` 的 `desc` 字段 |
| 项目主页 / 反馈渠道 | 已有 | https://github.com/virkingz/fnos-vnt2 |
| 开源协议 | 已有 | MIT，见 `LICENSE` |

### 建议在申请时提供的信息

```
应用名称：VNT 组网
应用标识：vnt-nas
版本号　：1.0.0
安装包　：https://github.com/virkingz/fnos-vnt2/releases
项目地址：https://github.com/virkingz/fnos-vnt2
开发者　：VirKing
架构支持：x86_64 / ARM64
最低系统：fnOS 1.1.0
```

---

## 三、Manifest 字段自检

按官方 Manifest 文档逐项核对：

| 字段 | 值 | 说明 |
|------|-----|------|
| `appname` | `vnt-nas` | 应用唯一标识 |
| `version` | `1.0.0` | 版本号 |
| `display_name` | `VNT 组网` | 应用中心展示名称 |
| `desc` | 见 manifest | 应用描述 |
| `source` | `thirdparty` | 第三方应用 |
| `platform` | `all` | 包内不含架构二进制，核心在运行时按架构下载 |
| `os_min_version` | `1.1.0` | 最低支持的系统版本 |
| `maintainer` / `maintainer_url` | `VirKing` + 仓库地址 | 开发者信息 |
| `distributor` / `distributor_url` | `VirKing` + 仓库地址 | 发布者信息 |
| `helpurl` | 仓库地址 | 帮助与反馈地址 |
| `service_port` | `19988` | 服务端口 |
| `ctl_stop` | `true` | 允许在应用中心启动/停止 |
| `desktop_uidir` | `ui` | 桌面入口目录 |
| `desktop_applaunchname` | `vnt-nas.main` | 应用卡片打开的入口 |
| `changelog` | 见 manifest | 更新说明 |

> `checksum` 由打包脚本自动写入，无需手工维护。

### 未填写的字段及原因

- `os_max_version`：官方说明「如果应用存在明确的兼容上限，可填写该字段」。
  本应用无常驻的版本上限，按官方「不要声明超过应用真实支持能力的范围」的要求**不填**。
- `install_type`：留空即由用户在安装时选择存储位置，符合本应用需求。
- `install_dep_apps`：本应用无依赖应用。
- `disable_authorization_path`：本应用需要用户选择存储目录存放核心与配置，故不使用。

---

## 四、截图准备（待补）

官方要求：**截图应展示真实应用界面和核心流程，不应使用占位页面或开发阶段的临时设计图。**

建议补以下 4～6 张，存入仓库 `screenshots/` 目录：

1. 应用中心 / 桌面入口（展示两个图标）
2. 管理页「核心更新」标签（展示版本与检查更新）
3. 管理页「访问设置」标签（展示令牌、端口、代理、监听范围）
4. 管理页「组网面板」标签（展示面板地址、打开按钮与自检）
5. 管理页「日志」标签
6. 安装向导（只填一个面板端口）

截图要求：

- PNG 格式，宽度建议 1280px 以上
- 界面语言使用简体中文
- **不要**包含真实令牌、公网 IP、组网密码等敏感信息（令牌请先遮挡或改用测试值）

---

## 五、发布前测试清单

官方要求「不要只依赖一次成功安装」，需在计划支持的 fnOS 版本与硬件架构上测试。
上架前请逐项确认：

- [ ] 可在 fnOS 上正常安装、打开、运行（x86_64）
- [ ] 可在 fnOS 上正常安装、打开、运行（ARM64，如有条件）
- [ ] 安装向导只填面板端口即可完成
- [ ] 核心可正常下载 / 上传，组网可正常创建与启动
- [ ] 令牌自动登录生效（打开面板免手工粘贴）
- [ ] 桌面「VNT 组网面板」图标能直接打开组网界面
- [ ] 改动监听范围后，面板的可访问性随之变化
- [ ] 「自检面板可用性」各项结论正确
- [ ] 升级 fpk 后，已下载的核心与配置不丢失
- [ ] 卸载后残留文件与进程正确清理
- [ ] 网络异常时的表现：下载失败有明确提示且可回滚
- [ ] 未配置代理时不影响本机访问（回环请求不走代理）

### 关于权限的说明

本应用声明 `run-as: root`，原因是：

- 需要创建与管理 TUN 虚拟网卡（组网必需）
- 需要在系统端口范围外监听服务端口并托管核心进程

除上述用途外，应用**不访问用户文件**（`config/privilege` 中无额外授权目录），
仅在自身数据目录内读写配置与核心二进制。

---

## 六、可选：第三方应用源收录

除官方应用中心外，也可提交到社区应用源 [2FStore](https://github.com/yuexps/2FStore)，
其会自动从本仓库读取 `manifest`、`ICON_256.PNG` 与 Release 中的 `.fpk`。

提交方式：在 [2FStore Issues](https://github.com/yuexps/2FStore/issues) 选择「应用提交」模板，
填写应用 ID、名称与仓库地址即可。

本仓库已满足其规范要求：

- 根目录有 `ICON.PNG` 与 `ICON_256.PNG`
- 根目录有 `manifest`
- GitHub Release 中已附带 `.fpk`

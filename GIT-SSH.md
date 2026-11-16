# Git 用 SSH 推送

本文记录本机已配置好的 SSH 推送方式，以及换新环境时的复现步骤。

## 当前状态

本机已配置完成，`origin` 走 SSH：

```
origin      git@github.com:virkingz/fnos-vnt2.git      ← 当前使用
old-origin  https://github.com/virkingz/fnos-vnt2.git  ← 备份，可回退
```

本机密钥：

```
类型    ED25519
文件    ~/.ssh/id_ed25519  (私钥)
        ~/.ssh/id_ed25519.pub (公钥)
指纹    SHA256:YYxxL7jIXjJ67g4WfQ+qpfA4R87hA5xUxkz0bDWG+cg
```

## 换新环境：4 步

### 1. 生成密钥

```bash
ssh-keygen -t ed25519 -C "你的邮箱或备注"
# 一路回车即可（默认路径 ~/.ssh/id_ed25519）
```

已有密钥就跳过这步。查看现有公钥：

```bash
cat ~/.ssh/id_ed25519.pub
```

### 2. 把公钥加到 GitHub

复制上一步输出的整行内容，然后：

**GitHub → 右上角头像 → Settings → SSH and GPG keys → New SSH key**

- Title 随便填（如 `办公机`）
- Key type 选 `Authentication Key`
- Key 粘贴刚才复制的整行

> 注意：粘贴的是 `.pub` **公钥**，不要贴私钥（`id_ed25519`，无后缀那个）。

### 3. 验证认证

```bash
ssh -T git@github.com
```

看到下面这行就成功了：

```
Hi <你的用户名>! You've successfully authenticated, but GitHub does not provide shell access.
```

首次连接会问 `Are you sure you want to continue connecting?`，输入 `yes` 回车。

### 4. 切换仓库的 remote

```bash
cd <仓库目录>

# 备份原 HTTPS 地址（便于回退）
git remote add old-origin https://github.com/virkingz/fnos-vnt2.git

# 切换为 SSH
git remote set-url origin git@github.com:virkingz/fnos-vnt2.git

# 确认
git remote -v
```

## 验证 SSH 通道真的可用

**光有 `ssh -T` 通过还不够**（那只是认证）。三种操作都测一遍：

```bash
git ls-remote origin main   # 连通性
git fetch origin main       # 拉取
git push origin main        # 推送（已最新会显示 Everything up-to-date）
```

## 回退到 HTTPS

SSH 在受限网络下可能不通（有些企业网络封 22 端口）。切回：

```bash
git remote set-url origin https://github.com/virkingz/fnos-vnt2.git
```

## 常见问题

### `Permission denied (publickey)`

密钥没被 GitHub 认到。检查：

```bash
ssh -T git@github.com          # 看报错
ssh-add -l                     # 看 ssh-agent 里有没有加载密钥
ssh-add ~/.ssh/id_ed25519      # 手动加载
```

确认 GitHub 上那条 key 的**指纹**和本地一致：

```bash
ssh-keygen -lf ~/.ssh/id_ed25519.pub
```

### 端口 22 被封

用 GitHub 的 443 端口替代。在 `~/.ssh/config` 加：

```
Host github.com
    HostName ssh.github.com
    Port 443
    User git
```

然后 `ssh -T git@github.com` 测试（应仍然显示 `Hi <用户名>!`）。

### 连接超时

企业网络常见。依次试：

1. 上面的 443 端口方案
2. 切回 HTTPS（见上一节），HTTPS 可走系统 HTTP 代理

### 已经在用 HTTPS 凭据，切 SSH 后没影响吧？

没影响。SSH 密钥和 HTTPS token 并存，互不干扰。

**但注意**：SSH 密钥**不能用于 GitHub API 调用**。`git credential fill` 取到的
HTTPS token 仍然独立存在于系统凭据管理器里，调 API（如更新 Release 附件、
查 CI 状态）时仍然需要它。

## 一次性配置多台机器

每台机器各自生成密钥、各自添加到 GitHub，共用同一个账号即可。
GitHub 允许一个账号绑定多个 SSH key，建议 Title 写清机器名便于日后清理。

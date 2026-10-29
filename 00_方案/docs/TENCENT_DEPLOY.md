# 腾讯云：部署 BTC/ETH 只读观察器

本流程针对新建 Ubuntu 24.04 轻量应用服务器。部署范围为公开行情和候选信号，不迁移 Binance OAuth 凭证，不开启真实订单。当前本机曾遇到 HTTP 451；购买云服务器不保证行情访问恢复，也不改变平台的地区服务资格。请按平台允许的范围选择地域，先月付验证。

## 1. 创建服务器

进入腾讯云「轻量应用服务器 → 新建」，使用以下配置：

| 项目 | 选择 |
| --- | --- |
| 地域 | 平台允许且行情可用的地域；新加坡是待验证的候选 |
| 创建方式 | 操作系统镜像 |
| 系统 | Ubuntu Server 24.04 LTS，x86_64 |
| 配置 | 2 核、2GB 内存、至少 40GB SSD |
| 时长/数量 | 1 个月、1 台 |
| 登录 | SSH 密钥；保管下载的私钥 |
| 名称 | quant-observer |

2026-10-28 查询的腾讯云海外 Linux 入门套餐为 2 核2GB、40GB SSD、20Mbps、512GB/月流量，30 元/月；具体以控制台当前地域和可售套餐为准。这里的峰值带宽不是持续带宽承诺。

官方文档：

- [创建 Linux 实例](https://cloud.tencent.com/document/product/1207/44548/)
- [价格总览](https://cloud.tencent.com/document/product/1207/73452)
- [管理 SSH 密钥](https://cloud.tencent.com/document/product/1207/44573)

## 2. 登录与防火墙

Ubuntu 默认登录用户为 `ubuntu`。控制台绑定同地域的 SSH 密钥；以下命令假设私钥放在本机 WSL 的 `~/.ssh/tencent-quant.pem`。不要把私钥或服务器密码发到聊天里。

在轻量服务器「防火墙」中，仅将入站 TCP 22 放行给你当前实际的 SSH 出口公网 IP/32。SSH 的出口可能与浏览器代理不同，修改规则前确认，以免锁住连接。此阶段没有网站，不需要放行入站 80、443、9443。行情使用主动发出的连接，出站需允许 HTTPS 443 和 WSS 9443。腾讯云轻量防火墙主要控制入流量，不要为行情添加入站 9443。

本机 WSL：将 `服务器公网IP` 替换为购买后显示的实际 IP。

```bash
chmod 600 ~/.ssh/tencent-quant.pem
ssh -i ~/.ssh/tencent-quant.pem ubuntu@服务器公网IP
```

首次 SSH 连接先核对主机指纹。没有公网 SSH 条件时，可先用腾讯云控制台提供的登录入口。

## 3. 上传源代码包

已准备好的部署包位于本机 `/home/kkk/Quant/dist/quant-observer-20261028.tar.gz`。它只包含允许的源代码和部署文件，不包含 `.venv`、运行数据库、API 密钥或 OAuth 凭证。包内 `DEPLOY_MANIFEST.json` 保存每个源文件的 SHA-256。

在本机 WSL 执行：

```bash
scp -i ~/.ssh/tencent-quant.pem \
  /home/kkk/Quant/dist/quant-observer-20261028.tar.gz \
  /home/kkk/Quant/dist/quant-observer-20261028.tar.gz.sha256 \
  ubuntu@服务器公网IP:/tmp/
```

然后在服务器终端执行：

```bash
cd /tmp
sha256sum -c quant-observer-20261028.tar.gz.sha256
sudo install -d -o root -g root -m 0755 /opt/quant
sudo tar --no-same-owner -xzf /tmp/quant-observer-20261028.tar.gz -C /opt/quant
sudo bash /opt/quant/deploy/tencent/install-observer.sh
```

仅在新建服务器的空 `/opt/quant` 目录中解包。已经存在服务时，先按升级流程备份并停机，不要覆盖正在运行的代码。安装脚本会安装 Python 3.12、虚拟环境与项目依赖，创建专用低权限用户和 systemd 服务，但不会自动启动观察器。

## 4. 检查行情访问

在服务器执行：

```bash
sudo -u quant-observer timeout --kill-after=5s 45s \
  /opt/quant-venv/bin/python /opt/quant/scripts/crypto_market_check.py
```

只有输出 `ok: true`，且两个币的 REST 和 WebSocket 共四项检查均通过，才进入下一步。返回 HTTP 403、418、451 时停止，并向行情服务提供方核对可用范围；不进行代理切换或替换 endpoint 重试。429 应等待服务端限频窗口后再试。网络不通也先定位原因，不把购买成功当成可用证明。

## 5. 启动常驻服务

```bash
sudo systemctl enable --now quant-observer
sudo systemctl status quant-observer --no-pager
sudo journalctl -u quant-observer -n 30 --no-pager
```

数据库位于 `/var/lib/quant-observer/observation.sqlite`。在服务器查询：

```bash
sudo -u quant-observer /opt/quant-venv/bin/quant crypto watch-status \
  --db /var/lib/quant-observer/observation.sqlite
```

确认 `service_running: true`、两个 `data_current: true`，然后等一个完整小时收盘确认各新增一根 K 线和一条信号。初次生成的历史信号不计入连续观察期。观察器无需 Binance Key；本机已经授权的 OAuth MCP 不会自动变成服务器上的账户连接。

systemd 服务可在断开 SSH 后运行并在开机后启动；普通网络错误按退避策略重连，异常退出有重启限制。HTTP 403/418/451 或数据冲突导致退出码 2 时不自动重启，避免无限重试。修复原因后人工执行 `sudo systemctl reset-failed quant-observer`、`sudo systemctl start quant-observer`。

## 6. 停止、日志与备份

```bash
# 停止；下次开机仍会启动
sudo systemctl stop quant-observer

# 停止并取消开机启动
sudo systemctl disable --now quant-observer

# 实时查看日志，Ctrl+C 仅退出日志查看
sudo journalctl -u quant-observer -f
```

一致性备份使用 SQLite backup API，不能只复制运行中的 `.sqlite` 而忽略 WAL 文件：

```bash
sudo -u quant-observer /opt/quant-venv/bin/python /opt/quant/scripts/backup_crypto_observer.py \
  --db /var/lib/quant-observer/observation.sqlite \
  --output /var/lib/quant-observer/backups/manual-001.sqlite
```

备份名必须未被使用。此命令仅生成同机备份；后续应通过自己的安全存储保留异地副本。升级代码前停服务并备份；实现/策略指纹变化后须换新观察数据库并保留旧数据库，不要篡改指纹。

本次部署不含外部通知服务、主机故障自动迁移或实盘执行。systemd 显示 active 也不等于行情新鲜，应结合 `watch-status` 检查。完整实盘还需要订单恢复、用户数据流、交易所账目对账、Testnet 验收、明确风险参数和允许的服务资格。

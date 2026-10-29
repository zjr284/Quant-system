# 腾讯云部署 Binance Spot Testnet 自动下单

此部署用于虚拟资金的端到端订单验收。它只连接 `https://testnet.binance.vision` 和 `wss://stream.testnet.binance.vision`；生产下单没有被实现。代码先读取 Testnet 已收盘小时线，使用当前候选 SMA24/72 规则，只对 BTCUSDT、ETHUSDT 做现货限价 IOC。每个币目标仓位 25%，组合目标上限 50%；默认每单 25 USDT、日内亏损锁定 10 USDT、价格偏差 0.5%。这些仅是 Testnet 验收参数，不代表策略已经有效。

首次启动只载入历史 K 线，不回放历史订单。之后每根新收盘小时线最多处理一次。服务重启会先查询本地未决订单和交易所开放订单；无法确认的状态会锁定并退出，须人工检查日志和 Testnet 账户后处理。没有用户数据流，因此它还不能满足实盘验收。

## 先做 Testnet 联通

打开 [Binance Spot Test Network](https://testnet.binance.vision/)，通过 GitHub 登录并在页面生成测试网 API Key。该网络会自动发放虚拟资产；余额和订单都不是真实资金，测试网也会定期重置。不要使用真实 Binance API Key、Agentic 子账户 OAuth 凭证或聊天中的账户授权。Testnet 凭证与主网凭证不能互换。

新建腾讯云轻量服务器时使用 Ubuntu Server 24.04 x86_64，2 核、2GB、40GB SSD，按月购买。设置 SSH 密钥；入站 TCP 22 限制为你实际 SSH 出口 IP/32，不开放其他入站端口。选择符合 Binance 服务条款、且通过后续网络预检的地域；不能把云地域当作绕开账户资格限制的方法。

## 安装

在 WSL 上传本部署包和 SHA 文件：

```bash
scp -i ~/.ssh/tencent-quant.pem \
  /home/kkk/Quant/dist/quant-testnet-20261028-r2.tar.gz \
  /home/kkk/Quant/dist/quant-testnet-20261028-r2.tar.gz.sha256 \
  ubuntu@服务器公网IP:/tmp/
```

在 Ubuntu 服务器执行：

```bash
cd /tmp
sha256sum -c quant-testnet-20261028-r2.tar.gz.sha256
sudo install -d -o root -g root -m 0755 /opt/quant
sudo tar --no-same-owner -xzf /tmp/quant-testnet-20261028-r2.tar.gz -C /opt/quant
sudo bash /opt/quant/deploy/tencent/install-testnet.sh
```

创建仅 root 可读的凭证文件。将下面占位内容替换为 **Spot Testnet** 的 key 和 secret，不要把真实主网凭证写入：

```bash
sudo install -o root -g root -m 0600 /dev/null /etc/quant-testnet.env
sudo nano /etc/quant-testnet.env
```

文件内容：

```text
BINANCE_TESTNET_API_KEY=填入测试网APIKey
BINANCE_TESTNET_API_SECRET=填入测试网Secret
```

## 预检并启动

先用四项公开行情检查验证 BTC/ETH 的 REST 与 WebSocket：

```bash
sudo -u quant-testnet timeout --kill-after=5s 45s \
  /opt/quant-venv/bin/python /opt/quant/scripts/testnet_market_check.py
```

只有 `ok: true` 才启动。服务启动后会执行 Testnet 账户/开放订单核对，然后装载 K 线；真实主网完全不在此流程中：

```bash
sudo systemctl enable --now quant-testnet
sudo systemctl status quant-testnet --no-pager
sudo journalctl -u quant-testnet -n 50 --no-pager
sudo -u quant-testnet /opt/quant-venv/bin/quant crypto testnet-status \
  --db /var/lib/quant-testnet/trading.sqlite
```

主机必须持续在线至少 72 小时，观察信号、订单回执、部分成交/未成交、进程重启后的对账及风控锁定。Testnet 的撮合、流动性和账户行为不保证等同主网。

停止或查看日志：

```bash
sudo systemctl stop quant-testnet
sudo systemctl disable --now quant-testnet
sudo journalctl -u quant-testnet -f
```

本次包不含短信/邮件告警、用户数据流、生产 API 适配器或真实交易授权。只有后续补齐交易所实时账户事件、订单恢复和余额/成交对账，并经独立验收后，才可以设计主网方案。当前 Agentic MCP 授权用于支持的 AI 客户端会话；不能直接当作此 Python systemd 服务的持久 API 凭证。Binance MCP 文档说明该服务供连接的 AI 客户端在专用 Agentic 子账户内使用，且非读取操作先由用户确认；具体请见 [Binance MCP Server](https://developers.binance.com/en/docs/agent-native/mcp-server/agentic)。

Spot Testnet 使用测试网 REST 和 WebSocket 域名，见 [Spot Testnet WebSocket 文档](https://developers.binance.com/zh-CN/docs/products/spot/testnet/web-socket-streams)。

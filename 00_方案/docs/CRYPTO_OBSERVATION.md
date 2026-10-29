# BTC/ETH 只读观察

本阶段冻结候选规则 SMA 24/72，每小时收盘后计算。快线高于慢线时输出单币 25% 的候选目标权重，否则为 0；不发送订单，不读交易凭证，不改真实账户。观察阶段用于检验数据与程序恢复，尚未完成策略盈利能力验证。

## 本机操作

在 `/home/kkk/Quant` 终端执行：

```bash
.venv/bin/quant crypto watch --once --db runs/crypto-watch-v2.sqlite
.venv/bin/quant crypto watch --db runs/crypto-watch-v2.sqlite
```

第一个命令同步 BTCUSDT、ETHUSDT 的最近已收盘小时数据并退出；第二个命令维持 WebSocket 连接。进程日志为 JSON 行，按 Ctrl+C 停止。在另一个终端查询：

```bash
.venv/bin/quant crypto watch-status --db runs/crypto-watch-v2.sqlite
```

- `data_current`：是否存在最近一个已收盘小时的数据。
- `service_running`：两个 WebSocket 流是否都有 90 秒以内的心跳。一次同步结束后为 false。
- `latest_signal.target_weight`：候选目标仓位，不是当前账户仓位。
- `health.state`：synced、streaming、stopped、reconnecting 或 blocked。

初次下载历史数据会生成历史信号。不能把这些历史信号当作此前已经实时观察过，更不能用回填的日期证明 30 天观察验收。信号与完整的原始输入一起保存，重复处理相同数据不会生成第二条信号；不接受未收盘、非有限数或断小时的 K 线。

## 恢复与停止

重启使用相同命令、相同数据库。启动与断线重连时分页补齐缺失的小时线，数据冲突或补录仍存在缺口时停止并记录 blocked。先检查错误与行情源，保留旧日志；策略、参数或实现改变后换新数据库。连接建立在 REST 同步之前，以覆盖同步期间已经收盘的 WebSocket 事件。

心跳过期时 `service_running` 会变成 false，机器关机/WSL 休眠会中断观察。这一阶段尚未接入外部告警通知；不能把终端进程当作可用的无人值守实盘服务。

## 后续验收

先完成一轮实时观察、重启和断线恢复演练，再持续记录实际运行天数和异常。之后补齐用户订单/余额数据流、订单状态恢复、成交与费用对账、Testnet 故障场景和外部告警。所有流程通过、风险限额显式配置且主机能持续在线后，再单独启用实盘执行。

网站上的 Agentic OAuth 权限、当前聊天可调用的 MCP 工具和本地服务的账户凭证需要分别核验。只读观察无需创建普通子账户 API Key，也无需再充值或划转。

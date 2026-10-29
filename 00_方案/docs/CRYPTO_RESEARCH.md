# Binance Spot 多币种策略研究

此工作流只读取公共行情 CSV 和 Binance 公共市场数据，不读取账户、不连接交易密钥、不发送订单。研究注册表与 `adaptive.py` 的 AI/Testnet 两档白名单隔离；回测通过不代表策略可实盘或可供 AI 选择。

已落盘的本机数据、有效报告路径和离线重跑命令见 [本机研究操作记录](CRYPTO_RESEARCH_LOCAL.md)。

## 1. 建立数据目录与候选币清单

先分页下载候选的已收盘小时线（UTC 起始时间包含、结束时间不包含），命令只使用 Binance 公共 REST 行情，不需要 API Key：

```bash
quant crypto fetch-history \
  --symbols BTCUSDT ETHUSDT SOLUSDT XRPUSDT BNBUSDT NEARUSDT DOGEUSDT TRXUSDT SUIUSDT UNIUSDT LINKUSDT AAVEUSDT LTCUSDT BCHUSDT AVAXUSDT ADAUSDT \
  --start 2025-10-28 --end 2026-10-28 --data-dir data/crypto
```

下载器会拒绝覆盖同名文件，并验证小时连续和日期边界。为比较多个候选，需让它们具有相同的数据区间。生成文件列为 `timestamp,open,high,low,close,volume,quote_volume`，至少覆盖 365 天。筛选会读取当前 Binance Spot 交易状态、最近 90 根已收盘日线的 quote volume、中间价附近当前买卖价差，以及本地小时线历史长度：

```bash
quant crypto universe-screen --data-dir data/crypto \
  --limit 8 --output runs/universe-20261028-v1.json
```

输出只会把符合当前数据门槛的候选写入 `symbols`，不足八个会以退出码 2 明确提示，不会用未达标币填充。检查清单中的 `ranked_candidates`，再把清单作为冻结的回测输入。候选池为 SOL、XRP、BNB、NEAR、DOGE、TRX、SUI、UNI；备选 LINK、AAVE、LTC、BCH、AVAX、ADA。

该筛选器的现货状态与价差是筛选时的快照，不是历史点时数据；要得出不受幸存者偏差影响的结论，仍需另建带下架币和历史上市状态的点时数据集。

## 2. 运行多币、多策略、多周期回测

```bash
quant crypto strategy-backtest \
  --data-dir data/crypto \
  --universe-file runs/universe-20261028-v1.json \
  --timeframes 1h,4h,1d \
  --output runs/strategy-research-20261028-v1.json
```

每个小时 CSV 必须连续、无重复、时间戳整点对齐；所有币会按各周期 UTC 边界聚合，并只保留所有币都有完整收盘 K 线的时间点。最少需要 365 天。信号使用已收盘 K 线，成交模拟发生在下一根 K 线，并加入限价可成交条件、固定每单上限、手续费、滑点、IOC 未成交和部分成交假设。

研究档位包括空仓、等权持有、SMA/EMA 交叉、Donchian 突破、带趋势过滤的布林带/RSI、横截面动量和逆波动率权重。每档分别报告基准成本与压力成本下的净值、最大回撤、Sharpe、Sortino、阶段结果、费用、换手、成交情况和仓位暴露；报告保存冻结币种名单、数据范围、CSV SHA256 和假设。另有空仓、等权组合、BTC 单币持有和 ETH 单币持有基准；BTC/ETH 基准缺少本地 CSV 时会明确标记不可用。默认基准成本是手续费 0.1%、滑点 0.05%、IOC 未成交 10%、部分成交 5%；压力成本是 0.15%、0.1%、20%、10%。仓位限定为单币 25%、组合 50%，每单默认 25 USDT，每日亏损锁默认 10 USDT；触发后只减仓，锁定持续到回测结束。

网格在报告中明确标记为未评分，需先实现独立的挂单队列、库存、资金占用和订单簿成交模型。回测不是交易所真实成交模拟，也没有自动审批策略。

IOC 只尝试下一根 K 线开盘价加滑点，之后盘中高低价不能补救已经失效的 IOC。等权持有固定初始目标份额，按单笔限额分批建仓，不做周期再平衡。同一周期内所有档位和 BTC/ETH 对照使用共同预热窗口，报告保留实际评估起止时间；不同周期的预热后样本日期可能不同。亏损检查发生在该周期收盘时，日线研究无法代表盘中止损能力。

Testnet 每日亏损锁会跨重启保留。触发后继续处理小时收盘信号，但目标仓位被限制为当前持仓或更低，只会允许卖出/减仓；其他锁原因仍停止全部订单。人工核对账户与挂单、停止相关服务后，可使用独立的 `--confirm-daily-loss-review I_REVIEWED_DAILY_LOSS_AND_TESTNET_ACCOUNT` 复核命令解除该锁并重设当日基准。该命令在有挂单或无法读取账户时拒绝解锁。

## 3. 研究闸门

报告把序列按时间拆成 60% 研究、20% 验证、20% 最终保留区间，后两段分别列出表现；先冻结策略与参数，再查看 holdout 指标，避免根据 holdout 反复调参。BTC/ETH 基准同样套用现货单币 25% 上限。逐个比较空仓、BTC/ETH 基准和等权持有；记录所有候选策略和失败结果。当前筛选器使用当下交易状态与流动性快照，无法还原过去时点下架/上市币池，因此尚不能消除幸存者偏差；在声称无幸存者偏差前，需接入并冻结历史点时上市/交易状态数据。只有在多个阶段、压力成本和成交情景下表现可解释且经人工审核的策略，才可另行提出 AI 白名单变更。此实现不会更改现有 AI 白名单或 Testnet 交易对。

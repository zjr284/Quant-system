# 本机研究操作记录

本地目录为 `/home/kkk/Quant`，使用 `.venv`。此批次读取冻结 CSV、币池与公开市场快照，离线计算历史模拟结果。

## 已冻结的输入

- 数据目录：`data/crypto-research-20261028`。
- 数据清单：目录内 `dataset-manifest-v1.json`，含 16 个币各 8,775 根小时线、SHA256、时间边界与来源。
- 小时线范围：2025-10-28 00:00 UTC 至 2026-10-28 15:00 UTC，结束边界不包含。
- 筛选快照：目录内 `market-snapshot-20261029-v1.json`，保存 14 个候选币的现货状态、90 根收盘日成交额、即时买卖报价。
- 冻结币池：`runs/universe-20261029-local-v1.json`。
- 研究币：SOL、XRP、BNB、NEAR、DOGE、SUI、UNI、ADA；BTC/ETH 为对照。

本机直连 REST 在此环境受限，这批候选小时线与筛选快照通过只读 Binance MCP 获取，再由本地程序计算；BTC/ETH 沿用已有本地历史 CSV，原始来源未在此批次独立重验。

## 查看有效结果

打开 `runs/strategy-research-20261029-local-v2-summary.md` 可看中文表格；同名前缀 CSV 可用表格软件打开。原始结果在 `runs/strategy-research-20261029-local-v2.json`。

v1 已作废，原因见 `runs/strategy-research-20261029-local-v1-WITHDRAWN.md`；不能用它挑选策略。

v2 已完成 66 组策略情景及 6 组 BTC/ETH 对照，核验记录在 `runs/strategy-research-20261029-local-v2-validation.json`，对应实现源码已单独冻结留存。本轮未批准新策略：小时、4 小时主动策略在基准成本下均为负收益；日线布林带全段约 +3.62%，最后 20% 时间段约 −1.66%。日线等权持有约 +17.83%，最后一段约 −9.60%，且与小时/4 小时的实际评估日期不同，不能据此断言日线周期更优。

## 用相同输入重跑

在本机 WSL 终端执行；更换输出版本号，保留每次研究记录：

```bash
cd /home/kkk/Quant
.venv/bin/quant crypto strategy-backtest \
  --data-dir data/crypto-research-20261028 \
  --universe-file runs/universe-20261029-local-v1.json \
  --timeframes 1h,4h,1d \
  --initial-cash 1000 \
  --max-order-quote 25 \
  --max-daily-loss-quote 10 \
  --output runs/strategy-research-local-v3.json
```

生成可读汇总：

```bash
.venv/bin/python scripts/summarize_crypto_research.py \
  --report runs/strategy-research-local-v3.json \
  --output-prefix runs/strategy-research-local-v3-summary
```

这两步不需要网络或交易密钥。完整计算可能需要数分钟，CLI 在完成后统一输出；关闭本机或中断进程会停止这次计算。

## 重新生成同一筛选快照的币池

```bash
.venv/bin/quant crypto universe-screen \
  --data-dir data/crypto-research-20261028 \
  --market-snapshot data/crypto-research-20261028/market-snapshot-20261029-v1.json \
  --limit 8 \
  --output runs/universe-local-v2.json
```

此命令重放已保存快照。若需要更新当下市场筛选，应另行采集并保存新的快照和数据版本。

## 本轮实现修正与检查

修正布林带退出、下一开盘 IOC 判断、部分成交比例、末根估值、买入持有误做再平衡、各周期基准日期、共同预热窗口，以及日线亏损锁基准。研究模块的 9 项检查覆盖此次核心错误与既有数据下载/筛选流程。

同一周期所有策略与基准使用共同预热窗口：小时线 720 根、4 小时线 200 根、日线 200 根。日线预热后仅剩约 165 天可评估；不同周期的实际评估日期不同。

当前结果仍属于探索比较：币池在当前时点冻结，会有选币前视与幸存者偏差；时间分段继承持仓和亏损锁，不是独立重启或完整滚动样本外验证。亏损锁只在所选周期收盘时检查；它阻止新增风险并允许策略减仓，不保证将最大回撤限制为 10 USDT。后续应补长历史、历史时点币池和滚动验证，再决定哪些档位进入人工审核。

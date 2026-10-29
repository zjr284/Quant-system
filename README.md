# Binance 现货自动盯盘与 Testnet 交易系统

本项目聚焦 Binance Spot 的 BTCUSDT、ETHUSDT 小时级行情观察、研究回测、AI 档位选择和 Testnet 验收。AI 只给出白名单策略建议，程序风控负责是否允许订单。当前没有可用的 Binance 主网自动下单适配器；Testnet 结果不代表实盘能力或收益。

## 项目目录

| 目录 | 内容 |
|---|---|
| `00_方案/` | 设计文档、数据契约、Hypothesis First 模板 |
| `01_数据提取/` | BTC/ETH 与研究候选币小时线、数据清单 |
| `02_增强处理/` | 特征和市场数据实现索引 |
| `03_回测引擎/` | 回测实现索引 |
| `04_策略研究/` | 策略、筛选与研究实现索引 |
| `05_审核记录/` | 审核、验收及决策记录 |
| `06_运维脚本/` | 腾讯云 systemd 配置、部署与维护脚本 |
| `07_配置参数/` | 无密钥配置模板 |
| `99_结果/` | 回测、研究、运行记录与部署归档 |
| `quant/crypto/` | 实际 Python 应用代码 |
| `tests/` | Crypto 系统测试 |

原有命令使用的 `data/`、`docs/`、`deploy/`、`dist/`、`runs/`、`scripts/` 路径保留为兼容入口，指向编号目录。ETF/多资产研究模块和数据已从项目中移除。

此运行环境将 `.git/` 保留为只读挂载点，项目快照使用根目录的 `./gitw` 包装命令和被忽略的 `.git-snapshot/` 元数据。修改后先用 `./gitw status` 检查，再 `./gitw add ...`、`./gitw commit -m "..."` 留快照；仓库尚未连接远程时，提交仍保存在本机。

## 本地安装

```bash
cd /home/kkk/Quant
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[crypto,dev]'
.venv/bin/quant crypto --help
```

## 获取行情与跑档位回测

开始下载、清洗、冻结行情前，先按 [`数据自检 SOP`](01_数据提取/数据自检SOP.md) 执行来源记录、CSV 自检、SHA256 冻结和异常处理。可用 `quant crypto data-check --help` 查看数据检查命令。

```bash
.venv/bin/quant crypto fetch-history \
  --symbols BTCUSDT ETHUSDT \
  --start 2025-10-01 --end 2026-10-01 \
  --data-dir 01_数据提取/data/crypto
```

需要先准备 BTC、ETH 两份 CSV 后运行：

```bash
.venv/bin/quant crypto adaptive-backtest \
  --btc-csv 01_数据提取/data/crypto/BTCUSDT-1h.csv \
  --eth-csv 01_数据提取/data/crypto/ETHUSDT-1h.csv \
  --output 99_结果/runs/adaptive-backtest-v1.json
```

研究数据、策略和执行假设的限制见 [`CRYPTO_RESEARCH.md`](00_方案/docs/CRYPTO_RESEARCH.md) 与 [`CRYPTO_RESEARCH_LOCAL.md`](00_方案/docs/CRYPTO_RESEARCH_LOCAL.md)。候选策略尚未因此自动获准进入 AI 或交易白名单。

## 观察、AI 与 Testnet

- 行情观察操作见 [`CRYPTO_OBSERVATION.md`](00_方案/docs/CRYPTO_OBSERVATION.md)。
- AI 策略选择和影子运行见 [`CRYPTO_ADAPTIVE.md`](00_方案/docs/CRYPTO_ADAPTIVE.md)。
- Testnet 与腾讯云部署见 [`TENCENT_TESTNET.md`](00_方案/docs/TENCENT_TESTNET.md)。
- 服务器凭证只放受限权限的环境文件；不要把 API Key、Secret 或 AI 密钥提交到本机项目或聊天中。
- 主网 API 密钥不得用于 Testnet；现阶段不要把观察、历史回测或 Testnet 结果当作实盘验收。

数据更新、策略新增、回测产物都应使用新版本路径并保留输入数据、配置和代码摘要。先填写 [`Hypothesis First 模板`](00_方案/HYPOTHESIS_FIRST_TEMPLATE.md)，再开展新策略研究。

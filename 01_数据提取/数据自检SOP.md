# 数据自检 SOP

版本：1.0　适用系统：Quant-system Binance Spot　数据周期：UTC 1 小时 K 线

## 1. 目的与适用范围

本 SOP 规定公共行情 CSV、运行时行情账本与研究输入的检查、冻结、复核和异常处置流程。覆盖 BTCUSDT、ETHUSDT，以及 `quant.crypto.research` 中登记的研究候选币。

数据自检分成三层，三层结论不能互相替代：

1. **文件完整性**：字段、数值、时间顺序、缺失小时、币种间对齐、覆盖长度、时效和 SHA256。
2. **来源与正确性**：来源声明、下载日志及抽样与 Binance 公共 REST 回查。SHA256 只能证明文件之后没有改变，不能证明源头真实或行情正确。
3. **运行账本健康**：SQLite 完整性、最新小时、连续观察记录、AI 评估缺失/失败及订单/账户对账状态。

本流程只检验数据和账本，不判断策略是否盈利，也不构成 Testnet 或实盘准入。任何关键检查不通过，停止使用该批数据；禁止插值、复制前值、删行或手工修改 CSV 来“修复”后继续研究。

## 2. 职责与执行时机

- **每次采集后、首次回测前**：操作者执行第 3 至第 6 节，生成唯一版本的检查报告。
- **每次更换数据、补历史或改字段后**：作为新批次重新检查；禁止覆盖冻结数据和旧报告。
- **持续盯盘期间**：每个值守日按第 7 节检查状态；服务报告缺小时、过期或冲突时立即按第 9 节处置。
- **启用/恢复 Testnet 前**：完成第 7 至第 8 节，并做交易所账户、挂单和成交对账。仅行情检查通过不能解除风控锁。
- **备份与归档时**：记录数据和报告哈希，SQLite 使用在线备份脚本，不直接复制运行中的 `.sqlite` 文件。

## 3. 采集前检查

1. 明确市场为 Binance Spot、标的、周期 `1h`、时区 UTC、起止范围。历史区间采用左闭右开 `[start, end)`；只纳入已收盘 K 线。
2. 记录操作者、采集日期、来源与方法、代码版本、币种列表、开始/结束时间、预期行数。对 Binance 下载器，来源标为 `Binance Spot public REST /api/v3/klines`；手动或第三方数据必须如实标注。
3. 为每次数据采集新建目录，例如 `01_数据提取/data/btc-eth-1h-20261029-v1/`。下载器拒绝覆盖已有同名文件。需要重抓时新建 `v2` 目录并保留旧批次及失败记录。
4. 首次一年级别策略研究通常要求至少 365 天；实时观察只要求最新数据完整及时，按命令显式设 `--min-days 0`。

示例：下载器将自己分页抓取并检查开始、结束边界和小时连续性；不需要 API Key：

```bash
.venv/bin/quant crypto fetch-history \
  --symbols BTCUSDT ETHUSDT \
  --start 2025-10-28T00:00:00Z --end 2026-10-29T00:00:00Z \
  --data-dir 01_数据提取/data/btc-eth-1h-20261029-v1
```

若网络超时、HTTP 403/418/429/451、返回空页或下载器报错，记录时间、错误类型和符号；不要切换到未登记来源后仍把数据称为 Binance 数据。确认来源限制后再决定稍后重试或使用可审计的替代源。

## 4. CSV 自动自检

项目新增的 `crypto data-check` 会检查：必需列 `timestamp,open,high,low,close,volume`；时间戳为 UTC 小时边界；K 线已收盘；数值有限且价格为正；OHLC 高低关系成立；成交量非负；时间升序、无重复且小时连续；各币时间戳完全对齐；覆盖长度和（实时模式）最新数据时效。它记录每个输入文件的行数、起止时间和 SHA256。零成交量是警告，需要复核，不会自动判错。

### 4.1 历史研究数据（默认严格对齐、至少 365 天）

```bash
.venv/bin/quant crypto data-check \
  --data-dir 01_数据提取/data/btc-eth-1h-20261029-v1 \
  --symbols BTCUSDT ETHUSDT \
  --purpose research --min-days 365 \
  --source "Binance Spot public REST /api/v3/klines" \
  --output 99_结果/runs/data-check-btc-eth-20261029-v1.json
```

### 4.2 实时数据新鲜度检查

```bash
.venv/bin/quant crypto data-check \
  --data-dir 01_数据提取/data/live-snapshot \
  --symbols BTCUSDT ETHUSDT \
  --purpose live --min-days 0 --max-age-hours 2 \
  --source "Binance Spot public REST /api/v3/klines" \
  --output 99_结果/runs/data-check-live-20261029-v1.json
```

实时模式检查最新已收盘 K 线不超过指定年龄；这只是 CSV 快照时效检查，不能代替 WebSocket 心跳和服务状态。超过两小时即失败，除非系统配置与 SOP 经过审查后另行变更。不要为了通过检查随意放宽时限。

退出码 `0` 且报告 `passed: true` 才可进入下一步；退出码 `2` 或 `passed: false` 表示拒绝使用。失败报告也要保留，用新文件名重跑。报告里的 `source_authenticity_verified: false` 是刻意设置：来源标签是操作者声明，程序不会冒称已验证交易所来源。报告 `symbols` 中的 SHA256 是行情文件冻结依据。

> 报告输出路径必须是新路径；程序拒绝覆盖。报告含输入文件名、日期、哈希和错误信息，不含账户密钥；仍需按项目结果目录权限保存。

## 5. 来源抽查与冻结

1. 若数据由项目 `fetch-history` 下载，留存命令、终端回执、数据批次目录和 `data-check` 报告。下载器使用公开 Spot K 线 REST，并校验小时连续及所请求的边界。
2. 若 CSV 来自手工下载或其他供应商，对每个标的至少抽查 5 个时点：区间首部、中部、尾部和两个随机时点。用 Binance 公共端点 `GET /api/v3/klines` 对照对应小时的 OHLC、base volume；记录抽查时点、请求结果、差异和来源限制。若无法访问 Binance 或供应商字段定义不同，标记“来源抽查未完成”，不得把格式校验通过写成来源已验证。
3. 比较原始十进制值或按供应商精度比较，解释任何不一致；不能静默容差、替换字段或混合供应商行。
4. 检查报告为通过后，冻结该目录。记录报告路径、CSV SHA256、清单 SHA256、抽查记录、币池文件哈希和研究代码提交。开始回测前再次执行：

   ```bash
   sha256sum 01_数据提取/data/btc-eth-1h-20261029-v1/*.csv
   sha256sum 99_结果/runs/data-check-btc-eth-20261029-v1.json
   ```

   文件哈希必须与清单一致。任何变化都要作为新批次重新检查，不能沿用旧报告。

## 6. 研究/回测放行检查

按顺序完成并归档证据：

1. `data-check` 全部通过，标的时间戳严格对齐，覆盖期满足研究需求。
2. 复核币种列表和时间范围。研究池筛选使用当前交易状态/流动性快照，不代表历史时点可交易池；幸存者偏差仍需在结论里披露。
3. 对研究输入运行 `universe-screen` 或复核冻结的币池文件，检查标的在允许名单内；不够数量时保留不足结果，不补入未达标币。
4. 输出使用全新版本路径。回测报告应包含输入 CSV SHA256、币池哈希、代码哈希、手续费、滑点、IOC 未成交/部分成交假设、时间切分及限制。
5. 检查报告中的 `start_ms/end_ms`、每币行数、共同评估区间和基准可用性。数据区间不一致、基准缺失、费用/成交假设不合理或关键字段缺失时暂停解释结果。
6. 不把指标或成功下载当成收益证明；回测通过也不等于 AI 能正确择档、Testnet 已验收或实盘获准。

BTC/ETH 白名单档位回测示例：

```bash
.venv/bin/quant crypto adaptive-backtest \
  --btc-csv 01_数据提取/data/btc-eth-1h-20261029-v1/BTCUSDT-1h.csv \
  --eth-csv 01_数据提取/data/btc-eth-1h-20261029-v1/ETHUSDT-1h.csv \
  --output 99_结果/runs/adaptive-backtest-20261029-v1.json
```

## 7. 服务器盯盘数据与 SQLite 自检

先查看服务，再查应用数据。示例适用于对应服务已安装的服务器；只对当前启用的模式执行对应命令。

```bash
sudo systemctl status quant-observer --no-pager
sudo -u quant-observer /opt/quant-venv/bin/quant crypto watch-status \
  --db /var/lib/quant-observer/observation.sqlite
```

观察模式要求 BTCUSDT、ETHUSDT `data_current: true`，`service_running: true`，最新信号时间一致且健康状态不是 `blocked`/`reconnecting`。历史回填仅填补数据，不计入实时观察天数。

AI 影子/自适应 Testnet 部署使用：

```bash
sudo systemctl status quant-adaptive-shadow --no-pager
sudo -u quant-adaptive /opt/quant-venv/bin/quant crypto adaptive-status \
  --db /var/lib/quant-adaptive/adaptive.sqlite
```

Testnet 执行模式还需：

```bash
sudo systemctl status quant-adaptive-testnet --no-pager
sudo -u quant-adaptive /opt/quant-venv/bin/quant crypto testnet-status \
  --db /var/lib/quant-adaptive/testnet.sqlite
```

执行以下只读 SQL 检查 SQLite 文件完整性和观察账本是否有缺小时。将数据库文件路径替换为实际启用的路径；在服务用户权限下运行：

```bash
sudo -u quant-observer python3 -c 'import sqlite3; p="file:/var/lib/quant-observer/observation.sqlite?mode=ro"; c=sqlite3.connect(p,uri=True); print(c.execute("PRAGMA integrity_check").fetchone()[0]); print(c.execute("SELECT symbol,COUNT(*) AS n,MIN(ts),MAX(ts),(MAX(ts)-MIN(ts))/3600000+1 AS expected FROM observer_candles GROUP BY symbol").fetchall())'
```

- `integrity_check` 必须返回 `ok`。
- 每个币种的 `n` 应等于 `expected`；否则有缺失小时。再确认 BTC 与 ETH 的 `MIN(ts)`、`MAX(ts)` 一致。
- adaptive SQLite 使用 `adaptive-status` 检查 `accepted_hours`、`observation_span_hours`、`missing_or_failed_hours`。30 天门槛需至少 720 个连续有效小时，且缺失/失败为零；服务事件和时间范围应可解释。
- Testnet SQLite 状态仍要与 Binance Testnet 账户、挂单、成交与费用核对。任何本地账本与交易所差异都停止新订单，不能直接改数据库消账。

若数据库完整性检查失败或状态不一致：先停止相应自动执行服务、保留日志和数据库文件，再做只读备份与排查。SQLite 在线备份使用项目脚本（目标文件必须是新路径）：

```bash
sudo /opt/quant-venv/bin/python /opt/quant/scripts/backup_crypto_observer.py \
  --db /var/lib/quant-observer/observation.sqlite \
  --output /var/backups/quant/observation-20261029-v1.sqlite
```

备份脚本用 SQLite backup API 包含已提交的 WAL 内容，并运行完整性检查。不要只复制正在运行的 `.sqlite` 主文件，也不要把服务器凭证或数据库上传到 GitHub。

## 8. 每日记录与审计归档

每次检查在 `99_结果/runs/` 生成新报告，不覆盖旧结果。审计记录至少包含：

- 操作者、检查时间（UTC）、执行机器/环境、代码提交/版本。
- 数据来源声明及实际采集方法、市场、币种、周期、起止范围、单位/时区。
- 输入行数、缺失/重复/异常统计、标的对齐状态、最新 K 线年龄、每个文件 SHA256。
- 抽样回查记录、检查报告路径与 SHA256、放行/拒绝结论、复核者。
- 若用于回测：币池及其哈希、费用/滑点/成交假设、结果文件路径。
- 若用于观察/交易：数据库健康结果、服务状态、缺失/失败小时、交易所对账人及结论。

行情 CSV、SQLite、AI/交易凭证和输出包默认不提交 Git。可将脱敏后的流程变更和模板提交 Git；数据审计产物留在受权限控制的本机/服务器目录，并按组织备份策略加密备份。

## 9. 异常分级与处置

| 级别 | 示例 | 必须动作 |
|---|---|---|
| P0 阻断 | 重复/乱序/缺小时、未收盘 K 线、未来时间、非法 OHLC、非有限值、两币错位、来源抽查出现无法解释的差异 | 该数据批次标记失败；不跑回测、不生成信号验收；保留输入/日志，查明源头后新建批次重采。运行中出现时停止新单。 |
| P1 严重 | 实时数据超过年龄门槛、REST/WebSocket 不一致、连续小时记录停顿、SQLite 非 `ok`、AI 小时缺失/失败、交易所对账差异 | 观察/交易服务按对应故障流程停机或保持锁定；保留 SQLite/WAL、系统日志、响应状态与时间；复核账户挂单/成交。未解释前不得重启交易或解锁。 |
| P2 复核 | 零成交量、异常大涨跌、短于预期覆盖、研究候选未达到历史长度、非关键基准缺失 | 记录为 warning；人工抽查源和市场事件。若影响研究假设，拒绝该轮研究或缩小结论，不静默删除。 |
| P3 文档 | 命名/记录字段遗漏但原始输入可追溯 | 补充审计记录，保留原报告；若来源、区间或哈希无法补证，升级为 P0。 |

任何故障恢复都应记录“发现时间—影响范围—停止动作—证据副本—根因—修复版本—重新自检结果—复核者”。策略服务显示 `failed_closed`、`locked` 或未知错误时，不把“进程重启成功”当作数据恢复通过。

## 10. 放行签署模板

```text
数据批次：
来源声明 / 采集命令：
市场 / 周期 / 币种：
UTC 区间 / 行数：
data-check 报告：
各文件 SHA256：
完整性检查：PASS / FAIL
跨币时间戳对齐：PASS / FAIL / N/A
来源抽查：PASS / FAIL / 未完成（注明原因）
实时新鲜度（如适用）：PASS / FAIL / N/A
运行 SQLite / 交易所对账（如适用）：PASS / FAIL / N/A
异常及处理：
用途：观察 / 回测 / Testnet / 其他
操作者 / UTC 时间：
复核者 / UTC 时间：
最终决定：放行 / 拒绝
```

所有必需项通过且没有未解释异常才可放行。当前策略白名单、风控门槛和市场范围不会因本 SOP 而自动改变。

# 策略研究

策略实现集中在根目录 Python 包，避免维护重复副本：

- 加密货币信号规则：`../quant/crypto/strategy.py`
- 策略档位与研究评估：`../quant/crypto/research.py`
- AI 受限档位选择：`../quant/crypto/adaptive.py`

研究结果和版本证据见 `../99_结果/runs/`，尚未通过审核的策略不得加入自动选择白名单。

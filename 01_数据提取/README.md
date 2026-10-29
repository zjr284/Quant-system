# 数据提取

`data/crypto/` 与 `data/crypto-research-20261028/` 保存加密货币小时 K 线与研究快照。采集入口在 `../06_运维脚本/scripts/` 和 `../quant/crypto/market.py`、`research.py`。更新后应生成数据清单和校验哈希，不能覆盖已用于研究的冻结快照。

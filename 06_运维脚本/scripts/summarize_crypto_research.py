"""Render an existing offline strategy report as CSV and a readable Markdown table."""
from __future__ import annotations

import argparse
import csv
import json
from datetime import datetime, timezone
from pathlib import Path


def stamp(value):
    return datetime.fromtimestamp(value / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def summarize(report_path, output_prefix):
    report = json.loads(Path(report_path).read_text(encoding="utf-8"))
    if report.get("execution_enabled") is not False:
        raise ValueError("Expected a research-only report")
    prefix = Path(output_prefix)
    csv_path, md_path = Path(str(prefix) + ".csv"), Path(str(prefix) + ".md")
    if csv_path.exists() or md_path.exists():
        raise ValueError("Summary output exists; choose a new versioned prefix")
    prefix.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    lines = ["# 本地多币种策略研究结果", "", f"输入报告：`{report_path}`", "",
             "本报告为固定参数的探索性历史比较，未批准任何策略进入 AI 或交易白名单。", "",
             f"研究池：{', '.join(report['universe'])}。BTC/ETH 为单币 25% 仓位上限对照。", "",
             f"小时线范围：{stamp(report['data']['start_ms'])} 至 {stamp(report['data']['end_ms'])}（最后一根开盘时间）。", "",
             "初始资金 1,000 USDT，每单上限 25 USDT，单日亏损锁 10 USDT；触发后持续禁止新买入，允许减仓。", "",
             "基准成本：手续费 0.1%、滑点 0.05%、未成交率 10%、部分成交率 5%。", "",
             "压力成本：手续费 0.15%、滑点 0.1%、未成交率 20%、部分成交率 10%。部分成交比例均为 50%。", ""]
    for timeframe, profiles in report["results"].items():
        sample = next(iter(profiles.values()))["base"]
        lines += [f"## {timeframe}", "",
                  f"共同评估区间：{stamp(sample['evaluation_start_ms'])} 至 {stamp(sample['evaluation_end_ms'])}；预热 {sample['warmup_bars']} 根。", "",
                  "| 策略 | 基准净收益 | 压力净收益 | 基准最大回撤 | Sharpe | 后段净收益 | 基准亏损锁 | 成交笔数 |", "|---|---:|---:|---:|---:|---:|:---:|---:|"]
        for name, profile in profiles.items():
            base, stress = profile["base"], profile["stress"]
            holdout = base["stages"][-1]
            rows.append({"timeframe": timeframe, "profile": name,
                         "base_return": base["return"], "stress_return": stress["return"],
                         "base_max_drawdown": base["max_drawdown"], "base_sharpe": base["sharpe"],
                         "base_holdout_return": holdout["return"],
                         "base_daily_loss_locked": base["stopped_on_daily_loss"],
                         "stress_daily_loss_locked": stress["stopped_on_daily_loss"],
                         "base_fills": base["orders_filled"], "base_fees_quote": base["fees_quote"],
                         "base_average_exposure": base["average_exposure"]})
            lines.append(f"| {name} | {base['return']:.2%} | {stress['return']:.2%} | {base['max_drawdown']:.2%} | {base['sharpe']:.2f} | {holdout['return']:.2%} | {'是' if base['stopped_on_daily_loss'] else '否'} | {base['orders_filled']} |")
        lines += ["", "BTC/ETH 对照（基准成本）：", ""]
        for name, base in report["benchmarks"]["btc_eth"][timeframe].items():
            if base.get("available"):
                lines.append(f"- {name}：净收益 {base['return']:.2%}，最大回撤 {base['max_drawdown']:.2%}，亏损锁 {'已触发' if base['stopped_on_daily_loss'] else '未触发'}。")
        lines.append("")
    lines += ["## 解释范围", "",
              "- 币池由当前 90 日成交额与交易状态冻结，再用于过去行情，存在选币前视和幸存者偏差；不能视为真实历史滚动筛币结果。", 
              "- 三个周期预热长度不同，实际评估日期不同；同一周期内策略和基准使用共同日期。", 
              "- 60/20/20 是同一路径的时间分段，后段继承先前持仓和亏损锁；这不是独立重启回测，也不是完整滚动样本外验证。", 
              "- 日线只有约一年输入，200 日预热后样本偏短；年化 Sharpe 仅供描述。", 
              "- IOC 仅按下一根开盘价加滑点尝试，无法还原历史盘口、真实排队、交易所逐币过滤和停牌事件。", 
              "- 仓位限制约束目标和买入；行情变化仍会造成持仓比例漂移。", 
              "- 网格未评分，AI 选择效果未在此回测中评估。", ""]
    with csv_path.open("x", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    md_path.write_text("\n".join(lines), encoding="utf-8")
    return {"csv": str(csv_path), "markdown": str(md_path), "comparisons": len(rows)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", required=True)
    parser.add_argument("--output-prefix", required=True)
    args = parser.parse_args()
    print(json.dumps(summarize(args.report, args.output_prefix), ensure_ascii=False))

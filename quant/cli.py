from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path


def emit(value):
    print(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False))


def parser():
    p = argparse.ArgumentParser(description="Binance 现货加密货币研究、监控与 Testnet")
    commands = p.add_subparsers(dest="command", required=True)
    c = commands.add_parser("crypto", help="加密货币现货研究与模拟；不提供实盘下单")
    cc = c.add_subparsers(dest="crypto_action", required=True)
    q = cc.add_parser("fetch", help="下载 Binance 已收盘小时 K 线")
    q.add_argument("--symbol", default="BTCUSDT"); q.add_argument("--limit", type=int, default=1000)
    q.add_argument("--output", default="data/crypto/BTCUSDT-1h.csv")
    for action in ("backtest", "paper"):
        q = cc.add_parser(action)
        q.add_argument("--csv", required=True); q.add_argument("--symbol", default="BTCUSDT")
        q.add_argument("--db", default=f"runs/crypto-{action}.sqlite")
        q.add_argument("--initial-cash", type=float, default=1000); q.add_argument("--fast", type=int, default=24)
        q.add_argument("--slow", type=int, default=72); q.add_argument("--fee-rate", type=float, default=0.001)
        q.add_argument("--slippage", type=float, default=0.0005); q.add_argument("--max-order-quote", type=float, default=250)
        q.add_argument("--max-position-weight", type=float, default=0.25); q.add_argument("--max-daily-loss", type=float, default=0.03)
        q.add_argument("--max-price-deviation", type=float, default=0.02)
    q = cc.add_parser("status"); q.add_argument("--db", default="runs/crypto-paper.sqlite"); q.add_argument("--price", type=float, default=0)
    q = cc.add_parser("halt"); q.add_argument("--db", default="runs/crypto-paper.sqlite"); q.add_argument("--reason", default="manual_stop")
    cc.add_parser("account", help="通过 Binance 官方 SDK 只读查询现货账户余额")
    q = cc.add_parser("observe", help="回放 BTC/ETH 收盘小时信号；只记录信号，不模拟或真实下单")
    q.add_argument("--btc-csv", required=True); q.add_argument("--eth-csv", required=True)
    q.add_argument("--db", default="runs/crypto-observe.sqlite"); q.add_argument("--fast", type=int, default=24); q.add_argument("--slow", type=int, default=72)
    q = cc.add_parser("watch", help="持续监听 BTC/ETH 收盘小时线并记录信号；不下单")
    q.add_argument("--db", default="runs/crypto-watch.sqlite"); q.add_argument("--fast", type=int, default=24); q.add_argument("--slow", type=int, default=72)
    q.add_argument("--once", action="store_true", help="仅同步历史已收盘小时线与信号，然后退出")
    q = cc.add_parser("watch-status", help="只读查看盯盘状态、最新信号及数据是否过期")
    q.add_argument("--db", default="runs/crypto-watch.sqlite")
    q = cc.add_parser("testnet-run", help="仅连接 Binance Spot Testnet，按小时策略自动下单")
    q.add_argument("--db", default="/var/lib/quant-testnet/trading.sqlite")
    q.add_argument("--fast", type=int, default=24); q.add_argument("--slow", type=int, default=72)
    q.add_argument("--max-order-quote", type=float, default=25)
    q.add_argument("--max-daily-loss-quote", type=float, default=10)
    q.add_argument("--max-price-deviation", type=float, default=0.005)
    q.add_argument("--enable-testnet", action="store_true")
    q.add_argument("--confirm-testnet-orders", default="")
    q.add_argument("--once", action="store_true", help="只初始化历史行情，不启动订单循环")
    q = cc.add_parser("testnet-status", help="只读查看本地 Testnet 订单日志状态")
    q.add_argument("--db", default="/var/lib/quant-testnet/trading.sqlite")
    q = cc.add_parser("testnet-unlock", help="仅在人工复核账户与挂单后解除指定 Testnet 锁")
    q.add_argument("--db", default="/var/lib/quant-adaptive/testnet.sqlite")
    q.add_argument("--confirm-manual-reset", default="")
    q.add_argument("--confirm-daily-loss-review", default="")
    q = cc.add_parser("adaptive-run", help="AI 策略选择；默认只读影子观察")
    q.add_argument("--mode", choices=["shadow", "testnet_auto"], default="shadow")
    q.add_argument("--db", default="/var/lib/quant-adaptive/adaptive.sqlite")
    q.add_argument("--trading-db", default="/var/lib/quant-adaptive/testnet.sqlite")
    q.add_argument("--acceptance-file", default="")
    q.add_argument("--max-order-quote", type=float, default=25)
    q.add_argument("--max-daily-loss-quote", type=float, default=10)
    q.add_argument("--max-price-deviation", type=float, default=0.005)
    q.add_argument("--enable-testnet", action="store_true")
    q.add_argument("--confirm-testnet-orders", default="")
    q = cc.add_parser("adaptive-status", help="只读查看 AI 策略选择账本")
    q.add_argument("--db", default="/var/lib/quant-adaptive/adaptive.sqlite")
    q = cc.add_parser("adaptive-backtest", help="含费用、滑点与 IOC 未成交假设的白名单档位历史比较")
    q.add_argument("--btc-csv", required=True); q.add_argument("--eth-csv", required=True)
    q.add_argument("--initial-cash", type=float, default=1000)
    q.add_argument("--fee-rate", type=float, default=0.001)
    q.add_argument("--slippage", type=float, default=0.0005)
    q.add_argument("--max-order-quote", type=float, default=25)
    q.add_argument("--max-daily-loss-quote", type=float, default=10)
    q.add_argument("--ioc-nonfill-rate", type=float, default=0.1)
    q.add_argument("--partial-fill-rate", type=float, default=0.05)
    q.add_argument("--partial-fill-fraction", type=float, default=0.5)
    q.add_argument("--output", default="runs/crypto-adaptive-backtest.json")
    q = cc.add_parser("universe-screen", help="按现货状态、90日成交额、价差与本地一年小时线筛币（只读）")
    q.add_argument("--data-dir", default="data/crypto")
    q.add_argument("--limit", type=int, default=8)
    q.add_argument("--output", default="runs/crypto-universe.json")
    q.add_argument("--market-snapshot", help="可选：由受信公开行情接口生成的状态/日成交额/盘口快照 JSON")
    q = cc.add_parser("fetch-history", help="分页下载候选币与 BTC/ETH 的 Binance 公共小时线历史")
    q.add_argument("--symbols", nargs="+", required=True)
    q.add_argument("--start", required=True, help="UTC 日期或小时，例如 2025-10-28")
    q.add_argument("--end", required=True, help="UTC 右开区间日期/小时")
    q.add_argument("--data-dir", default="data/crypto")
    q = cc.add_parser("data-check", help="检查小时 CSV 完整性、对齐、时效并生成 SHA256 清单")
    q.add_argument("--data-dir", default="01_数据提取/data/crypto")
    q.add_argument("--symbols", nargs="+", required=True, help="CSV 文件名前缀，例如 BTCUSDT ETHUSDT")
    q.add_argument("--purpose", choices=["research", "live"], default="research")
    q.add_argument("--source", required=True, help="数据来源声明，会写入检查清单；不会验证来源真实性")
    q.add_argument("--min-days", type=float, default=365)
    q.add_argument("--max-age-hours", type=float, default=2,
                    help="live 模式最新已收盘 K 线最大年龄；research 模式忽略")
    q.add_argument("--no-require-exact-alignment", action="store_true",
                    help="多币回测前不建议关闭；关闭后只逐文件检查")
    q.add_argument("--output", default="99_结果/runs/data-check-v1.json")
    q = cc.add_parser("strategy-backtest", help="多币种、多策略、多周期研究回测；不连接账户或下单")
    q.add_argument("--data-dir", required=True)
    q.add_argument("--universe-file", required=True)
    q.add_argument("--timeframes", default="1h,4h,1d")
    q.add_argument("--initial-cash", type=float, default=1000)
    q.add_argument("--max-order-quote", type=float, default=25)
    q.add_argument("--max-daily-loss-quote", type=float, default=10)
    q.add_argument("--partial-fill-fraction", type=float, default=0.5)
    q.add_argument("--base-fee-rate", type=float, default=0.001)
    q.add_argument("--base-slippage", type=float, default=0.0005)
    q.add_argument("--base-ioc-nonfill-rate", type=float, default=0.1)
    q.add_argument("--base-partial-fill-rate", type=float, default=0.05)
    q.add_argument("--stress-fee-rate", type=float, default=0.0015)
    q.add_argument("--stress-slippage", type=float, default=0.001)
    q.add_argument("--stress-ioc-nonfill-rate", type=float, default=0.2)
    q.add_argument("--stress-partial-fill-rate", type=float, default=0.1)
    q.add_argument("--output", required=True)
    q = cc.add_parser("portfolio-status", help="查看双币组合模拟/观察账本")
    q.add_argument("--db", default="runs/crypto-observe.sqlite")
    q = cc.add_parser("live", help="校验实盘门槛；本版本不发送真实订单")
    q.add_argument("--config", required=True); q.add_argument("--evidence", required=True)
    q.add_argument("--enable-live", action="store_true"); q.add_argument("--confirm-real-orders", default="")
    return p


def main(argv=None):
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = parser().parse_args(argv)
    try:
        if args.command == "crypto":
            from .crypto.market import fetch_binance, read_csv
            from .crypto.paper import CryptoPaperBroker, run as run_crypto
            from .crypto.portfolio import CryptoPortfolioPaper
            if args.crypto_action == "account":
                from .crypto.account import BinanceSpotAccountReadOnly
                client = BinanceSpotAccountReadOnly()
                emit(client.summarize(client.get_account()))
                return 0
            if args.crypto_action == "live":
                from .crypto.safety import live_gate
                config = json.loads(Path(args.config).read_text(encoding="utf-8"))
                gate = live_gate(enabled=args.enable_live, config=config, evidence_path=args.evidence,
                                 confirmation=args.confirm_real_orders)
                emit({**gate, "live_execution_implemented": False,
                      "message": "No production order is sent by this program version."})
                return 2 if not gate["prerequisites_passed"] else 0
            if args.crypto_action in ("watch", "watch-status"):
                from .crypto.observer import SignalObserver
                if args.crypto_action == "watch-status":
                    result = SignalObserver.read_status(args.db)
                    emit(result)
                    return 0 if all(s["data_current"] for s in result["symbols"]) else 2
                import asyncio
                from .crypto.monitor import watch_symbols
                observer = SignalObserver(args.db, fast=args.fast, slow=args.slow)
                try:
                    asyncio.run(watch_symbols(observer, once=args.once))
                except KeyboardInterrupt:
                    emit({"state":"stopped", "mode":"signal_only", "order_submission":False})
                finally:
                    observer.close()
                return 0
            if args.crypto_action == "testnet-run":
                from .crypto.testnet import TestnetTrader, make_testnet_client, run_testnet
                trader = None
                try:
                    trader = TestnetTrader(args.db, make_testnet_client(), fast=args.fast, slow=args.slow,
                        max_order_quote=args.max_order_quote, max_daily_loss_quote=args.max_daily_loss_quote,
                        max_price_deviation=args.max_price_deviation, enabled=args.enable_testnet,
                        confirmation=args.confirm_testnet_orders)
                    import asyncio
                    result = asyncio.run(run_testnet(trader, once=args.once))
                    emit({**result, "order_sent": False, "status": trader.status()})
                except KeyboardInterrupt:
                    emit({"state":"stopped", **(trader.status() if trader else {"mode":"spot_testnet"})})
                except Exception as exc:
                    emit({"state":"failed_closed", "mode":"spot_testnet",
                          "error_type":type(exc).__name__,
                          "status":trader.status() if trader else None})
                    return 2
                finally:
                    if trader:
                        trader.close()
                return 0
            if args.crypto_action == "adaptive-run":
                from .crypto.adaptive import (AdaptiveController, OpenAICompatibleProvider,
                                              post_webhook, validate_acceptance_gate)
                from .crypto.testnet import TestnetTrader, make_testnet_client, run_adaptive
                trader = controller = None
                try:
                    if args.mode == "testnet_auto":
                        if not args.acceptance_file:
                            raise ValueError("testnet_auto requires --acceptance-file")
                        validate_acceptance_gate(args.acceptance_file)
                        if not args.enable_testnet or args.confirm_testnet_orders != "I_ACCEPT_TESTNET_ORDERS":
                            raise ValueError("Testnet auto orders require explicit confirmation")
                        trader = TestnetTrader(args.trading_db, make_testnet_client(), fast=24, slow=72,
                            max_order_quote=args.max_order_quote,
                            max_daily_loss_quote=args.max_daily_loss_quote,
                            max_price_deviation=args.max_price_deviation,
                            enabled=True, confirmation=args.confirm_testnet_orders,
                            adaptive_profiles=True)
                    provider = OpenAICompatibleProvider()
                    webhook = os.environ.get("QUANT_ALERT_WEBHOOK_URL", "")
                    controller = AdaptiveController(args.db, mode=args.mode, provider=provider,
                        notifier=(lambda payload: post_webhook(webhook, payload)) if webhook else None)
                    import asyncio
                    asyncio.run(run_adaptive(controller, trader=trader))
                except KeyboardInterrupt:
                    if trader:
                        trader.halt("manual_shutdown")
                    emit({"state":"stopped", "mode":args.mode,
                          "adaptive":controller.status() if controller else None,
                          "testnet":trader.status() if trader else None})
                except Exception as exc:
                    emit({"state":"failed_closed", "mode":args.mode,
                          "error_type":type(exc).__name__,
                          "adaptive":controller.status() if controller else None,
                          "testnet":trader.status() if trader else None})
                    return 2
                finally:
                    if controller: controller.close()
                    if trader: trader.close()
                return 0
            if args.crypto_action == "testnet-unlock":
                from .crypto.testnet import TestnetTrader, make_testnet_client
                trader = TestnetTrader(args.db, make_testnet_client(), enabled=True,
                    confirmation="I_ACCEPT_TESTNET_ORDERS")
                try:
                    if args.confirm_manual_reset == "I_REVIEWED_TESTNET_ACCOUNT_AND_OPEN_ORDERS":
                        emit(trader.reset_manual_halt())
                    elif args.confirm_daily_loss_review == "I_REVIEWED_DAILY_LOSS_AND_TESTNET_ACCOUNT":
                        emit(trader.reset_daily_loss_lock_after_review())
                    else:
                        raise ValueError("Use the explicit matching confirmation for the lock reason")
                finally:
                    trader.close()
                return 0
            if args.crypto_action == "adaptive-status":
                path = Path(args.db)
                if not path.is_file():
                    emit({"mode":"shadow", "database_exists":False, "orders_allowed":False})
                    return 2
                import sqlite3
                with sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True) as db:
                    state = dict(db.execute("SELECT key,value FROM ai_state"))
                    counts = {row[0]:row[1] for row in db.execute("SELECT state,COUNT(*) FROM ai_hours GROUP BY state")}
                    span = db.execute("SELECT MIN(hour_ms),MAX(hour_ms),COUNT(*) FROM ai_hours WHERE state='accepted'").fetchone()
                    total = db.execute("SELECT COUNT(*) FROM ai_hours").fetchone()[0]
                    transitions = db.execute("SELECT COUNT(*) FROM ai_transitions").fetchone()[0]
                active_mode = state.get("mode", "shadow")
                span_hours = ((span[1]-span[0])//3_600_000+1) if span[0] is not None else 0
                emit({"mode":active_mode, "orders_allowed":active_mode == "testnet_auto",
                      "active_profile":state.get("active_profile"),
                      "candidate_profile":state.get("candidate_profile") or None,
                      "candidate_confirmations":int(state.get("candidate_count", "0")),
                      "evaluations":counts, "accepted_hours":counts.get("accepted", 0),
                      "observation_span_hours":span_hours,
                      "missing_or_failed_hours":max(0, span_hours-counts.get("accepted", 0)),
                      "journal_hours":total, "transitions":transitions})
                return 0
            if args.crypto_action == "adaptive-backtest":
                from .crypto.adaptive_backtest import backtest_profiles
                bars = {"BTCUSDT":read_csv(args.btc_csv), "ETHUSDT":read_csv(args.eth_csv)}
                result = backtest_profiles(bars, initial_cash=args.initial_cash,
                    fee_rate=args.fee_rate, slippage=args.slippage,
                    max_order_quote=args.max_order_quote,
                    max_daily_loss_quote=args.max_daily_loss_quote,
                    nonfill_rate=args.ioc_nonfill_rate,
                    partial_fill_rate=args.partial_fill_rate,
                    partial_fill_fraction=args.partial_fill_fraction)
                output = Path(args.output)
                if output.exists():
                    raise ValueError("Backtest output already exists; choose a new versioned path")
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
                emit({"output":str(output), **result})
                return 0
            if args.crypto_action == "universe-screen":
                from .crypto.research import screen_universe
                if not 1 <= args.limit <= 8:
                    raise ValueError("Universe limit must be between 1 and 8")
                result = screen_universe(args.data_dir, limit=args.limit,
                                         market_snapshot=args.market_snapshot)
                output = Path(args.output)
                if output.exists():
                    raise ValueError("Universe output already exists; choose a new versioned path")
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
                emit({"output": str(output), **result})
                return 0 if len(result["symbols"]) == args.limit else 2
            if args.crypto_action == "fetch-history":
                from .crypto.research import download_hourly_history
                emit(download_hourly_history(args.symbols, args.start, args.end, args.data_dir))
                return 0
            if args.crypto_action == "data-check":
                from .crypto.data_quality import check_hourly_csvs
                symbols = list(dict.fromkeys(s.upper() for s in args.symbols))
                if len(symbols) != len(args.symbols):
                    raise ValueError("Duplicate symbols are not allowed")
                output = Path(args.output)
                if output.exists():
                    raise ValueError("Data-check output already exists; choose a new versioned path")
                result = check_hourly_csvs(
                    files={s: Path(args.data_dir) / f"{s}-1h.csv" for s in symbols},
                    purpose=args.purpose, source_label=args.source, min_days=args.min_days,
                    max_age_hours=args.max_age_hours,
                    require_exact_alignment=not args.no_require_exact_alignment,
                    code_path=Path(__file__).with_name("crypto") / "data_quality.py")
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
                                  encoding="utf-8")
                emit({"output": str(output), **result})
                return 0 if result["passed"] else 2
            if args.crypto_action == "strategy-backtest":
                from .crypto.research import strategy_backtest
                timeframes = tuple(x.strip() for x in args.timeframes.split(",") if x.strip())
                result = strategy_backtest(args.data_dir, args.universe_file, timeframes,
                    initial_cash=args.initial_cash, max_order_quote=args.max_order_quote,
                    max_daily_loss=args.max_daily_loss_quote,
                    partial_fill_fraction=args.partial_fill_fraction,
                    base_costs={"fee_rate":args.base_fee_rate, "slippage":args.base_slippage,
                                "ioc_nonfill_rate":args.base_ioc_nonfill_rate,
                                "partial_fill_rate":args.base_partial_fill_rate},
                    stress_costs={"fee_rate":args.stress_fee_rate, "slippage":args.stress_slippage,
                                  "ioc_nonfill_rate":args.stress_ioc_nonfill_rate,
                                  "partial_fill_rate":args.stress_partial_fill_rate})
                output = Path(args.output)
                if output.exists():
                    raise ValueError("Backtest output already exists; choose a new versioned path")
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
                emit({"output": str(output), **result})
                return 0
            if args.crypto_action == "testnet-status":
                path = Path(args.db)
                if not path.is_file():
                    emit({"mode":"spot_testnet", "database_exists":False, "production_orders":False})
                    return 2
                import sqlite3
                with sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True) as db:
                    lock = db.execute("SELECT value FROM bot_state WHERE key='lock'").fetchone()
                    counts = {row[0]:row[1] for row in db.execute("SELECT status,COUNT(*) FROM bot_orders GROUP BY status")}
                    signals = db.execute("SELECT COUNT(*) FROM bot_signals").fetchone()[0]
                emit({"mode":"spot_testnet", "production_orders":False, "locked":bool(lock and lock[0]),
                      "lock_reason":lock[0] if lock else None, "orders":counts, "signals":signals})
                return 0
            if args.crypto_action in ("observe", "portfolio-status"):
                portfolio = CryptoPortfolioPaper(args.db)
                try:
                    if args.crypto_action == "observe":
                        events = portfolio.observe_bars({"BTCUSDT":read_csv(args.btc_csv),
                            "ETHUSDT":read_csv(args.eth_csv)}, fast=args.fast, slow=args.slow,
                            strategy_version=f"sma-{args.fast}-{args.slow}-candidate-v1")
                        emit({"mode":"signal_only","events":len(events),"order_submission":False,
                              "status":portfolio.status()})
                    elif args.crypto_action == "portfolio-status":
                        emit(portfolio.status())
                    return 0
                finally: portfolio.close()
            if args.crypto_action == "fetch":
                import csv
                rows = fetch_binance(args.symbol, args.limit)
                path = Path(args.output); path.parent.mkdir(parents=True, exist_ok=True)
                fields = ["timestamp", "open", "high", "low", "close", "volume"]
                with path.open("w", newline="", encoding="utf-8") as f:
                    writer = csv.DictWriter(f, fieldnames=fields); writer.writeheader()
                    writer.writerows({k:r[k] for k in fields} for r in rows)
                emit({"symbol":args.symbol.upper(),"interval":"1h","candles":len(rows),"output":str(path),"source":"Binance public REST","live_trading":False})
                return 0
            if args.crypto_action in ("status", "halt"):
                paper = CryptoPaperBroker(args.db)
                try:
                    if args.crypto_action == "status": emit(paper.status(args.price))
                    else: paper.halt(args.reason); emit({"kill_switch":"active","reason":args.reason})
                finally: paper.close()
                return 0
            paper = CryptoPaperBroker(args.db, initial_cash=args.initial_cash, fee_rate=args.fee_rate,
                slippage=args.slippage, max_order_quote=args.max_order_quote,
                max_position_weight=args.max_position_weight, max_daily_loss=args.max_daily_loss,
                max_price_deviation=args.max_price_deviation)
            try:
                result = run_crypto(read_csv(args.csv), paper, args.symbol.upper(), args.fast, args.slow)
                emit(result)
                return 0 if result["reconciliation"]["ok"] else 2
            finally: paper.close()
        return 2
    except (ValueError, OSError, KeyError) as exc:
        logging.error("%s", exc)
        return 2


if __name__ == "__main__":
    sys.exit(main())

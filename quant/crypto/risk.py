from __future__ import annotations


def check_order(*, side, quantity, price, cash, position, equity, max_order_quote,
                max_position_weight, max_daily_loss, daily_pnl, reference_price,
                max_price_deviation):
    if side not in ("BUY", "SELL") or quantity <= 0 or price <= 0:
        return "invalid_order"
    if equity <= 0 or daily_pnl <= -max_daily_loss * equity:
        return "daily_loss_limit"
    if quantity * price > max_order_quote:
        return "max_order_quote"
    if abs(price / reference_price - 1) > max_price_deviation:
        return "price_deviation"
    if side == "BUY" and quantity * price > cash:
        return "insufficient_cash"
    if side == "SELL" and quantity > position:
        return "insufficient_position"
    if side == "BUY" and (position + quantity) * price > equity * max_position_weight:
        return "max_position_weight"
    return None


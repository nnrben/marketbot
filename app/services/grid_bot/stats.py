import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from app.database import db
logger = logging.getLogger(__name__)

def _num(v: Any) -> float:
    if v is None:
        return 0.0
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0

def _iso(dt: Any) -> Optional[str]:
    if dt is None:
        return None
    if isinstance(dt, datetime):
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'
    return str(dt)

async def compute_bot_stats(bot_data: Dict[str, Any]) -> Dict[str, Any]:
    bot_id = int(bot_data['id'])
    async with db.acquire() as conn:
        trades = await conn.fetch('SELECT t.id, t.order_id, t.direction, t.lots, t.lot_size, t.price_per_share, t.quantity_shares, t.total_amount, t.commission, t.executed_at, o.level_idx, o.pair_level FROM trade_history t LEFT JOIN grid_orders o ON o.order_id = t.order_id AND o.bot_id = t.bot_id WHERE t.bot_id = $1 ORDER BY t.executed_at ASC, t.id ASC', bot_id)
    total_buy_amount = 0.0
    total_sell_amount = 0.0
    total_commission = 0.0
    gross_profit = 0.0
    open_buys: List[Dict[str, Any]] = []
    for t in trades:
        shares = _num(t.get('quantity_shares')) or _num(t.get('lots')) * (_num(t.get('lot_size')) or 1.0)
        total_amount = _num(t.get('total_amount'))
        price_per_share = _num(t.get('price_per_share')) or (total_amount / shares if shares > 0 else 0.0)
        commission = _num(t.get('commission'))
        total_commission += commission
        direction = t.get('direction')
        if direction == 'buy':
            total_buy_amount += total_amount
            open_buys.append({'shares': shares, 'price': price_per_share, 'level': t.get('level_idx')})
            continue
        if direction != 'sell':
            continue
        total_sell_amount += total_amount
        remaining = shares
        level_idx = t.get('level_idx')
        pair_level = t.get('pair_level')
        if pair_level is None and level_idx is not None:
            pair_level = level_idx - 1
        if pair_level is not None:
            for lot in open_buys:
                if remaining <= 1e-09:
                    break
                if lot['level'] != pair_level or lot['shares'] <= 1e-09:
                    continue
                matched = min(remaining, lot['shares'])
                gross_profit += (price_per_share - lot['price']) * matched
                lot['shares'] -= matched
                remaining -= matched
        while remaining > 1e-09:
            idx = -1
            best_price = float('inf')
            for i, lot in enumerate(open_buys):
                if lot['shares'] <= 1e-09:
                    continue
                if lot['price'] < best_price:
                    best_price = lot['price']
                    idx = i
            if idx == -1:
                break
            lot = open_buys[idx]
            matched = min(remaining, lot['shares'])
            gross_profit += (price_per_share - lot['price']) * matched
            lot['shares'] -= matched
            remaining -= matched
        open_buys = [lot for lot in open_buys if lot['shares'] > 1e-09]
    realized_profit = gross_profit - total_commission
    total_trades_amount = total_buy_amount + total_sell_amount
    open_shares = sum((lot['shares'] for lot in open_buys))
    open_cost = sum((lot['shares'] * lot['price'] for lot in open_buys))
    avg_open_price = open_cost / open_shares if open_shares > 0 else 0.0
    current_price: Optional[float] = None
    if bot_data.get('current_price') is not None:
        current_price = float(bot_data['current_price'])
    unrealized_profit: Optional[float] = None
    if current_price is not None and open_shares > 0:
        unrealized_profit = (current_price - avg_open_price) * open_shares
    capital = _num(bot_data.get('capital'))

    def pct(v: Optional[float]) -> Optional[float]:
        if v is None or capital <= 0:
            return None
        return round(v / capital * 100, 2)
    total_profit_combined = realized_profit + (unrealized_profit or 0.0)
    bot_created_at = bot_data.get('created_at')
    first_trade_at = trades[0]['executed_at'] if trades else None
    last_trade_at = trades[-1]['executed_at'] if trades else None
    started_at = bot_created_at or first_trade_at
    days_running: Optional[int] = None
    if isinstance(started_at, datetime):
        if started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=timezone.utc)
        days_running = max(0, int((datetime.now(timezone.utc) - started_at).total_seconds() // 86400))
    return {'source': 'user_server', 'totalTrades': len(trades), 'totalProfit': round(realized_profit, 2), 'realizedProfit': round(realized_profit, 2), 'grossProfit': round(gross_profit, 2), 'unrealizedProfit': None if unrealized_profit is None else round(unrealized_profit, 2), 'totalProfitCombined': round(total_profit_combined, 2), 'realizedProfitPct': pct(realized_profit), 'unrealizedProfitPct': pct(unrealized_profit), 'totalProfitPct': pct(total_profit_combined), 'capital': round(capital, 2) if capital > 0 else None, 'currentPrice': current_price, 'positionQuantity': round(open_shares, 6), 'averageOpenPrice': round(avg_open_price, 4), 'totalCommission': round(total_commission, 2), 'totalBuyAmount': round(total_buy_amount, 2), 'totalSellAmount': round(total_sell_amount, 2), 'totalTradesAmount': round(total_trades_amount, 2), 'botCreatedAt': _iso(bot_created_at), 'firstTradeAt': _iso(first_trade_at), 'lastTradeAt': _iso(last_trade_at), 'daysRunning': days_running}

async def list_trades(bot_id: int, limit: int=0, offset: int=0) -> Dict[str, Any]:
    async with db.acquire() as conn:
        total = await conn.fetchval('SELECT COUNT(*) FROM trade_history WHERE bot_id=$1', bot_id)
        if limit and limit > 0:
            rows = await conn.fetch('SELECT * FROM trade_history WHERE bot_id=$1 ORDER BY executed_at DESC, id DESC LIMIT $2 OFFSET $3', bot_id, limit, offset)
        else:
            rows = await conn.fetch('SELECT * FROM trade_history WHERE bot_id=$1 ORDER BY executed_at DESC, id DESC LIMIT -1 OFFSET $2', bot_id, offset)
    trades = []
    for r in rows:
        trades.append({'id': r['id'], 'bot_id': r['bot_id'], 'bot_type': r.get('bot_type') or 'simple', 'order_id': r.get('order_id'), 'figi': r.get('figi'), 'ticker': r.get('ticker'), 'direction': r['direction'], 'lots': r['lots'], 'lot_size': r.get('lot_size') or 1, 'price_per_share': _num(r.get('price_per_share')), 'quantity_shares': _num(r.get('quantity_shares')), 'total_amount': _num(r.get('total_amount')), 'commission': _num(r.get('commission')), 'executed_at': _iso(r.get('executed_at')), 'status': 'executed', 'created_at': _iso(r.get('created_at'))})
    total = int(total or 0)
    return {'trades': trades, 'pagination': {'limit': limit if limit and limit > 0 else total, 'offset': offset, 'total': total, 'returned': len(trades)}}

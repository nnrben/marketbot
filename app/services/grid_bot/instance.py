import asyncio
import uuid
import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional, List, Any
import httpx
from app.database import db
from app.config import settings, TICKER1_TO_FIGIA
from app.services.grid_bot.config import CA_BUNDLE_PATH, ACCOUNTS_URL, INSTRUMENT_URL, LAST_PRICES_URL, GET_CANDLES_URL, POST_ORDER_URL, ORDER_STATE_URL, POST_STOP_ORDER_URL, GET_STOP_ORDERS_URL, CANCEL_STOP_ORDER_URL, GET_POSITIONS_URL, TRADING_SCHEDULES_URL, GET_OPERATIONS_URL
from app.services.grid_bot.exceptions import ExchangeClosedError, _is_exchange_closed_error
from app.services.grid_bot.exchange_utils import _resolve_schedule_exchange
from app.license import license_manager
logger = logging.getLogger(__name__)

class GridBotInstance:

    def __init__(self, bot_id: int, user_id: str, tinkoff_token: str, ticker: str, class_code: str, P_low: float, P_high: float, capital: float, N: int, initial_lots: int, cash_remaining: float, current_price: Optional[float]=None, figi: Optional[str]=None):
        self.bot_id = bot_id
        self.user_id = user_id
        self.tinkoff_token = tinkoff_token
        self.ticker = ticker
        self.class_code = class_code
        self.P_low = P_low
        self.P_high = P_high
        self.capital = capital
        self.N = N
        self.initial_lots = initial_lots
        self.cash_remaining = cash_remaining
        self.current_price = current_price
        self.figi = figi or TICKER1_TO_FIGIA.get(ticker)
        self.instrument_uid: Optional[str] = None
        self.exchange: Optional[str] = None
        self.lot_size: int = 1
        self.step: float = 0.01
        self.d: float = 0.0
        self.levels: List[float] = []
        self.account_id: Optional[str] = None
        self.active_orders: Dict[str, dict] = {}
        self.monitor_task: Optional[asyncio.Task] = None
        self._stop_flag = False
        self.trading_paused = False
        self._license_hold = False
        self._last_deploy_attempt: Optional[datetime] = None

    def _get_headers(self) -> dict:
        return {'Authorization': f'Bearer {self.tinkoff_token}', 'Content-Type': 'application/json', 'accept': 'application/json'}

    async def _get_accounts(self, client: httpx.AsyncClient) -> List[dict]:
        resp = await client.post(ACCOUNTS_URL, headers=self._get_headers(), json={})
        resp.raise_for_status()
        return resp.json().get('accounts', [])

    async def _get_open_account_id(self, client: httpx.AsyncClient) -> str:
        accounts = await self._get_accounts(client)
        if settings.account_id:
            for a in accounts:
                if a.get('id') == settings.account_id:
                    if a.get('status') != 'ACCOUNT_STATUS_OPEN':
                        raise RuntimeError(f"Счёт {settings.account_id} найден, но не открыт (статус {a.get('status')})")
                    return a['id']
            raise RuntimeError(f'Счёт {settings.account_id} (переменная ACCOUNT_ID) не найден среди счетов, доступных этому токену')
        for a in accounts:
            if a['status'] == 'ACCOUNT_STATUS_OPEN' and a['type'] in ('ACCOUNT_TYPE_TINKOFF', 'ACCOUNT_TYPE_TINKOFF_IIS'):
                return a['id']
        raise RuntimeError('Нет открытого счёта')

    async def _get_instrument_by_ticker(self, client: httpx.AsyncClient) -> dict:
        body = {'idType': 'INSTRUMENT_ID_TYPE_TICKER', 'id': self.ticker, 'classCode': self.class_code}
        last_exc: Optional[Exception] = None
        for attempt in range(4):
            try:
                resp = await client.post(INSTRUMENT_URL, headers=self._get_headers(), json=body)
                if resp.status_code in (502, 503, 504) or resp.status_code == 429:
                    last_exc = httpx.HTTPStatusError(f'{resp.status_code}', request=resp.request, response=resp)
                    logger.warning(f'бот {self.bot_id}: GetInstrumentBy вернул {resp.status_code} (попытка {attempt + 1}/4), повтор через паузу')
                    await asyncio.sleep(1.5 * (attempt + 1))
                    continue
                resp.raise_for_status()
                return resp.json()['instrument']
            except (httpx.TimeoutException, httpx.TransportError) as e:
                last_exc = e
                logger.warning(f'бот {self.bot_id}: сетевая ошибка GetInstrumentBy (попытка {attempt + 1}/4): {e}, повтор через паузу')
                await asyncio.sleep(1.5 * (attempt + 1))
        if last_exc is not None:
            raise last_exc
        raise RuntimeError('Не удалось получить инструмент: неизвестная ошибка')

    async def _get_last_price(self, client: httpx.AsyncClient) -> Optional[float]:
        if not self.figi:
            logger.error('FIGI не установлен')
            return None
        body = {'instrumentId': [self.figi], 'lastPriceType': 'LAST_PRICE_EXCHANGE'}
        last_exc: Optional[Exception] = None
        for attempt in range(4):
            try:
                resp = await client.post(LAST_PRICES_URL, headers=self._get_headers(), json=body)
                if resp.status_code in (429, 502, 503, 504):
                    logger.warning(f'бот {self.bot_id}: GetLastPrices вернул {resp.status_code} (попытка {attempt + 1}/4), повтор через паузу')
                    last_exc = httpx.HTTPStatusError(f'{resp.status_code}', request=resp.request, response=resp)
                    await asyncio.sleep(1.0 * (attempt + 1))
                    continue
                resp.raise_for_status()
                data = resp.json()
                prices = data.get('lastPrices', [])
                if not prices:
                    return None
                price_obj = prices[0]['price']
                return int(price_obj['units']) + int(price_obj['nano']) / 1000000000.0
            except (httpx.TimeoutException, httpx.TransportError) as e:
                last_exc = e
                logger.warning(f'бот {self.bot_id}: сетевая ошибка GetLastPrices (попытка {attempt + 1}/4): {e}, повтор через паузу')
                await asyncio.sleep(1.0 * (attempt + 1))
            except Exception as e:
                logger.error(f'Ошибка получения последней цены: {e}')
                return None
        logger.error(f'Ошибка получения последней цены: исчерпаны повторы ({last_exc})')
        return None

    async def _get_candles(self, client: httpx.AsyncClient, from_dt: datetime, to_dt: datetime, interval: str='CANDLE_INTERVAL_HOUR') -> List[dict]:
        if not self.figi:
            logger.error('FIGI не установлен — свечи не запрашиваются')
            return []

        def _fmt(dt: datetime) -> str:
            return dt.strftime('%Y-%m-%dT%H:%M:%SZ')
        body = {'instrumentId': self.figi, 'from': _fmt(from_dt), 'to': _fmt(to_dt), 'interval': interval}
        try:
            resp = await client.post(GET_CANDLES_URL, headers=self._get_headers(), json=body)
            resp.raise_for_status()
            raw = resp.json().get('candles', [])
        except Exception as e:
            logger.error(f'Ошибка получения свечей {self.ticker}: {e}')
            return []

        def _q(v: Optional[dict]) -> float:
            if not v:
                return 0.0
            return int(v.get('units', 0)) + int(v.get('nano', 0)) / 1000000000.0
        out: List[dict] = []
        for c in raw:
            ts = self._parse_api_dt(c.get('time'))
            if ts is None:
                continue
            out.append({'time': int(ts.timestamp()), 'open': _q(c.get('open')), 'high': _q(c.get('high')), 'low': _q(c.get('low')), 'close': _q(c.get('close')), 'volume': int(c.get('volume', 0) or 0)})
        out.sort(key=lambda x: x['time'])
        return out

    async def _get_operations(self, client: httpx.AsyncClient, from_dt: datetime, to_dt: datetime) -> List[dict]:
        body = {'accountId': self.account_id, 'from': from_dt.strftime('%Y-%m-%dT%H:%M:%SZ'), 'to': to_dt.strftime('%Y-%m-%dT%H:%M:%SZ'), 'state': 'OPERATION_STATE_EXECUTED', 'figi': self.figi}
        resp = await client.post(GET_OPERATIONS_URL, headers=self._get_headers(), json=body)
        resp.raise_for_status()
        return resp.json().get('operations', []) or []

    async def _get_positions(self, client: httpx.AsyncClient) -> dict:
        body = {'accountId': self.account_id}
        resp = await client.post(GET_POSITIONS_URL, headers=self._get_headers(), json=body)
        resp.raise_for_status()
        return resp.json()

    async def _get_stop_orders(self, client: httpx.AsyncClient, status: str='STOP_ORDER_STATUS_ALL') -> List[dict]:
        body = {'accountId': self.account_id, 'status': status}
        resp = await client.post(GET_STOP_ORDERS_URL, headers=self._get_headers(), json=body)
        resp.raise_for_status()
        return resp.json().get('stopOrders', [])

    async def _post_order_async(self, client: httpx.AsyncClient, instrument_id: str, quantity: int, direction: str, order_type: str, order_id: str, price: Optional[dict]=None, time_in_force: Optional[str]=None, price_type: str='PRICE_TYPE_CURRENCY', confirm_margin_trade: bool=False) -> dict:
        body = {'instrumentId': instrument_id, 'quantity': str(quantity), 'direction': direction, 'accountId': self.account_id, 'orderType': order_type, 'orderId': order_id, 'priceType': price_type, 'confirmMarginTrade': confirm_margin_trade}
        if price is not None:
            body['price'] = price
        if time_in_force is not None:
            body['timeInForce'] = time_in_force
        if order_type == 'ORDER_TYPE_MARKET':
            body.pop('price', None)
            body.pop('timeInForce', None)
        resp = await client.post(POST_ORDER_URL, headers=self._get_headers(), json=body)
        if resp.status_code != 200:
            raise Exception(f'Ошибка заявки: {resp.status_code} {resp.text}')
        return resp.json()

    async def _post_stop_order(self, client: httpx.AsyncClient, instrument_id: str, quantity: int, direction: str, stop_price: dict, price: dict, order_id: str, expire_date: str) -> dict:
        body = {'instrumentId': instrument_id, 'quantity': str(quantity), 'direction': direction, 'accountId': self.account_id, 'stopOrderType': 'STOP_ORDER_TYPE_TAKE_PROFIT', 'takeProfitType': 'TAKE_PROFIT_TYPE_REGULAR', 'stopPrice': stop_price, 'price': price, 'exchangeOrderType': 'EXCHANGE_ORDER_TYPE_LIMIT', 'expirationType': 'STOP_ORDER_EXPIRATION_TYPE_GOOD_TILL_DATE', 'expireDate': expire_date, 'orderId': order_id}
        resp = await client.post(POST_STOP_ORDER_URL, headers=self._get_headers(), json=body)
        if resp.status_code != 200:
            raise Exception(f'Ошибка стоп-заявки: {resp.status_code} {resp.text}')
        return resp.json()

    async def _get_order_state(self, client: httpx.AsyncClient, order_id: str, order_id_type: str='ORDER_ID_TYPE_REQUEST') -> Optional[dict]:
        body = {'accountId': self.account_id, 'orderId': order_id, 'orderIdType': order_id_type}
        resp = await client.post(ORDER_STATE_URL, headers=self._get_headers(), json=body)
        if resp.status_code == 200:
            return resp.json()
        return None

    async def _cancel_stop_order(self, client: httpx.AsyncClient, stop_order_id: str) -> bool:
        body = {'accountId': self.account_id, 'stopOrderId': stop_order_id}
        resp = await client.post(CANCEL_STOP_ORDER_URL, headers=self._get_headers(), json=body)
        return resp.status_code == 200

    @staticmethod
    def _parse_api_dt(value: Optional[str]) -> Optional[datetime]:
        if not value:
            return None
        try:
            v = value.strip()
            if v.endswith('Z'):
                v = v[:-1] + '+00:00'
            dt = datetime.fromisoformat(v)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except Exception:
            return None

    async def _get_trading_schedule_day(self, client: httpx.AsyncClient, exchange: str, from_dt: datetime, to_dt: datetime) -> Optional[dict]:
        body: Dict[str, Any] = {'from': from_dt.strftime('%Y-%m-%dT%H:%M:%SZ'), 'to': to_dt.strftime('%Y-%m-%dT%H:%M:%SZ')}
        if exchange:
            body['exchange'] = exchange
        resp = await client.post(TRADING_SCHEDULES_URL, headers=self._get_headers(), json=body)
        if resp.status_code == 400 and exchange:
            logger.warning(f"бот {self.bot_id}: площадка '{exchange}' не принята TradingSchedules (400), повторяю запрос без фильтра площадки")
            body.pop('exchange', None)
            exchange = ''
            resp = await client.post(TRADING_SCHEDULES_URL, headers=self._get_headers(), json=body)
        resp.raise_for_status()
        data = resp.json()
        exchanges = data.get('exchanges', []) or []
        if not exchanges:
            return None
        target = None
        for ex in exchanges:
            if not exchange or ex.get('exchange') == exchange:
                target = ex
                break
        if target is None:
            target = exchanges[0]
        days = target.get('days', []) or []
        if not days:
            return None
        now = datetime.now(timezone.utc)
        best = None
        for day in days:
            start = self._parse_api_dt(day.get('startTime'))
            end = self._parse_api_dt(day.get('endTime'))
            date = self._parse_api_dt(day.get('date'))
            if start and end and (start <= now <= end):
                return day
            candidate_time = start or date
            if day.get('isTradingDay') and candidate_time and (candidate_time >= now):
                if best is None:
                    best = (candidate_time, day)
                elif candidate_time < best[0]:
                    best = (candidate_time, day)
        if best is not None:
            return best[1]
        return days[0]

    async def is_exchange_open(self, client: httpx.AsyncClient) -> bool:
        exchange = self.exchange or ''
        now = datetime.now(timezone.utc)
        from_dt = now
        to_dt = now + timedelta(days=2)
        try:
            day = await self._get_trading_schedule_day(client, exchange, from_dt, to_dt)
        except Exception as e:
            logger.warning(f'бот {self.bot_id}: не удалось получить расписание торгов ({exchange}): {e}')
            return True
        if not day:
            return True
        if not day.get('isTradingDay', False):
            return False

        def _in(a, b) -> bool:
            sa = self._parse_api_dt(a)
            sb = self._parse_api_dt(b)
            return bool(sa and sb and (sa <= now <= sb))
        if _in(day.get('startTime'), day.get('endTime')):
            return True
        if _in(day.get('eveningStartTime'), day.get('eveningEndTime')):
            return True
        return False

    async def next_exchange_open_dt(self, client: httpx.AsyncClient) -> Optional[datetime]:
        exchange = self.exchange or ''
        now = datetime.now(timezone.utc)
        from_dt = now
        to_dt = now + timedelta(days=7)
        try:
            day = await self._get_trading_schedule_day(client, exchange, from_dt, to_dt)
        except Exception:
            return None
        if not day:
            return None
        start = self._parse_api_dt(day.get('startTime'))
        return start

    @staticmethod
    def _money_to_float(money: Optional[dict]) -> float:
        if not money:
            return 0.0
        return float(money.get('units', 0)) + float(money.get('nano', 0)) / 1000000000.0

    @staticmethod
    def _price_to_dict(price_float: float) -> dict:
        units = int(price_float)
        nano = int(round((price_float - units) * 1000000000.0))
        return {'units': units, 'nano': nano}

    @staticmethod
    def _round_price(price: float, step: float) -> float:
        return round(price / step) * step

    def _extract_execution_details(self, state: dict):
        lots_executed = int(state.get('lotsExecuted', 0))
        avg_price = state.get('averagePositionPrice')
        executed_price = state.get('executedOrderPrice')
        if avg_price and (avg_price.get('units') or avg_price.get('nano')):
            price_per_share = self._money_to_float(avg_price)
        elif executed_price and lots_executed > 0:
            total = self._money_to_float(executed_price)
            price_per_share = total / (lots_executed * self.lot_size)
        else:
            price_per_share = 0.0
        commission = self._money_to_float(state.get('executedCommission') or state.get('initialCommission'))
        return (price_per_share, commission, lots_executed)

    async def _save_trade(self, order_id: str, direction: str, price_per_share: float, lots: int, lot_size: int, executed_at: datetime, commission: float=0.0) -> int:
        quantity_shares = lots * lot_size
        total_amount = price_per_share * quantity_shares
        async with db.acquire() as conn:
            row = await conn.fetchrow('INSERT INTO trade_history\n                   (bot_id, bot_type, order_id, figi, ticker, direction,\n                    lots, lot_size, price_per_share, quantity_shares,\n                    total_amount, commission, executed_at)\n                   VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)\n                   RETURNING id', self.bot_id, 'grid', order_id, self.figi, self.ticker, direction, lots, lot_size, price_per_share, quantity_shares, total_amount, commission, executed_at)
            return row['id']

    async def _place_stop_order(self, client: httpx.AsyncClient, level_idx: int, lots: int, side: str) -> bool:
        if lots <= 0:
            return False
        price_share = self._round_price(self.levels[level_idx], self.step)
        pd = self._price_to_dict(price_share)
        client_order_id = str(uuid.uuid4())
        expire_dt = datetime.utcnow() + timedelta(days=30)
        expire_str = expire_dt.strftime('%Y-%m-%dT%H:%M:%S') + '+00:00'
        direction = 'STOP_ORDER_DIRECTION_BUY' if side == 'buy' else 'STOP_ORDER_DIRECTION_SELL'
        pair_level = level_idx + 1 if side == 'buy' else level_idx - 1
        try:
            resp = await self._post_stop_order(client=client, instrument_id=self.figi, quantity=lots, direction=direction, stop_price=pd, price=pd, order_id=client_order_id, expire_date=expire_str)
        except Exception as e:
            if _is_exchange_closed_error(e):
                raise ExchangeClosedError(str(e))
            logger.error(f'бот {self.bot_id}: ошибка выставления {side} заявки уровень {level_idx}: {e}')
            return False
        stop_order_id = resp.get('stopOrderId')
        if not stop_order_id:
            logger.error(f'бот {self.bot_id}: ответ без stopOrderId: {resp}')
            return False
        async with db.acquire() as conn:
            await conn.execute("INSERT INTO grid_orders (bot_id, order_id, client_order_id, side, level_idx, lots, pair_level, status)\n                   VALUES ($1, $2, $3, $4, $5, $6, $7, 'active')", self.bot_id, stop_order_id, client_order_id, side, level_idx, lots, pair_level)
        self.active_orders[stop_order_id] = {'side': side, 'level_idx': level_idx, 'lots': lots, 'pair': pair_level}
        logger.info(f'бот {self.bot_id}: выставлена {side} заявка уровень={level_idx} цена={price_share} лотов={lots} stopOrderId={stop_order_id}')
        return True

    async def _place_stop_buy(self, client: httpx.AsyncClient, level_idx: int, lots: int) -> bool:
        return await self._place_stop_order(client, level_idx, lots, 'buy')

    async def _place_stop_sell(self, client: httpx.AsyncClient, level_idx: int, lots: int) -> bool:
        return await self._place_stop_order(client, level_idx, lots, 'sell')

    async def _ensure_counter_order(self, client: httpx.AsyncClient, side: str, level_idx: int, lots: int):
        if level_idx < 0 or level_idx > self.N:
            logger.info(f'бот {self.bot_id}: уровень {level_idx} вне границ сетки, встречная заявка не требуется')
            return
        async with db.acquire() as conn:
            existing = await conn.fetchrow("SELECT order_id FROM grid_orders WHERE bot_id=$1 AND side=$2 AND level_idx=$3 AND status='active'", self.bot_id, side, level_idx)
        if existing:
            logger.info(f'бот {self.bot_id}: встречная {side} заявка уже активна на уровне {level_idx}')
            return
        await self._place_stop_order(client, level_idx, lots, side)

    async def _handle_stop_order_executed(self, client: httpx.AsyncClient, row: dict, broker_order: dict):
        exchange_order_id = broker_order.get('exchangeOrderId')
        lots_executed = int(row['lots'])
        price_per_share = 0.0
        commission = 0.0
        resolved = False
        if exchange_order_id:
            for _ in range(5):
                state = await self._get_order_state(client, exchange_order_id, 'ORDER_ID_TYPE_EXCHANGE')
                if state and int(state.get('lotsExecuted', 0)) > 0:
                    price_per_share, commission, executed_lots = self._extract_execution_details(state)
                    lots_executed = executed_lots or lots_executed
                    resolved = True
                    break
                await asyncio.sleep(1)
        if not resolved:
            fallback_price = broker_order.get('price') or broker_order.get('stopPrice')
            price_per_share = self._money_to_float(fallback_price)
        executed_at = datetime.utcnow()
        await self._save_trade(order_id=row['order_id'], direction=row['side'], price_per_share=price_per_share, lots=lots_executed, lot_size=self.lot_size, executed_at=executed_at, commission=commission)
        async with db.acquire() as conn:
            await conn.execute("UPDATE grid_orders SET status='filled', exchange_order_id=$1, updated_at=NOW() WHERE order_id=$2 AND bot_id=$3", exchange_order_id, row['order_id'], self.bot_id)
        self.active_orders.pop(row['order_id'], None)
        if row['side'] == 'buy':
            self.initial_lots += lots_executed
            self.cash_remaining -= price_per_share * lots_executed * self.lot_size + commission
            if self.trading_paused:
                logger.info(f"бот {self.bot_id}: торговля на паузе — парная стоп-продажа на уровне {row['pair_level']} не выставляется (лоты сохранены в позиции)")
            else:
                await self._ensure_counter_order(client, 'sell', row['pair_level'], lots_executed)
        else:
            self.initial_lots -= lots_executed
            self.cash_remaining += price_per_share * lots_executed * self.lot_size - commission
            if self.trading_paused:
                logger.info(f"бот {self.bot_id}: тейк-профит исполнен на паузе — прибыль зафиксирована, парная стоп-покупка на уровне {row['pair_level']} не выставляется")
            else:
                await self._ensure_counter_order(client, 'buy', row['pair_level'], lots_executed)
        async with db.acquire() as conn:
            await conn.execute('UPDATE grid_bots SET initial_lots=$1, cash_remaining=$2 WHERE id=$3', self.initial_lots, self.cash_remaining, self.bot_id)
        logger.info(f"бот {self.bot_id}: исполнена {row['side']} заявка уровень={row['level_idx']} цена={price_per_share:.4f} лотов={lots_executed}")

    async def _handle_stop_order_gone(self, client: httpx.AsyncClient, row: dict, status: str):
        db_status = 'canceled' if status == 'STOP_ORDER_STATUS_CANCELED' else 'expired'
        async with db.acquire() as conn:
            await conn.execute('UPDATE grid_orders SET status=$1, updated_at=NOW() WHERE order_id=$2 AND bot_id=$3', db_status, row['order_id'], self.bot_id)
        self.active_orders.pop(row['order_id'], None)
        logger.warning(f"бот {self.bot_id}: заявка {row['order_id']} ({row['side']} уровень {row['level_idx']}) статус {status}, восстанавливаю на том же уровне")
        await self._place_stop_order(client, row['level_idx'], row['lots'], row['side'])

    async def _sync_stop_orders(self, client: httpx.AsyncClient):
        stop_orders = await self._get_stop_orders(client, 'STOP_ORDER_STATUS_ALL')
        by_id = {so['stopOrderId']: so for so in stop_orders if so.get('stopOrderId')}
        async with db.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM grid_orders WHERE bot_id=$1 AND status='active'", self.bot_id)
        for record in rows:
            row = dict(record)
            broker_order = by_id.get(row['order_id'])
            if broker_order is None:
                logger.warning(f"бот {self.bot_id}: стоп-заявка {row['order_id']} не найдена у брокера")
                await self._handle_stop_order_gone(client, row, 'STOP_ORDER_STATUS_CANCELED')
                continue
            status = broker_order.get('status')
            if status == 'STOP_ORDER_STATUS_ACTIVE':
                self.active_orders[row['order_id']] = {'side': row['side'], 'level_idx': row['level_idx'], 'lots': row['lots'], 'pair': row['pair_level']}
            elif status == 'STOP_ORDER_STATUS_EXECUTED':
                await self._handle_stop_order_executed(client, row, broker_order)
            elif status in ('STOP_ORDER_STATUS_CANCELED', 'STOP_ORDER_STATUS_EXPIRED'):
                await self._handle_stop_order_gone(client, row, status)

    async def _audit_positions(self, client: httpx.AsyncClient):
        try:
            positions = await self._get_positions(client)
        except Exception as e:
            logger.error(f'бот {self.bot_id}: ошибка GetPositions: {e}')
            return
        held_lots = 0
        for sec in positions.get('securities', []):
            if sec.get('figi') == self.figi or (self.instrument_uid and sec.get('instrumentUid') == self.instrument_uid):
                held_lots = int(sec.get('balance', 0)) // max(self.lot_size, 1)
        async with db.acquire() as conn:
            active_total = await conn.fetchval("SELECT COUNT(*) FROM grid_orders WHERE bot_id=$1 AND status='active'", self.bot_id)
            active_buy = await conn.fetchval("SELECT COUNT(*) FROM grid_orders WHERE bot_id=$1 AND status='active' AND side='buy'", self.bot_id)
            active_sell = await conn.fetchval("SELECT COUNT(*) FROM grid_orders WHERE bot_id=$1 AND status='active' AND side='sell'", self.bot_id)
        max_lots = None
        if self.current_price:
            max_lots = int(self.capital / self.current_price / max(self.lot_size, 1))
        logger.info(f'бот {self.bot_id}: активных заявок={active_total} (buy={active_buy}, sell={active_sell}), лотов на бирже={held_lots}, лотов в учёте бота={self.initial_lots}, максимум лотов из капитала={max_lots}')
        if held_lots != self.initial_lots:
            logger.warning(f'бот {self.bot_id}: расхождение позиции — на бирже {held_lots}, в учёте бота {self.initial_lots}')

    async def _get_held_lots(self, client: httpx.AsyncClient) -> int:
        positions = await self._get_positions(client)
        for sec in positions.get('securities', []) or []:
            if sec.get('figi') == self.figi or (self.instrument_uid and sec.get('instrumentUid') == self.instrument_uid):
                balance = int(sec.get('balance', 0) or 0)
                return balance // max(self.lot_size, 1)
        return 0

    async def _get_foreign_stop_orders(self, client: httpx.AsyncClient) -> List[dict]:
        async with db.acquire() as conn:
            rows = await conn.fetch("SELECT order_id FROM grid_orders WHERE bot_id=$1 AND status='active'", self.bot_id)
        known = {r['order_id'] for r in rows}
        broker_orders = await self._get_stop_orders(client, 'STOP_ORDER_STATUS_ACTIVE')
        foreign = []
        for so in broker_orders:
            sid = so.get('stopOrderId')
            if not sid or sid in known:
                continue
            same_instr = so.get('figi') == self.figi or (self.instrument_uid and so.get('instrumentUid') == self.instrument_uid)
            if same_instr:
                foreign.append(so)
        return foreign

    async def _startup_safety_check(self, client: httpx.AsyncClient, price: float) -> None:
        try:
            foreign = await self._get_foreign_stop_orders(client)
        except Exception as e:
            logger.warning(f'бот {self.bot_id}: не удалось проверить стоп-заявки на счёте: {e}')
            foreign = []
        if foreign:
            if settings.reset_orders_on_start:
                logger.warning(f'бот {self.bot_id}: RESET_ORDERS_ON_START=true — отменяю {len(foreign)} старых стоп-заявок по {self.ticker}')
                for so in foreign:
                    try:
                        await self._cancel_stop_order(client, so['stopOrderId'])
                    except Exception as e:
                        logger.error(f"бот {self.bot_id}: не удалось отменить стоп-заявку {so.get('stopOrderId')}: {e}")
            else:
                raise RuntimeError(f'На счёте уже есть {len(foreign)} активных стоп-заявок по инструменту {self.ticker}, не относящихся к этому запуску бота (вероятно, остались от предыдущего деплоя или выставлены вручную). Чтобы избежать двойной торговли, запуск остановлен. Варианты: отменить заявки вручную в приложении Т-Инвестиций, либо задать RESET_ORDERS_ON_START=true — бот отменит их сам при старте.')
        try:
            held_lots = await self._get_held_lots(client)
        except Exception as e:
            logger.warning(f'бот {self.bot_id}: не удалось проверить позицию на счёте: {e}')
            return
        if held_lots > 0:
            if settings.allow_existing_position:
                self.initial_lots = held_lots
                self.cash_remaining = max(self.capital - held_lots * price * self.lot_size, 0.0)
                async with db.acquire() as conn:
                    await conn.execute('UPDATE grid_bots SET initial_lots=$1, cash_remaining=$2, updated_at=NOW() WHERE id=$3', self.initial_lots, self.cash_remaining, self.bot_id)
                logger.warning(f'бот {self.bot_id}: ALLOW_EXISTING_POSITION=true — принято {held_lots} лотов {self.ticker} как начальная позиция, расчётный остаток денег {self.cash_remaining:.2f} руб.')
            else:
                raise RuntimeError(f'На счёте уже есть позиция по инструменту {self.ticker} ({held_lots} лотов), а у бота начальная позиция пуста. Чтобы бот не покупал повторно, запуск остановлен. Варианты: продать позицию вручную, либо задать ALLOW_EXISTING_POSITION=true — бот примет её как свою начальную позицию и продолжит работу без повторной покупки.')

    async def initialize(self, client: httpx.AsyncClient):
        self.account_id = await self._get_open_account_id(client)
        instr = await self._get_instrument_by_ticker(client)
        self.figi = instr['figi']
        self.instrument_uid = instr.get('uid')
        self.exchange = _resolve_schedule_exchange(instr)
        self.lot_size = int(instr['lot'])
        min_step = instr['minPriceIncrement']
        self.step = int(min_step['units']) + int(min_step['nano']) / 1000000000.0
        if self.step == 0:
            raise ValueError('Нулевой шаг цены')
        if not await self.is_exchange_open(client):
            raise ExchangeClosedError(f"Биржа {self.exchange or ''} сейчас закрыта — запуск отложен до открытия")
        self.d = (self.P_high - self.P_low) / self.N
        self.levels = [self.P_low + i * self.d for i in range(self.N + 1)]
        price = await self._get_last_price(client)
        if price is None:
            raise RuntimeError('Не удалось получить текущую цену')
        self.current_price = price
        logger.info(f'Текущая цена {self.ticker}: {price:.2f} руб.')
        if self.initial_lots == 0:
            await self._startup_safety_check(client, price)
        if not self.P_low <= price <= self.P_high:
            logger.warning(f'бот {self.bot_id}: цена {price:.2f} вне диапазона [{self.P_low:.2f}; {self.P_high:.2f}] — запуск в режиме мониторинга без торговли. Сетка развернётся автоматически при возврате цены.')
            self.trading_paused = True
            async with db.acquire() as conn:
                await conn.execute("UPDATE grid_bots SET status='active', current_price=$1, updated_at=NOW() WHERE id=$2", self.current_price, self.bot_id)
            return
        await self._deploy_grid(client, price)

    async def _deploy_grid(self, client: httpx.AsyncClient, price: float):
        if not license_manager.is_active():
            self._license_hold = True
            self.trading_paused = True
            logger.warning('бот %s: лицензия неактивна (%s) — сетка не разворачивается, торговля на паузе (новые заявки не выставляются)', self.bot_id, license_manager.reason)
            return
        i_cur = 0
        for i in range(self.N):
            if self.levels[i] <= price < self.levels[i + 1]:
                i_cur = i
                break
        logger.info(f'Текущий уровень сетки: {self.levels[i_cur]:.2f} (индекс {i_cur})')
        buy_levels = list(range(0, i_cur))
        sell_levels = list(range(i_cur + 1, self.N + 1))
        try:
            await self._sync_stop_orders(client)
        except Exception as e:
            logger.warning(f'бот {self.bot_id}: не удалось синхронизировать заявки перед разворачиванием сетки: {e}')
        occupied_buy = {o['level_idx'] for o in self.active_orders.values() if o['side'] == 'buy'}
        occupied_sell = {o['level_idx'] for o in self.active_orders.values() if o['side'] == 'sell'}
        if occupied_buy or occupied_sell:
            logger.info(f'бот {self.bot_id}: уже есть живые заявки — уровни покупки {sorted(occupied_buy)}, уровни продажи {sorted(occupied_sell)}; на них новые заявки не выставляются')
        buy_levels = [i for i in buy_levels if i not in occupied_buy]
        sell_levels = [i for i in sell_levels if i not in occupied_sell]
        reserved_sell_lots = sum((int(o.get('lots', 0)) for o in self.active_orders.values() if o['side'] == 'sell'))
        available_lots = max(self.initial_lots - reserved_sell_lots, 0)
        projected_lots = 0
        if self.initial_lots == 0:
            available_cash = self.cash_remaining if self.cash_remaining > 0 else self.capital
            amount_to_spend = 0.5 * available_cash
            min_cost = 1 * price * self.lot_size
            if amount_to_spend < min_cost:
                raise ValueError(f'Недостаточно капитала для покупки одного лота. Требуется минимум {min_cost:.2f} руб., доступно {amount_to_spend:.2f} руб.')
            projected_lots = int(amount_to_spend / price / self.lot_size)
            if projected_lots == 0:
                raise ValueError('Недостаточно капитала для покупки хотя бы 1 лота')
            projected_cash_remaining = available_cash - projected_lots * price * self.lot_size
            if buy_levels:
                money_per_buy = projected_cash_remaining / len(buy_levels)
                if int(money_per_buy / price / self.lot_size) < 1:
                    max_buy_levels = int(projected_cash_remaining / (price * self.lot_size))
                    raise ValueError(f'Слишком много уровней ниже текущей цены ({len(buy_levels)}) для доступного капитала после первоначальной покупки ({projected_cash_remaining:.2f} руб.). На каждый уровень покупки не хватает средств хотя бы на 1 лот. Максимум уровней ниже цены при N={self.N}: {max_buy_levels}. Уменьшите количество уровней (N) или увеличьте капитал.')
            if sell_levels:
                if projected_lots // len(sell_levels) == 0:
                    raise ValueError(f'Слишком много уровней выше текущей цены ({len(sell_levels)}) для количества лотов, которое будет куплено ({projected_lots}). Максимум уровней выше цены: {projected_lots}. Уменьшите количество уровней (N) или увеличьте капитал.')
        else:
            if buy_levels:
                affordable_buy_levels = int(self.cash_remaining / (price * self.lot_size))
                if affordable_buy_levels < len(buy_levels):
                    logger.warning(f'бот {self.bot_id}: денег ({self.cash_remaining:.2f} руб.) хватает на {affordable_buy_levels} уровней покупки из {len(buy_levels)} — выставляю заявки только на ближайшие к цене уровни')
                    buy_levels = buy_levels[-affordable_buy_levels:] if affordable_buy_levels > 0 else []
            if sell_levels:
                if available_lots < len(sell_levels):
                    logger.warning(f'бот {self.bot_id}: свободных лотов {available_lots} (всего {self.initial_lots}, зарезервировано под живые стоп-продажи {reserved_sell_lots}) — меньше, чем уровней продажи ({len(sell_levels)}). Выставляю заявки только на ближайшие к цене уровни')
                    sell_levels = sell_levels[:available_lots] if available_lots > 0 else []
        if self.initial_lots == 0:
            lots = projected_lots
            logger.info(f'Покупаем {lots} лотов {self.ticker} (1 лот = {self.lot_size} акций) ...')
            order_id = str(uuid.uuid4())
            try:
                resp = await self._post_order_async(client=client, instrument_id=self.figi, quantity=lots, direction='ORDER_DIRECTION_BUY', order_type='ORDER_TYPE_MARKET', order_id=order_id, confirm_margin_trade=True)
            except Exception as e:
                if _is_exchange_closed_error(e):
                    raise ExchangeClosedError(str(e))
                raise
            order_req_id = resp.get('orderRequestId')
            if not order_req_id:
                raise RuntimeError('Не получен orderRequestId рыночной заявки')
            filled = False
            for _ in range(90):
                state = await self._get_order_state(client, order_req_id)
                if state is None:
                    await asyncio.sleep(2)
                    continue
                status = state.get('executionReportStatus')
                executed = int(state.get('lotsExecuted', 0))
                logger.info(f'Статус рыночной покупки: {status}, исполнено лотов: {executed}')
                if status == 'EXECUTION_REPORT_STATUS_FILL' and executed == lots:
                    price_per_share, commission, _ = self._extract_execution_details(state)
                    executed_at = datetime.utcnow()
                    await self._save_trade(order_id=order_req_id, direction='buy', price_per_share=price_per_share, lots=lots, lot_size=self.lot_size, executed_at=executed_at, commission=commission)
                    total_cost = price_per_share * lots * self.lot_size + commission
                    self.initial_lots = lots
                    self.cash_remaining = self.capital - total_cost
                    logger.info(f'Исполнено: цена акции {price_per_share:.2f}, лотов {lots}, остаток денег: {self.cash_remaining:.2f} руб.')
                    filled = True
                    break
                if status in ('EXECUTION_REPORT_STATUS_CANCELLED', 'EXECUTION_REPORT_STATUS_REJECTED'):
                    raise RuntimeError(f'Рыночная заявка не исполнена, статус: {status}')
                await asyncio.sleep(2)
            if not filled:
                raise RuntimeError('Рыночная заявка не подтвердилась за отведённое время')
            async with db.acquire() as conn:
                await conn.execute("UPDATE grid_bots SET initial_lots=$1, cash_remaining=$2, current_price=$3, status='active' WHERE id=$4", self.initial_lots, self.cash_remaining, self.current_price, self.bot_id)
        else:
            async with db.acquire() as conn:
                await conn.execute("UPDATE grid_bots SET status='active', current_price=$1 WHERE id=$2", self.current_price, self.bot_id)
        if buy_levels:
            money_per_buy = self.cash_remaining / len(buy_levels)
            logger.info(f'Стоп-заявки на покупку на {len(buy_levels)} уровнях (по ~{money_per_buy:.2f} руб.)')
            for idx in buy_levels:
                level_price = self.levels[idx]
                lots_to_buy = int(money_per_buy / level_price / self.lot_size)
                if lots_to_buy <= 0:
                    logger.warning(f'бот {self.bot_id}: уровень покупки {idx} (цена {level_price:.2f}) получил 0 лотов при распределении капитала — пропущен.')
                    continue
                await self._place_stop_buy(client, idx, lots_to_buy)
        else:
            logger.info('Нет уровней ниже текущей цены для стоп-покупок.')
        if sell_levels:
            free_lots = max(self.initial_lots - reserved_sell_lots, 0)
            lots_per_sell = free_lots // len(sell_levels)
            if lots_per_sell == 0:
                if reserved_sell_lots > 0 or self.trading_paused:
                    logger.warning(f'бот {self.bot_id}: свободных лотов ({free_lots}) не хватает на {len(sell_levels)} уровней продажи — новые стоп-продажи не выставляются, действующие тейк-профиты остаются в силе')
                    sell_levels = []
                else:
                    raise RuntimeError(f'Недостаточно лотов ({self.initial_lots}) для распределения по {len(sell_levels)} уровням продажи. Заявки не выставлены, бот не запущен.')
            if sell_levels:
                logger.info(f'Стоп-заявки на продажу на {len(sell_levels)} уровнях (по {lots_per_sell} лотов)')
                for idx in sell_levels:
                    await self._place_stop_sell(client, idx, lots_per_sell)
        else:
            logger.info('Нет уровней выше текущей цены для стоп-продаж.')
        self.trading_paused = False
        logger.info('Инициализация завершена.')

    async def resume(self, client: httpx.AsyncClient):
        self.account_id = await self._get_open_account_id(client)
        instr = await self._get_instrument_by_ticker(client)
        self.figi = instr['figi']
        self.instrument_uid = instr.get('uid')
        self.exchange = _resolve_schedule_exchange(instr)
        self.lot_size = int(instr['lot'])
        min_step = instr['minPriceIncrement']
        self.step = int(min_step['units']) + int(min_step['nano']) / 1000000000.0
        self.d = (self.P_high - self.P_low) / self.N
        self.levels = [self.P_low + i * self.d for i in range(self.N + 1)]
        price = await self._get_last_price(client)
        if price is not None:
            self.current_price = price
        await self._sync_stop_orders(client)
        await self._audit_positions(client)
        if price is not None and (not self.P_low <= price <= self.P_high):
            logger.warning(f'бот {self.bot_id}: возобновление при цене {price:.2f} вне диапазона [{self.P_low:.2f}; {self.P_high:.2f}] — режим мониторинга без торговли')
            self.trading_paused = True
        elif not self.active_orders:
            logger.info(f'бот {self.bot_id}: активных заявок нет — сетка будет развёрнута заново при цене внутри диапазона')
            self.trading_paused = True

    async def _pause_trading(self, client: httpx.AsyncClient):
        self.trading_paused = True
        if not self.active_orders:
            async with db.acquire() as conn:
                rows = await conn.fetch("SELECT * FROM grid_orders WHERE bot_id=$1 AND status='active'", self.bot_id)
            for row in rows:
                self.active_orders[row['order_id']] = {'side': row['side'], 'level_idx': row['level_idx'], 'lots': row['lots'], 'pair': row['pair_level']}
        buy_left = sum((1 for o in self.active_orders.values() if o['side'] == 'buy'))
        sell_left = sum((1 for o in self.active_orders.values() if o['side'] == 'sell'))
        logger.info(f'бот {self.bot_id}: торговля на паузе, действующие заявки СОХРАНЕНЫ (buy={buy_left}, sell={sell_left}), позиция сохранена (лотов={self.initial_lots}, остаток={self.cash_remaining:.2f} руб.). Новые заявки не выставляются до возврата цены в диапазон.')

    async def _monitor_loop(self):
        from app.services.grid_bot.service import GridBotService
        async with httpx.AsyncClient(verify=CA_BUNDLE_PATH, timeout=30.0) as client:
            while not self._stop_flag:
                try:
                    if not license_manager.is_active():
                        if not self._license_hold or not self.trading_paused:
                            logger.warning(f'бот {self.bot_id}: лицензия неактивна ({license_manager.reason}) — торговля на паузе, новые заявки не выставляются')
                        self._license_hold = True
                        self.trading_paused = True
                    else:
                        self._license_hold = False
                    price = await self._get_last_price(client)
                    if price is not None:
                        self.current_price = price
                        async with db.acquire() as conn:
                            await conn.execute('UPDATE grid_bots SET current_price=$1 WHERE id=$2', price, self.bot_id)
                        if price >= self.P_high or price <= self.P_low:
                            if not self.trading_paused:
                                logger.warning(f'бот {self.bot_id}: цена {price} вышла за границы сетки [{self.P_low:.2f}; {self.P_high:.2f}] — торговля приостановлена, мониторинг продолжается')
                                await self._pause_trading(client)
                        elif self.trading_paused and (not self._license_hold) and (self.P_low < price < self.P_high):
                            now_utc = datetime.now(timezone.utc)
                            if self._last_deploy_attempt is None or (now_utc - self._last_deploy_attempt).total_seconds() >= 60:
                                self._last_deploy_attempt = now_utc
                                logger.info(f'бот {self.bot_id}: цена {price} вернулась в диапазон [{self.P_low:.2f}; {self.P_high:.2f}] — разворачиваю сетку заново')
                                try:
                                    if not await self.is_exchange_open(client):
                                        logger.info(f'бот {self.bot_id}: цена в диапазоне, но биржа закрыта — сетка будет развёрнута после открытия торгов')
                                    else:
                                        await self._deploy_grid(client, price)
                                        logger.info(f'бот {self.bot_id}: торговля возобновлена')
                                except ExchangeClosedError as e:
                                    logger.info(f'бот {self.bot_id}: биржа закрыта, сетка будет развёрнута после открытия: {e}')
                                except Exception as e:
                                    logger.error(f'бот {self.bot_id}: не удалось развернуть сетку после возврата цены в диапазон: {e}. Повтор через минуту.')
                    await self._sync_stop_orders(client)
                    await self._audit_positions(client)
                    if datetime.now(timezone.utc).minute % 5 == 0:
                        try:
                            await GridBotService.sync_commissions(self.bot_id)
                        except Exception as e:
                            logger.warning(f'бот {self.bot_id}: не удалось синхронизировать комиссии: {e}')
                except Exception as e:
                    logger.error(f'бот {self.bot_id}: ошибка в цикле мониторинга: {e}')
                await asyncio.sleep(5)

    async def start_monitoring(self):
        if self.monitor_task is None or self.monitor_task.done():
            self._stop_flag = False
            self.monitor_task = asyncio.create_task(self._monitor_loop())
            logger.info(f'Запущен мониторинг для бота {self.bot_id}')

    async def stop(self):
        self._stop_flag = True
        if self.monitor_task:
            self.monitor_task.cancel()
            try:
                await self.monitor_task
            except asyncio.CancelledError:
                pass
            self.monitor_task = None
        async with httpx.AsyncClient(verify=CA_BUNDLE_PATH, timeout=30.0) as client:
            if not self.account_id:
                self.account_id = await self._get_open_account_id(client)
            if not self.active_orders:
                async with db.acquire() as conn:
                    rows = await conn.fetch("SELECT * FROM grid_orders WHERE bot_id=$1 AND status='active'", self.bot_id)
                for row in rows:
                    self.active_orders[row['order_id']] = {'side': row['side'], 'level_idx': row['level_idx'], 'lots': row['lots'], 'pair': row['pair_level']}
            for stop_order_id in list(self.active_orders.keys()):
                try:
                    await self._cancel_stop_order(client, stop_order_id)
                except Exception as e:
                    logger.error(f'Ошибка при отмене ордера {stop_order_id}: {e}')
                async with db.acquire() as conn:
                    await conn.execute("UPDATE grid_orders SET status='cancelled', updated_at=NOW() WHERE order_id=$1 AND bot_id=$2", stop_order_id, self.bot_id)
            self.active_orders.clear()
        async with db.acquire() as conn:
            await conn.execute("UPDATE grid_bots SET status='stopped', updated_at=NOW() WHERE id=$1", self.bot_id)
        logger.info(f'Бот {self.bot_id} остановлен.')

    async def delete(self):
        await self.stop()
        async with db.acquire() as conn:
            await conn.execute('DELETE FROM grid_bots WHERE id=$1', self.bot_id)

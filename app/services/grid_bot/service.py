import asyncio
import uuid
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional, List, Set
import httpx
from app.config import settings, TICKER1_TO_FIGIA
from app.database import db
from app.services.grid_bot.models import GridBotCreate, GridBotLevelsEstimateRequest
from app.services.grid_bot.config import CA_BUNDLE_PATH
from app.services.grid_bot.exceptions import BotOperationInProgress, ExchangeClosedError, _is_exchange_closed_error
from app.services.grid_bot.exchange_utils import _resolve_schedule_exchange
from app.services.grid_bot.instance import GridBotInstance
logger = logging.getLogger(__name__)
_active_bots: Dict[int, 'GridBotInstance'] = {}
_waiting_bots: Dict[int, dict] = {}
_waiting_lock = asyncio.Lock()
_waiting_scheduler_task: Optional[asyncio.Task] = None
_bot_ops_in_progress: Set[int] = set()
_bot_ops_lock = asyncio.Lock()

@asynccontextmanager
async def bot_operation_guard(bot_id: int):
    async with _bot_ops_lock:
        if bot_id in _bot_ops_in_progress:
            raise BotOperationInProgress('По этому боту уже выполняется операция. Дождитесь её завершения.')
        _bot_ops_in_progress.add(bot_id)
    try:
        yield
    finally:
        async with _bot_ops_lock:
            _bot_ops_in_progress.discard(bot_id)

class GridBotService:

    @staticmethod
    async def sync_commissions(bot_id: int) -> dict:
        bot_data = await GridBotService.get_bot(bot_id)
        if not bot_data:
            raise ValueError('Бот не найден')
        async with db.acquire() as conn:
            trades = await conn.fetch('SELECT id, order_id, direction, lots, lot_size, executed_at, commission FROM trade_history WHERE bot_id=$1 ORDER BY executed_at ASC', bot_id)
        if not trades:
            return {'updated': 0, 'matched': 0}
        token = await GridBotService._get_user_token(bot_data['user_id'])
        instance = GridBotService._instance_from_bot_data(bot_data, token)
        oldest = min((t['executed_at'] for t in trades))
        if oldest.tzinfo is None:
            oldest = oldest.replace(tzinfo=timezone.utc)
        from_dt = oldest - timedelta(days=2)
        to_dt = datetime.now(timezone.utc) + timedelta(minutes=5)
        async with httpx.AsyncClient(verify=CA_BUNDLE_PATH, timeout=30.0) as client:
            instance.account_id = await instance._get_open_account_id(client)
            instr = await instance._get_instrument_by_ticker(client)
            instance.figi = instr['figi']
            instance.lot_size = int(instr['lot'])
            operations = await instance._get_operations(client, from_dt, to_dt)
        fees: List[dict] = []
        deals: List[dict] = []
        for op in operations:
            op_type = str(op.get('operationType') or '').upper()
            op_dt = GridBotInstance._parse_api_dt(op.get('date'))
            if op_dt is None:
                continue
            payment = abs(GridBotInstance._money_to_float(op.get('payment')))
            if 'BROKER_FEE' in op_type:
                fees.append({'dt': op_dt, 'amount': payment, 'used': False})
            elif op_type in ('OPERATION_TYPE_BUY', 'OPERATION_TYPE_SELL'):
                deals.append({'dt': op_dt, 'direction': 'buy' if 'BUY' in op_type else 'sell', 'quantity': int(op.get('quantity') or 0), 'amount': payment})
        updated = 0
        matched = 0
        for t in trades:
            executed_at = t['executed_at']
            if executed_at.tzinfo is None:
                executed_at = executed_at.replace(tzinfo=timezone.utc)
            shares = int(t['lots']) * int(t['lot_size'] or 1)
            deal = None
            best = timedelta(minutes=15)
            for d in deals:
                if d['direction'] != t['direction']:
                    continue
                if d['quantity'] and d['quantity'] != shares:
                    continue
                delta = abs(d['dt'] - executed_at)
                if delta < best:
                    best = delta
                    deal = d
            if deal is None:
                continue
            fee_amount = None
            best_fee = timedelta(minutes=10)
            fee_ref = None
            for f in fees:
                if f['used']:
                    continue
                delta = abs(f['dt'] - deal['dt'])
                if delta < best_fee:
                    best_fee = delta
                    fee_ref = f
            if fee_ref is None:
                continue
            fee_ref['used'] = True
            fee_amount = fee_ref['amount']
            matched += 1
            if abs(float(t['commission'] or 0.0) - fee_amount) < 1e-09:
                continue
            async with db.acquire() as conn:
                await conn.execute('UPDATE trade_history SET commission=$1 WHERE id=$2', fee_amount, t['id'])
            updated += 1
        logger.info(f'бот {bot_id}: синхронизация комиссий — сопоставлено {matched}, обновлено {updated}')
        return {'updated': updated, 'matched': matched}

    @staticmethod
    async def _get_user_token(user_id: str) -> str:
        token = settings.tinkoff_token
        if not token:
            logger.warning('Токен Т-Инвестиций не задан в переменных окружения')
            raise ValueError('Токен Т-Инвестиций не задан. Укажите переменную окружения TINKOFF_TOKEN.')
        return token

    @staticmethod
    def _instance_from_bot_data(bot_data: dict, token: str) -> GridBotInstance:
        figi = TICKER1_TO_FIGIA.get(bot_data['ticker'])
        return GridBotInstance(bot_id=bot_data['id'], user_id=bot_data['user_id'], tinkoff_token=token, ticker=bot_data['ticker'], class_code=bot_data['class_code'], P_low=float(bot_data['p_low']), P_high=float(bot_data['p_high']), capital=float(bot_data['capital']), N=int(bot_data['n']), initial_lots=int(bot_data.get('initial_lots') or 0), cash_remaining=float(bot_data.get('cash_remaining') or 0.0), current_price=float(bot_data['current_price']) if bot_data.get('current_price') is not None else None, figi=figi)

    @staticmethod
    async def estimate_max_levels(data: GridBotLevelsEstimateRequest) -> dict:
        if data.P_high <= data.P_low:
            raise ValueError('Верхняя граница должна быть больше нижней')
        if data.capital <= 0:
            raise ValueError('Капитал должен быть положительным числом')
        token = await GridBotService._get_user_token(data.user_id)
        probe = GridBotInstance(bot_id=0, user_id=data.user_id, tinkoff_token=token, ticker=data.ticker, class_code=data.class_code, P_low=data.P_low, P_high=data.P_high, capital=data.capital, N=1, initial_lots=0, cash_remaining=0.0)
        async with httpx.AsyncClient(verify=CA_BUNDLE_PATH, timeout=30.0) as client:
            probe.account_id = await probe._get_open_account_id(client)
            instr = await probe._get_instrument_by_ticker(client)
            probe.figi = instr['figi']
            lot_size = int(instr['lot'])
            min_step = instr['minPriceIncrement']
            step = int(min_step['units']) + int(min_step['nano']) / 1000000000.0
            price = await probe._get_last_price(client)
        if price is None:
            raise RuntimeError('Не удалось получить текущую цену инструмента')
        if lot_size <= 0:
            raise ValueError('Некорректный размер лота инструмента')
        if not data.P_low <= price <= data.P_high:
            raise ValueError(f'Текущая цена ({price:.2f} руб.) вне заданного диапазона [{data.P_low:.2f}; {data.P_high:.2f}]')
        amount_to_spend = 0.5 * data.capital
        min_cost = price * lot_size
        initial_lots = int(amount_to_spend / price / lot_size)
        if initial_lots < 1:
            return {'current_price': price, 'lot_size': lot_size, 'step': step, 'initial_lots': 0, 'cash_remaining_estimate': data.capital, 'max_levels': 0, 'reason': f'Капитала недостаточно для покупки даже одного лота по текущей цене. Требуется минимум {min_cost:.2f} руб. на покупку одного лота (при делении капитала пополам доступно {amount_to_spend:.2f} руб.).'}
        cash_remaining_est = data.capital - initial_lots * price * lot_size
        max_buy_levels = int(cash_remaining_est / (price * lot_size))
        max_sell_levels = initial_lots
        f = (price - data.P_low) / (data.P_high - data.P_low)
        f = min(max(f, 0.0), 1.0)
        max_levels = 0
        upper_bound_search = 2000
        for n_candidate in range(1, upper_bound_search + 1):
            i_cur = min(int(f * n_candidate), n_candidate - 1) if n_candidate > 0 else 0
            i_cur = max(i_cur, 0)
            buy_count = i_cur
            sell_count = n_candidate - i_cur
            buy_ok = buy_count == 0 or buy_count <= max_buy_levels
            sell_ok = sell_count == 0 or sell_count <= max_sell_levels
            if buy_ok and sell_ok:
                max_levels = n_candidate
        return {'current_price': price, 'lot_size': lot_size, 'step': step, 'initial_lots': initial_lots, 'cash_remaining_estimate': cash_remaining_est, 'max_levels': max_levels}

    @staticmethod
    async def create_bot(data: GridBotCreate) -> int:
        await GridBotService._get_user_token(data.user_id)
        if data.N < 1:
            raise ValueError('Количество уровней (N) должно быть не менее 1')
        if data.P_high <= data.P_low:
            raise ValueError('Верхняя граница должна быть больше нижней')
        if data.capital <= 0:
            raise ValueError('Капитал должен быть положительным числом')
        estimate = await GridBotService.estimate_max_levels(GridBotLevelsEstimateRequest(user_id=data.user_id, ticker=data.ticker, class_code=data.class_code, P_low=data.P_low, P_high=data.P_high, capital=data.capital))
        if data.N > estimate['max_levels']:
            raise ValueError(f"Слишком много уровней ({data.N}) для указанного капитала. При текущей цене ({estimate['current_price']:.2f} руб.) максимально допустимое количество уровней: {estimate['max_levels']}. Уменьшите количество уровней или увеличьте капитал.")
        async with db.acquire() as conn:
            row = await conn.fetchrow("INSERT INTO grid_bots (user_id, ticker, class_code, p_low, p_high, capital, n, status, type) VALUES ($1, $2, $3, $4, $5, $6, $7, 'created', 'simple') RETURNING id", data.user_id, data.ticker, data.class_code, data.P_low, data.P_high, data.capital, data.N)
            return row['id']

    @staticmethod
    async def get_bot(bot_id: int) -> Optional[dict]:
        async with db.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM grid_bots WHERE id=$1 AND (type IS NULL OR type='simple')", bot_id)
            return dict(row) if row else None

    @staticmethod
    async def get_bots_by_user(user_id: str) -> List[dict]:
        async with db.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM grid_bots WHERE user_id = $1 AND (type IS NULL OR type='simple') AND status != 'archived' ORDER BY id DESC", user_id)
            return [dict(r) for r in rows]

    @staticmethod
    async def get_active_orders(bot_id: int) -> List[dict]:
        async with db.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM grid_orders WHERE bot_id=$1 AND status='active'", bot_id)
            return [dict(r) for r in rows]

    @staticmethod
    async def _do_start_bot(bot_id: int) -> str:
        bot_data = await GridBotService.get_bot(bot_id)
        if not bot_data:
            raise ValueError('Бот не найден')
        if bot_data['status'] == 'archived':
            raise ValueError('Невозможно запустить архивированный бот')
        if bot_id in _active_bots:
            return 'started'
        token = await GridBotService._get_user_token(bot_data['user_id'])
        instance = GridBotService._instance_from_bot_data(bot_data, token)
        async with httpx.AsyncClient(verify=CA_BUNDLE_PATH, timeout=30.0) as client:
            try:
                if bot_data['status'] == 'active' and instance.initial_lots > 0:
                    await instance.resume(client)
                else:
                    await instance.initialize(client)
            except ExchangeClosedError as e:
                logger.info(f'бот {bot_id}: биржа закрыта, откладываю запуск: {e}')
                await GridBotService._enqueue_waiting(bot_id, bot_data)
                return 'waiting'
            except Exception as e:
                if _is_exchange_closed_error(e):
                    logger.info(f'бот {bot_id}: биржа закрыта (по ответу брокера), откладываю запуск: {e}')
                    await GridBotService._enqueue_waiting(bot_id, bot_data)
                    return 'waiting'
                logger.error(f'Ошибка запуска бота {bot_id}: {e}')
                raise ValueError(f'Ошибка инициализации: {str(e)}')
        await instance.start_monitoring()
        _active_bots[bot_id] = instance
        await GridBotService._dequeue_waiting(bot_id)
        return 'started'

    @staticmethod
    async def start_bot(bot_id: int) -> dict:
        result = await GridBotService._do_start_bot(bot_id)
        if result == 'waiting':
            opens_at = _waiting_bots.get(bot_id, {}).get('opens_at')
            return {'status': 'waiting', 'opens_at': opens_at, 'message': 'Биржа закрыта. Бот поставлен в очередь и запустится автоматически после открытия торгов.'}
        return {'status': 'started', 'message': 'Бот запущен'}

    @staticmethod
    async def _enqueue_waiting(bot_id: int, bot_data: dict) -> None:
        opens_at_iso: Optional[str] = None
        exchange: Optional[str] = None
        try:
            token = await GridBotService._get_user_token(bot_data['user_id'])
            instance = GridBotService._instance_from_bot_data(bot_data, token)
            async with httpx.AsyncClient(verify=CA_BUNDLE_PATH, timeout=30.0) as client:
                try:
                    instr = await instance._get_instrument_by_ticker(client)
                    instance.exchange = _resolve_schedule_exchange(instr)
                    exchange = instance.exchange
                except Exception:
                    pass
                nxt = await instance.next_exchange_open_dt(client)
                if nxt is not None:
                    opens_at_iso = nxt.strftime('%Y-%m-%dT%H:%M:%SZ')
        except Exception as e:
            logger.warning(f'бот {bot_id}: не удалось определить время открытия биржи: {e}')
        async with _waiting_lock:
            _waiting_bots[bot_id] = {'user_id': bot_data['user_id'], 'exchange': exchange, 'opens_at': opens_at_iso, 'enqueued_at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}
        async with db.acquire() as conn:
            await conn.execute("UPDATE grid_bots SET status='waiting', updated_at=NOW() WHERE id=$1", bot_id)
        GridBotService.ensure_waiting_scheduler()

    @staticmethod
    async def _dequeue_waiting(bot_id: int) -> None:
        async with _waiting_lock:
            _waiting_bots.pop(bot_id, None)

    @staticmethod
    def ensure_waiting_scheduler() -> None:
        global _waiting_scheduler_task
        if _waiting_scheduler_task is None or _waiting_scheduler_task.done():
            _waiting_scheduler_task = asyncio.create_task(GridBotService._waiting_scheduler_loop())
            logger.info('Запущен планировщик очереди ожидания открытия биржи')

    @staticmethod
    async def _waiting_scheduler_loop(poll_seconds: int=30) -> None:
        while True:
            try:
                async with _waiting_lock:
                    pending_ids = list(_waiting_bots.keys())
                if not pending_ids:
                    logger.info('Очередь ожидания пуста, планировщик остановлен')
                    return
                for bot_id in pending_ids:
                    try:
                        bot_data = await GridBotService.get_bot(bot_id)
                        if not bot_data or bot_data['status'] == 'archived':
                            await GridBotService._dequeue_waiting(bot_id)
                            continue
                        token = await GridBotService._get_user_token(bot_data['user_id'])
                        instance = GridBotService._instance_from_bot_data(bot_data, token)
                        async with httpx.AsyncClient(verify=CA_BUNDLE_PATH, timeout=30.0) as client:
                            try:
                                instr = await instance._get_instrument_by_ticker(client)
                                instance.exchange = _resolve_schedule_exchange(instr)
                            except Exception:
                                pass
                            is_open = await instance.is_exchange_open(client)
                        if is_open:
                            logger.info(f'бот {bot_id}: биржа открылась, автоматический запуск')
                            result = await GridBotService._do_start_bot(bot_id)
                            if result == 'started':
                                await GridBotService._dequeue_waiting(bot_id)
                                logger.info(f'бот {bot_id}: автозапуск успешен, убран из очереди')
                    except Exception as e:
                        logger.error(f'бот {bot_id}: ошибка автозапуска из очереди: {e}')
            except Exception as e:
                logger.error(f'Ошибка в планировщике очереди ожидания: {e}')
            await asyncio.sleep(poll_seconds)

    @staticmethod
    def get_waiting_bots() -> Dict[int, dict]:
        return dict(_waiting_bots)

    @staticmethod
    async def stop_bot(bot_id: int) -> bool:
        bot_data = await GridBotService.get_bot(bot_id)
        if not bot_data:
            raise ValueError('Бот не найден')
        await GridBotService._dequeue_waiting(bot_id)
        if bot_data['status'] == 'waiting':
            async with db.acquire() as conn:
                await conn.execute("UPDATE grid_bots SET status='stopped', updated_at=NOW() WHERE id=$1", bot_id)
            return True
        instance = _active_bots.get(bot_id)
        if instance is None:
            if bot_data['status'] != 'active':
                return True
            token = await GridBotService._get_user_token(bot_data['user_id'])
            instance = GridBotService._instance_from_bot_data(bot_data, token)
            await instance.stop()
        else:
            await instance.stop()
            del _active_bots[bot_id]
        return True

    @staticmethod
    async def delete_bot(bot_id: int, sell_position: bool=False) -> dict:
        await GridBotService._dequeue_waiting(bot_id)
        bot_data = await GridBotService.get_bot(bot_id)
        if not bot_data:
            raise ValueError('Бот не найден')
        token = await GridBotService._get_user_token(bot_data['user_id'])
        instance = _active_bots.get(bot_id)
        if instance is None:
            instance = GridBotService._instance_from_bot_data(bot_data, token)
        try:
            instance._stop_flag = True
            if instance.monitor_task:
                instance.monitor_task.cancel()
                try:
                    await instance.monitor_task
                except asyncio.CancelledError:
                    pass
                instance.monitor_task = None
        except Exception as e:
            logger.error(f'бот {bot_id}: ошибка остановки мониторинга при удалении: {e}')
        canceled_orders = 0
        sold = False
        sold_lots = 0
        sell_error: Optional[str] = None
        async with httpx.AsyncClient(verify=CA_BUNDLE_PATH, timeout=30.0) as client:
            if not instance.account_id:
                try:
                    instance.account_id = await instance._get_open_account_id(client)
                except Exception as e:
                    logger.error(f'бот {bot_id}: не удалось получить счёт при удалении: {e}')
            try:
                instr = await instance._get_instrument_by_ticker(client)
                instance.figi = instr['figi']
                instance.instrument_uid = instr.get('uid')
                instance.exchange = _resolve_schedule_exchange(instr)
                instance.lot_size = int(instr['lot'])
            except Exception as e:
                logger.warning(f'бот {bot_id}: не удалось получить инструмент при удалении: {e}')
            canceled_orders = await GridBotService._cancel_all_stop_orders(client, instance)
            if sell_position:
                try:
                    sold_lots = await GridBotService._sell_all_position_market(client, instance)
                    sold = sold_lots > 0
                except ExchangeClosedError as e:
                    sell_error = 'Биржа закрыта — продать по рынку сейчас нельзя.'
                    logger.warning(f'бот {bot_id}: {sell_error} ({e})')
                except Exception as e:
                    sell_error = f'Не удалось продать актив: {e}'
                    logger.error(f'бот {bot_id}: {sell_error}')
        if bot_id in _active_bots:
            del _active_bots[bot_id]
        async with db.acquire() as conn:
            await conn.execute("UPDATE grid_bots SET status='archived', updated_at=NOW() WHERE id=$1", bot_id)
        logger.info(f'бот {bot_id} удалён: отменено заявок={canceled_orders}, продано лотов={sold_lots}, продажа_запрошена={sell_position}')
        return {'status': 'archived', 'canceledOrders': canceled_orders, 'soldPosition': sold, 'soldLots': sold_lots, 'sellError': sell_error}

    @staticmethod
    async def _cancel_all_stop_orders(client: httpx.AsyncClient, instance: 'GridBotInstance') -> int:
        canceled = 0
        order_ids: set = set()
        try:
            async with db.acquire() as conn:
                rows = await conn.fetch("SELECT order_id FROM grid_orders WHERE bot_id=$1 AND status='active'", instance.bot_id)
            for r in rows:
                if r['order_id']:
                    order_ids.add(r['order_id'])
        except Exception as e:
            logger.error(f'бот {instance.bot_id}: ошибка чтения активных заявок из БД: {e}')
        try:
            broker_orders = await instance._get_stop_orders(client, 'STOP_ORDER_STATUS_ACTIVE')
            for so in broker_orders:
                sid = so.get('stopOrderId')
                if not sid:
                    continue
                same_instr = so.get('figi') == instance.figi or (instance.instrument_uid and so.get('instrumentUid') == instance.instrument_uid)
                if same_instr:
                    order_ids.add(sid)
        except Exception as e:
            logger.warning(f'бот {instance.bot_id}: не удалось получить список стоп-заявок брокера: {e}')
        for sid in order_ids:
            try:
                ok = await instance._cancel_stop_order(client, sid)
                if ok:
                    canceled += 1
            except Exception as e:
                logger.error(f'бот {instance.bot_id}: ошибка отмены стоп-заявки {sid}: {e}')
            try:
                async with db.acquire() as conn:
                    await conn.execute("UPDATE grid_orders SET status='cancelled', updated_at=NOW() WHERE order_id=$1 AND bot_id=$2", sid, instance.bot_id)
            except Exception as e:
                logger.error(f'бот {instance.bot_id}: ошибка обновления статуса заявки {sid}: {e}')
        instance.active_orders.clear()
        return canceled

    @staticmethod
    async def _sell_all_position_market(client: httpx.AsyncClient, instance: 'GridBotInstance') -> int:
        if not instance.account_id:
            instance.account_id = await instance._get_open_account_id(client)
        held_lots = 0
        try:
            positions = await instance._get_positions(client)
            for sec in positions.get('securities', []) or []:
                if sec.get('figi') == instance.figi or (instance.instrument_uid and sec.get('instrumentUid') == instance.instrument_uid):
                    balance = int(sec.get('balance', 0) or 0)
                    held_lots = balance // max(instance.lot_size, 1)
        except Exception as e:
            logger.error(f'бот {instance.bot_id}: ошибка получения позиции для продажи: {e}')
            held_lots = max(int(instance.initial_lots or 0), 0)
        if held_lots <= 0:
            logger.info(f'бот {instance.bot_id}: продавать нечего (0 лотов)')
            return 0
        order_id = str(uuid.uuid4())
        try:
            resp = await instance._post_order_async(client=client, instrument_id=instance.figi, quantity=held_lots, direction='ORDER_DIRECTION_SELL', order_type='ORDER_TYPE_MARKET', order_id=order_id, confirm_margin_trade=False)
        except Exception as e:
            if _is_exchange_closed_error(e):
                raise ExchangeClosedError(str(e))
            raise
        order_req_id = resp.get('orderRequestId') or order_id
        for _ in range(90):
            state = await instance._get_order_state(client, order_req_id)
            if state is None:
                await asyncio.sleep(2)
                continue
            status = state.get('executionReportStatus')
            executed = int(state.get('lotsExecuted', 0))
            if status == 'EXECUTION_REPORT_STATUS_FILL' and executed >= held_lots:
                price_per_share, commission, _ = instance._extract_execution_details(state)
                await instance._save_trade(order_id=order_req_id, direction='sell', price_per_share=price_per_share, lots=held_lots, lot_size=instance.lot_size, executed_at=datetime.utcnow(), commission=commission)
                instance.initial_lots = max(instance.initial_lots - held_lots, 0)
                logger.info(f'бот {instance.bot_id}: продано по рынку {held_lots} лотов по цене {price_per_share:.2f}')
                break
            if status in ('EXECUTION_REPORT_STATUS_CANCELLED', 'EXECUTION_REPORT_STATUS_REJECTED'):
                raise RuntimeError(f'Рыночная продажа не исполнена, статус: {status}')
            await asyncio.sleep(2)
        try:
            async with db.acquire() as conn:
                await conn.execute('UPDATE grid_bots SET initial_lots=$1 WHERE id=$2', instance.initial_lots, instance.bot_id)
        except Exception:
            pass
        return held_lots

    @staticmethod
    async def get_position_summary(bot_id: int) -> dict:
        bot_data = await GridBotService.get_bot(bot_id)
        if not bot_data:
            raise ValueError('Бот не найден')
        token = await GridBotService._get_user_token(bot_data['user_id'])
        instance = _active_bots.get(bot_id) or GridBotService._instance_from_bot_data(bot_data, token)
        held_lots = 0
        lot_size = 1
        async with httpx.AsyncClient(verify=CA_BUNDLE_PATH, timeout=30.0) as client:
            try:
                if not instance.account_id:
                    instance.account_id = await instance._get_open_account_id(client)
                instr = await instance._get_instrument_by_ticker(client)
                instance.figi = instr['figi']
                instance.instrument_uid = instr.get('uid')
                lot_size = int(instr['lot'])
                instance.lot_size = lot_size
                positions = await instance._get_positions(client)
                for sec in positions.get('securities', []) or []:
                    if sec.get('figi') == instance.figi or (instance.instrument_uid and sec.get('instrumentUid') == instance.instrument_uid):
                        balance = int(sec.get('balance', 0) or 0)
                        held_lots = balance // max(lot_size, 1)
            except Exception as e:
                logger.warning(f'бот {bot_id}: не удалось получить позицию: {e}')
                held_lots = max(int(bot_data.get('initial_lots') or 0), 0)
        return {'botId': bot_id, 'hasPosition': held_lots > 0, 'lots': held_lots, 'lotSize': lot_size, 'shares': held_lots * lot_size, 'ticker': bot_data['ticker']}

    @staticmethod
    async def get_grid_chart(bot_id: int, hours: int=168) -> dict:
        bot_data = await GridBotService.get_bot(bot_id)
        if not bot_data:
            raise ValueError('Бот не найден')
        p_low = float(bot_data.get('p_low') or 0.0)
        p_high = float(bot_data.get('p_high') or 0.0)
        n = int(bot_data.get('n') or 0)
        candles: List[dict] = []
        current_price: Optional[float] = None
        lot_size = 1
        step = 0.01
        try:
            token = await GridBotService._get_user_token(bot_data['user_id'])
            instance = GridBotService._instance_from_bot_data(bot_data, token)
            async with httpx.AsyncClient(verify=CA_BUNDLE_PATH, timeout=30.0) as client:
                instr = await instance._get_instrument_by_ticker(client)
                instance.figi = instr['figi']
                lot_size = int(instr['lot'])
                min_step = instr['minPriceIncrement']
                step = int(min_step['units']) + int(min_step['nano']) / 1000000000.0
                if step == 0:
                    step = 0.01
                now = datetime.now(timezone.utc)
                frm = now - timedelta(hours=max(hours, 1))
                candles = await instance._get_candles(client, frm, now, interval='CANDLE_INTERVAL_HOUR')
                current_price = await instance._get_last_price(client)
        except ValueError as e:
            logger.warning(f'бот {bot_id}: свечи для графика недоступны: {e}')
        except Exception as e:
            logger.error(f'бот {bot_id}: не удалось получить данные для графика: {e}')
        if current_price is None:
            cp = bot_data.get('current_price')
            current_price = float(cp) if cp is not None else None
        grid_step = (p_high - p_low) / n if n > 0 else 0.0
        levels: List[dict] = []
        if n > 0 and grid_step > 0:
            raw_levels = [p_low + i * grid_step for i in range(n + 1)]
            i_cur = 0
            if current_price is not None:
                for i in range(n):
                    if raw_levels[i] <= current_price < raw_levels[i + 1]:
                        i_cur = i
                        break
                else:
                    if current_price >= raw_levels[-1]:
                        i_cur = n
            for i, price in enumerate(raw_levels):
                if i < i_cur:
                    side = 'buy'
                elif i > i_cur:
                    side = 'sell'
                else:
                    side = 'current'
                levels.append({'index': i, 'price': round(price / step) * step if step else price, 'side': side})
        return {'ticker': bot_data['ticker'], 'lot_size': lot_size, 'step': step, 'grid_step': grid_step, 'P_low': p_low, 'P_high': p_high, 'current_price': current_price, 'candles': candles, 'levels': levels}

    @staticmethod
    async def restore_active_bots():
        GridBotService.ensure_waiting_scheduler()
        async with db.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM grid_bots WHERE status='active' AND (type IS NULL OR type='simple')")
        for row in rows:
            bot_data = dict(row)
            try:
                token = await GridBotService._get_user_token(bot_data['user_id'])
                instance = GridBotService._instance_from_bot_data(bot_data, token)
                async with httpx.AsyncClient(verify=CA_BUNDLE_PATH, timeout=30.0) as client:
                    await instance.resume(client)
                await instance.start_monitoring()
                _active_bots[bot_data['id']] = instance
                logger.info(f"Восстановлен бот {bot_data['id']} ({bot_data['ticker']})")
            except Exception as e:
                logger.error(f"Ошибка восстановления бота {bot_data['id']}: {e}")
        async with db.acquire() as conn:
            waiting_rows = await conn.fetch("SELECT * FROM grid_bots WHERE status='waiting' AND (type IS NULL OR type='simple')")
        for row in waiting_rows:
            bot_data = dict(row)
            try:
                await GridBotService._enqueue_waiting(bot_data['id'], bot_data)
                logger.info(f"Бот {bot_data['id']} возвращён в очередь ожидания открытия биржи")
            except Exception as e:
                logger.error(f"Ошибка возврата бота {bot_data['id']} в очередь ожидания: {e}")

    @staticmethod
    async def shutdown_monitoring() -> None:
        global _waiting_scheduler_task
        for bot_id, instance in list(_active_bots.items()):
            try:
                instance._stop_flag = True
                if instance.monitor_task:
                    instance.monitor_task.cancel()
                    try:
                        await instance.monitor_task
                    except asyncio.CancelledError:
                        pass
                    instance.monitor_task = None
                logger.info(f'бот {bot_id}: мониторинг остановлен (завершение приложения)')
            except Exception as e:
                logger.error(f'бот {bot_id}: ошибка остановки мониторинга при завершении: {e}')
        _active_bots.clear()
        if _waiting_scheduler_task is not None and (not _waiting_scheduler_task.done()):
            _waiting_scheduler_task.cancel()
            try:
                await _waiting_scheduler_task
            except asyncio.CancelledError:
                pass
        _waiting_scheduler_task = None

    @staticmethod
    async def collect_license_stats() -> dict:
        from app.services.grid_bot.stats import compute_bot_stats
        from app.license import license_manager
        bots_out = []
        try:
            async with db.acquire() as conn:
                rows = await conn.fetch("SELECT * FROM grid_bots WHERE status != 'archived' AND (type IS NULL OR type='simple') ORDER BY id")
        except Exception as e:
            logger.warning('Статистика лицензии: не удалось прочитать ботов: %s', e)
            rows = []
        for row in rows:
            bot = dict(row)
            try:
                stats = await compute_bot_stats(bot)
            except Exception:
                stats = {}
            inst = _active_bots.get(bot['id'])
            bots_out.append({'id': bot['id'], 'ticker': bot.get('ticker'), 'status': bot.get('status'), 'trading_paused': bool(inst.trading_paused) if inst else None, 'license_hold': bool(inst._license_hold) if inst else None, 'stats': stats})
        return {'app': 't-invest-grid-bot', 'bots_total': len(bots_out), 'bots_active': sum((1 for b in bots_out if b['status'] == 'active')), 'license': license_manager.snapshot(), 'bots': bots_out}

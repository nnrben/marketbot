import logging
from fastapi import APIRouter, HTTPException
from app.services.grid_bot.models import DEFAULT_USER_ID, GridBotCreate, GridBotResponse, GridBotLevelsEstimateRequest, GridBotLevelsEstimateResponse
from app.services.grid_bot.service import GridBotService, bot_operation_guard
from app.services.grid_bot.exceptions import BotOperationInProgress
from typing import List
from datetime import datetime
logger = logging.getLogger(__name__)
router = APIRouter(prefix='/api/grid-bot', tags=['grid-bot'])

@router.post('/create', response_model=dict)
async def create_bot(data: GridBotCreate):
    try:
        bot_id = await GridBotService.create_bot(data)
        return {'id': bot_id, 'message': 'Бот создан'}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f'Ошибка создания бота: {e}')
        raise HTTPException(status_code=500, detail='Внутренняя ошибка сервера')

@router.post('/estimate-max-levels', response_model=GridBotLevelsEstimateResponse)
async def estimate_max_levels(data: GridBotLevelsEstimateRequest):
    try:
        result = await GridBotService.estimate_max_levels(data)
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f'Ошибка расчёта максимального количества уровней: {e}')
        raise HTTPException(status_code=500, detail='Внутренняя ошибка сервера')

@router.post('/start/{bot_id}')
async def start_bot(bot_id: int):
    try:
        async with bot_operation_guard(bot_id):
            result = await GridBotService.start_bot(bot_id)
        return result
    except BotOperationInProgress as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f'Неизвестная ошибка при старте бота {bot_id}: {e}')
        raise HTTPException(status_code=500, detail='Внутренняя ошибка сервера')

@router.post('/stop/{bot_id}')
async def stop_bot(bot_id: int):
    try:
        async with bot_operation_guard(bot_id):
            await GridBotService.stop_bot(bot_id)
        return {'message': 'Бот остановлен'}
    except BotOperationInProgress as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.error(f'Ошибка остановки бота {bot_id}: {e}')
        raise HTTPException(status_code=500, detail='Внутренняя ошибка сервера')

@router.post('/sync-commissions/{bot_id}')
async def sync_commissions(bot_id: int):
    try:
        return await GridBotService.sync_commissions(bot_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.error(f'Ошибка синхронизации комиссий бота {bot_id}: {e}')
        raise HTTPException(status_code=500, detail='Внутренняя ошибка сервера')

@router.get('/position/{bot_id}')
async def get_bot_position(bot_id: int):
    try:
        return await GridBotService.get_position_summary(bot_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.error(f'Ошибка получения позиции бота {bot_id}: {e}')
        raise HTTPException(status_code=500, detail='Внутренняя ошибка сервера')

@router.get('/chart/{bot_id}')
async def get_bot_chart(bot_id: int, hours: int=168):
    try:
        return await GridBotService.get_grid_chart(bot_id, hours=hours)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.error(f'Ошибка получения графика бота {bot_id}: {e}')
        raise HTTPException(status_code=500, detail='Внутренняя ошибка сервера')

@router.get('/stats/{bot_id}')
async def get_bot_stats(bot_id: int, sync: bool=False):
    from app.services.grid_bot.stats import compute_bot_stats
    bot = await GridBotService.get_bot(bot_id)
    if not bot:
        raise HTTPException(status_code=404, detail='Бот не найден')
    if sync:
        try:
            await GridBotService.sync_commissions(bot_id)
        except Exception as e:
            logger.warning(f'Синхронизация комиссий бота {bot_id} недоступна: {e}')
    try:
        return await compute_bot_stats(bot)
    except Exception as e:
        logger.error(f'Ошибка расчёта статистики бота {bot_id}: {e}')
        raise HTTPException(status_code=500, detail='Внутренняя ошибка сервера')

@router.get('/trades/{bot_id}')
async def get_bot_trades(bot_id: int, limit: int=0, offset: int=0):
    from app.services.grid_bot.stats import list_trades
    bot = await GridBotService.get_bot(bot_id)
    if not bot:
        raise HTTPException(status_code=404, detail='Бот не найден')
    try:
        return await list_trades(bot_id, limit=limit, offset=max(offset, 0))
    except Exception as e:
        logger.error(f'Ошибка получения истории сделок бота {bot_id}: {e}')
        raise HTTPException(status_code=500, detail='Внутренняя ошибка сервера')

@router.get('/waiting')
async def waiting_bots():
    try:
        return {'waiting': GridBotService.get_waiting_bots()}
    except Exception as e:
        logger.error(f'Ошибка получения очереди ожидания: {e}')
        raise HTTPException(status_code=500, detail='Внутренняя ошибка сервера')

@router.delete('/{bot_id}')
async def delete_bot(bot_id: int, sell_position: bool=False):
    try:
        async with bot_operation_guard(bot_id):
            result = await GridBotService.delete_bot(bot_id, sell_position=sell_position)
        return {'message': 'Бот удалён', **result}
    except BotOperationInProgress as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.error(f'Ошибка удаления бота {bot_id}: {e}')
        raise HTTPException(status_code=500, detail='Внутренняя ошибка сервера')

@router.get('/list', response_model=List[GridBotResponse])
async def list_bots(user_id: str=DEFAULT_USER_ID):
    try:
        bots = await GridBotService.get_bots_by_user(user_id)
        result = []
        for b in bots:
            item = {'id': b['id'], 'user_id': b['user_id'], 'ticker': b['ticker'], 'class_code': b['class_code'], 'P_low': float(b['p_low']) if b.get('p_low') is not None else 0.0, 'P_high': float(b['p_high']) if b.get('p_high') is not None else 0.0, 'capital': float(b['capital']) if b.get('capital') is not None else 0.0, 'N': int(b['n']) if b.get('n') is not None else 0, 'initial_lots': int(b.get('initial_lots') or 0), 'cash_remaining': float(b.get('cash_remaining') or 0.0), 'current_price': float(b['current_price']) if b.get('current_price') is not None else None, 'status': b['status'], 'created_at': b['created_at'].isoformat() if isinstance(b.get('created_at'), datetime) else str(b.get('created_at')), 'updated_at': b['updated_at'].isoformat() if isinstance(b.get('updated_at'), datetime) else str(b.get('updated_at'))}
            result.append(item)
        return result
    except Exception as e:
        logger.error(f'Ошибка получения списка ботов: {e}')
        raise HTTPException(status_code=500, detail='Внутренняя ошибка сервера')

@router.get('/{bot_id}', response_model=GridBotResponse)
async def get_bot(bot_id: int):
    bot = await GridBotService.get_bot(bot_id)
    if not bot:
        raise HTTPException(status_code=404, detail='Бот не найден')
    item = {'id': bot['id'], 'user_id': bot['user_id'], 'ticker': bot['ticker'], 'class_code': bot['class_code'], 'P_low': float(bot['p_low']) if bot.get('p_low') is not None else 0.0, 'P_high': float(bot['p_high']) if bot.get('p_high') is not None else 0.0, 'capital': float(bot['capital']) if bot.get('capital') is not None else 0.0, 'N': int(bot['n']) if bot.get('n') is not None else 0, 'initial_lots': int(bot.get('initial_lots') or 0), 'cash_remaining': float(bot.get('cash_remaining') or 0.0), 'current_price': float(bot['current_price']) if bot.get('current_price') is not None else None, 'status': bot['status'], 'created_at': bot['created_at'].isoformat() if isinstance(bot.get('created_at'), datetime) else str(bot.get('created_at')), 'updated_at': bot['updated_at'].isoformat() if isinstance(bot.get('updated_at'), datetime) else str(bot.get('updated_at'))}
    return item

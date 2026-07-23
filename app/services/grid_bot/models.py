from pydantic import BaseModel
from typing import Optional

# В контейнере работает один пользователь — владелец токена из переменных
# окружения, поэтому user_id по умолчанию фиксированный.
DEFAULT_USER_ID = "default"


class GridBotCreate(BaseModel):
    user_id: str = DEFAULT_USER_ID
    ticker: str
    class_code: str
    P_low: float
    P_high: float
    capital: float
    N: int


class GridBotUpdate(BaseModel):
    status: Optional[str] = None


class GridBotResponse(BaseModel):
    id: int
    user_id: str
    ticker: str
    class_code: str
    P_low: float
    P_high: float
    capital: float
    N: int
    initial_lots: int = 0
    cash_remaining: float = 0.0
    current_price: Optional[float] = None
    status: str
    created_at: str
    updated_at: str


class GridOrderResponse(BaseModel):
    id: int
    bot_id: int
    order_id: str
    side: str
    level_idx: int
    lots: int
    pair_level: Optional[int]
    status: str
    created_at: str
    updated_at: str


class GridBotLevelsEstimateRequest(BaseModel):
    """Запрос на расчёт максимально допустимого количества уровней сетки
    исходя из капитала пользователя и текущей рыночной цены инструмента."""
    user_id: str = DEFAULT_USER_ID
    ticker: str
    class_code: str
    P_low: float
    P_high: float
    capital: float


class GridBotLevelsEstimateResponse(BaseModel):
    """Результат расчёта максимально допустимого количества уровней сетки."""
    current_price: float
    lot_size: int
    step: float
    initial_lots: int
    cash_remaining_estimate: float
    max_levels: int
    reason: Optional[str] = None

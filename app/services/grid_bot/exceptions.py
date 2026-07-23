from app.services.grid_bot.config import (
    EXCHANGE_CLOSED_ERROR_CODES,
    EXCHANGE_CLOSED_ERROR_SUBSTRINGS,
)


class ExchangeClosedError(Exception):
    """Поднимается, когда заявку нельзя выставить, потому что биржа закрыта.

    Обрабатывается на уровне start_bot: вместо ошибки бот ставится в
    очередь ожидания открытия биржи (_waiting_bots) и запускается
    автоматически, как только площадка откроется.
    """
    pass


class BotOperationInProgress(Exception):
    """Поднимается, когда по боту уже выполняется мутирующая операция
    (запуск/остановка/удаление), а пришёл повторный такой же запрос.

    Защищает от дубль-команд с фронта: например, когда пользователь
    повторяет «удалить и продать», не дождавшись долгой рыночной продажи.
    Раньше второй запрос запускал вторую ликвидацию и, видя уже
    заблокированную заявкой позицию, рапортовал «продано 0» поверх реально
    идущей продажи. Роут отдаёт по этому исключению HTTP 409.
    """
    pass


def _is_exchange_closed_error(exc: Exception) -> bool:
    """Определяет по тексту исключения, что причина — закрытая биржа."""
    text = str(exc).lower()
    for code in EXCHANGE_CLOSED_ERROR_CODES:
        if code in text:
            return True
    for sub in EXCHANGE_CLOSED_ERROR_SUBSTRINGS:
        if sub in text:
            return True
    return False

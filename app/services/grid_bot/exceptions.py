from app.services.grid_bot.config import EXCHANGE_CLOSED_ERROR_CODES, EXCHANGE_CLOSED_ERROR_SUBSTRINGS

class ExchangeClosedError(Exception):
    pass

class BotOperationInProgress(Exception):
    pass

def _is_exchange_closed_error(exc: Exception) -> bool:
    text = str(exc).lower()
    for code in EXCHANGE_CLOSED_ERROR_CODES:
        if code in text:
            return True
    for sub in EXCHANGE_CLOSED_ERROR_SUBSTRINGS:
        if sub in text:
            return True
    return False

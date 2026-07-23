import logging

from app.config import settings
from app.ssl_bundle import ensure_ca_bundle

logger = logging.getLogger(__name__)

# Путь к набору корневых сертификатов для TLS-соединения с API Т-Инвестиций.
# Bundle собирается на этапе сборки Docker-образа; если файла нет (например,
# при локальном запуске без Docker) — собирается на лету или подменяется
# стандартным набором certifi.
CA_BUNDLE_PATH = ensure_ca_bundle(settings.ca_bundle_path)


# Коды/подстроки ошибок брокера, означающие "биржа закрыта / нет приёма
# заявок в стакан" — их мы не считаем фатальными, а откладываем запуск.
EXCHANGE_CLOSED_ERROR_CODES = {"30049"}
EXCHANGE_CLOSED_ERROR_SUBSTRINGS = (
    "net predlozhenij v stakane",
    "нет предложений в стакане",
    "market is closed",
    "instrument is not available for trading",
    "торги по инструменту закрыты",
)

API_BASE = "https://invest-public-api.tbank.ru/rest/tinkoff.public.invest.api.contract.v1"
ACCOUNTS_URL = f"{API_BASE}.UsersService/GetAccounts"
INSTRUMENT_URL = f"{API_BASE}.InstrumentsService/GetInstrumentBy"
LAST_PRICES_URL = f"{API_BASE}.MarketDataService/GetLastPrices"
GET_CANDLES_URL = f"{API_BASE}.MarketDataService/GetCandles"
POST_ORDER_URL = f"{API_BASE}.OrdersService/PostOrderAsync"
ORDER_STATE_URL = f"{API_BASE}.OrdersService/GetOrderState"
POST_STOP_ORDER_URL = f"{API_BASE}.StopOrdersService/PostStopOrder"
GET_STOP_ORDERS_URL = f"{API_BASE}.StopOrdersService/GetStopOrders"
CANCEL_STOP_ORDER_URL = f"{API_BASE}.StopOrdersService/CancelStopOrder"
GET_POSITIONS_URL = f"{API_BASE}.OperationsService/GetPositions"
TRADING_SCHEDULES_URL = f"{API_BASE}.InstrumentsService/TradingSchedules"
GET_OPERATIONS_URL = f"{API_BASE}.OperationsService/GetOperations"

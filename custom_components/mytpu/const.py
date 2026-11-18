"""Constants for the Tacoma Public Utilities integration."""

DOMAIN = "mytpu"

CONF_POWER_SERVICE = "power_service"
CONF_WATER_SERVICE = "water_service"
CONF_TOKEN_DATA = "token_data"

TPU_POWER_SENSOR_ID_SUFFIX = "_energy"
TPU_WATER_SENSOR_ID_SUFFIX = "_water"

CONF_UPDATE_INTERVAL_HOURS = "update_interval_hours"
CONF_POWER_PRICE_ENTITY = "power_price_entity"
CONF_WATER_PRICE_ENTITY = "water_price_entity"

# Default update interval in hours
DEFAULT_UPDATE_INTERVAL_HOURS = 1
MIN_UPDATE_INTERVAL_HOURS = 1

SERVICE_IMPORT_HISTORY = "import_history"
ATTR_CONFIG_ENTRY_ID = "config_entry_id"
ATTR_START_DATE = "start_date"

# History import fetches backward in chunks until TPU returns no readings
HISTORY_CHUNK_DAYS = 365
MAX_HISTORY_CHUNKS = 10
# How long to wait for the recorder to commit a history rewrite
HISTORY_COMMIT_TIMEOUT_SECONDS = 300

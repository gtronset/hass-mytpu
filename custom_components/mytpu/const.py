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

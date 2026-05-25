import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
import homeassistant.helpers.config_validation as cv

DOMAIN = "hass_cozylife_local_pull"

class CozyLifeLocalConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Обработка настройки CozyLife Local через UI."""
    
    VERSION = 1

    async def async_step_user(self, user_input=None):
        """Шаг, запускаемый при поиске интеграции пользователем."""
        errors = {}

        if user_input is not None:
            # Превращаем строку с IP/подсетями обратно в список для кода
            return self.async_create_entry(
                title="CozyLife Local", 
                data={
                    "lang": user_input.get("lang", "en"),
                    "ip": [ip.strip() for ip in user_input.get("ip", "").split(",") if ip.strip()],
                    "scan_interval": user_input.get("scan_interval", 300)
                }
            )

        # Схема полей формы ввода данных
        DATA_SCHEMA = vol.Schema({
            vol.Required("lang", default="en"): vol.In({"en": "English", "ru": "Русский"}),
            vol.Optional("ip", default=""): str,
            vol.Optional("scan_interval", default=300): int,
        })

        return self.async_show_form(
            step_id="user", data_schema=DATA_SCHEMA, errors=errors
        )

"""Example Load Platform integration."""
from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers.discovery import async_load_platform
from homeassistant.helpers.typing import ConfigType
from homeassistant.config_entries import ConfigEntry
import logging
import time
from .const import (
    DOMAIN,
    LANG
)
from .utils import get_pid_list
from .udp_discover import get_ip
from .tcp_client import tcp_client

_LOGGER = logging.getLogger(__name__)


def setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Настройка через старый configuration.yaml (если он используется)."""
    if DOMAIN not in config:
        return True
        
    # Просто перенаправляем данные в нашу общую функцию инициализации
    return _core_setup(hass, config[DOMAIN], config)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Настройка интеграции, запущенная через UI (Config Flow)."""
    # Извлекаем данные, которые пользователь ввел в интерфейсе HA
    config_data = dict(entry.data)
    
    # Запускаем инициализацию в отдельном потоке, так как оригинальный код использует time.sleep()
    return await hass.async_add_executor_job(
        _core_setup, hass, config_data, {DOMAIN: config_data}
    )


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Удаление интеграции через UI."""
    return True


def _core_setup(hass: HomeAssistant, domain_config: dict, full_config: dict) -> bool:
    """Общая логика инициализации для YAML и UI."""
    ip = get_ip()
    ip_from_config = domain_config.get('ip') if domain_config.get('ip') is not None else []    
    ip += ip_from_config
    ip_list = []
    [ip_list.append(i) for i in ip if i not in ip_list]

    if 0 == len(ip_list):
        _LOGGER.info('discover nothing')
        return True

    _LOGGER.info('try conncet ip_list: %s', ip_list)
    lang_from_config = domain_config.get('lang') if domain_config.get('lang') is not None else LANG
    get_pid_list(lang_from_config)

    hass.data[DOMAIN] = {
        'temperature': 24,
        'ip': ip_list,
        'tcp_client': [tcp_client(item) for item in ip_list],
    }

    # Ожидание ответов от устройств (оригинальная логика автора)
    time.sleep(3)
    
    hass.loop.call_soon_threadsafe(hass.async_create_task, async_load_platform(hass, 'light', DOMAIN, {}, full_config))
    hass.loop.call_soon_threadsafe(hass.async_create_task, async_load_platform(hass, 'switch', DOMAIN, {}, full_config))
    return True

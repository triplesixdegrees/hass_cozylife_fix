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

# Список платформ, которые поддерживает этот плагин
PLATFORMS = ["light", "switch"]


def setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Настройка через старый configuration.yaml."""
    if DOMAIN not in config:
        return True
    return _core_setup(hass, config[DOMAIN], config, is_yaml=True)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Настройка интеграции, запущенная через UI (Config Flow)."""
    config_data = dict(entry.data)
    
    # Запускаем инициализацию в отдельном потоке из-за блокирующих сокетов и time.sleep()
    success = await hass.async_add_executor_job(
        _core_setup, hass, config_data, {DOMAIN: config_data}, False
    )
    
    if success:
        # Для UI-настройки регистрируем платформы по современному стандарту HA
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
        
    return success


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Удаление интеграции через UI."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unload_ok


def _core_setup(hass: HomeAssistant, domain_config: dict, full_config: dict, is_yaml: bool = False) -> bool:
    """Общая логика инициализации."""
    _LOGGER.info("Starting CozyLife Local initialization...")
    
    # 1. Сканируем сеть на устройства
    try:
        ip = get_ip()
    except Exception as e:
        _LOGGER.error("Error during UDP discovery: %s", e)
        ip = []

    # 2. Добавляем IP из конфига
    ip_from_config = domain_config.get('ip') if domain_config.get('ip') is not None else []    
    if isinstance(ip_from_config, str):
        ip_from_config = [i.strip() for i in ip_from_config.split(",") if i.strip()]
        
    ip += ip_from_config
    ip_list = []
    [ip_list.append(i) for i in ip if i not in ip_list]

    if 0 == len(ip_list):
        _LOGGER.warning('CozyLife Local: No devices discovered on the network.')
        hass.data[DOMAIN] = {'temperature': 24, 'ip': [], 'tcp_client': []}
        if is_yaml:
            _register_platforms_yaml(hass, full_config)
        return True

    _LOGGER.info('CozyLife Local found devices: %s', ip_list)
    
    # 3. Безопасное получение списка PID из облака
    lang_from_config = domain_config.get('lang') if domain_config.get('lang') is not None else LANG
    try:
        _LOGGER.info("Fetching device models from CozyLife cloud...")
        get_pid_list(lang_from_config)
    except Exception as err:
        _LOGGER.warning("CozyLife cloud network timeout/error: %s. Using local fallback.", err)

    # 4. Подключаемся к устройствам
    hass.data[DOMAIN] = {
        'temperature': 24,
        'ip': ip_list,
        'tcp_client': [tcp_client(item) for item in ip_list],
    }

    # Ожидание ответов (оригинальная задержка автора)
    time.sleep(3)
    
    # Если это старый YAML режим, регистрируем платформы по-старому
    if is_yaml:
        _register_platforms_yaml(hass, full_config)
        
    return True


def _register_platforms_yaml(hass: HomeAssistant, full_config: dict):
    """Регистрация компонентов старым методом (только для configuration.yaml)."""
    hass.loop.call_soon_threadsafe(hass.async_create_task, async_load_platform(hass, 'light', DOMAIN, {}, full_config))
    hass.loop.call_soon_threadsafe(hass.async_create_task, async_load_platform(hass, 'switch', DOMAIN, {}, full_config))

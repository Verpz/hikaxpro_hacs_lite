"""The hikvision_axpro integration."""

import asyncio
import logging
from datetime import timedelta

import hikaxpro
import requests
import xmltodict
from homeassistant.components.alarm_control_panel import (
    AlarmControlPanelState,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    ATTR_CODE_FORMAT,
    CONF_CODE,
    CONF_ENABLED,
    CONF_HOST,
    CONF_PASSWORD,
    CONF_SCAN_INTERVAL,
    CONF_USERNAME,
    SERVICE_RELOAD,
    Platform,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.service import async_register_admin_service
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .client import AuthenticationError, LiteHikAxPro
from .const import (
    ALLOW_SUBSYSTEMS,
    DATA_COORDINATOR,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    USE_CODE_ARMING,
    lite_scan_interval,
)
from .entity_id import migrate_invalid_entity_ids
from .model import (
    Arming,
    ExDevStatusResponse,
    ExtensionModule,
    JSONResponseStatus,
    Keypad,
    OutputConfList,
    OutputStatusFull,
    RelayStatusSearchResponse,
    RelaySwitchConf,
    Repeater,
    Siren,
    Status,
    SubSys,
    SubSystemResponse,
    Zone,
    ZoneConfig,
    ZonesConf,
    ZonesResponse,
)

PLATFORMS: list[Platform] = [Platform.ALARM_CONTROL_PANEL]
_LOGGER = logging.getLogger(__name__)


async def async_setup(hass: HomeAssistant, config: ConfigEntry):
    """Set up the hikvision_axpro integration component."""
    hass.data.setdefault(DOMAIN, {})

    async def _handle_reload(service):
        """Handle reload service call."""
        _LOGGER.info("Service %s.reload called: reloading integration", DOMAIN)

        current_entries = hass.config_entries.async_entries(DOMAIN)

        reload_tasks = [
            hass.config_entries.async_reload(entry.entry_id)
            for entry in current_entries
        ]

        await asyncio.gather(*reload_tasks)

    async_register_admin_service(hass, DOMAIN, SERVICE_RELOAD, _handle_reload)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up hikvision_axpro from a config entry."""
    host = entry.data[CONF_HOST]
    username = entry.data[CONF_USERNAME]
    password = entry.data[CONF_PASSWORD]
    use_code = entry.data[CONF_ENABLED]
    code_format = entry.data[ATTR_CODE_FORMAT]
    code = entry.data[CONF_CODE]
    use_code_arming = entry.data[USE_CODE_ARMING]
    use_sub_systems = entry.data.get(ALLOW_SUBSYSTEMS, False)
    axpro = LiteHikAxPro(
        host, username, password, user_level=hikaxpro.USER_LEVEL_ADMIN_OPERATOR
    )
    update_interval = lite_scan_interval(
        entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
    )

    try:
        mac = await hass.async_add_executor_job(axpro.get_interface_mac_address, 1)
    except (
        AuthenticationError,
        requests.RequestException,
        TimeoutError,
        ConnectionError,
    ) as ex:
        raise ConfigEntryNotReady from ex

    coordinator = HikAxProDataUpdateCoordinator(
        hass,
        axpro,
        mac,
        use_code,
        code_format,
        use_code_arming,
        code,
        update_interval,
        use_sub_systems,
        config_entry=entry,
    )
    try:
        await hass.async_add_executor_job(coordinator.init_device)
    except (
        AuthenticationError,
        requests.RequestException,
        TimeoutError,
        ConnectionError,
    ) as ex:
        raise ConfigEntryNotReady from ex
    await coordinator.async_config_entry_first_refresh()
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = {DATA_COORDINATOR: coordinator}

    migrate_invalid_entity_ids(hass, entry)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(update_listener))

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        hass.data[DOMAIN].pop(entry.entry_id)

    return unload_ok


async def update_listener(hass: HomeAssistant, config_entry: ConfigEntry):
    """Update listener."""
    await hass.config_entries.async_reload(config_entry.entry_id)


class HikAxProDataUpdateCoordinator(DataUpdateCoordinator):
    """Class to manage fetching ax pro data."""

    axpro: hikaxpro.HikAxPro
    zone_status: ZonesResponse | None
    zones: dict[int, Zone] | None = None
    device_info: dict | None = None
    device_model: str | None = None
    device_name: str | None = None
    sub_systems: dict[int, SubSys] = {}
    """ Zones aka devices """
    devices: dict[int, ZoneConfig] = {}
    relays: dict[int, RelaySwitchConf] = {}
    relays_status: dict[int, OutputStatusFull] = {}
    sirens: dict[int, Siren] = {}
    keypads: dict[int, Keypad] = {}
    repeaters: dict[int, Repeater] = {}
    extensions: dict[int, ExtensionModule] = {}
    host_status: dict | None = None
    ac_power_status: dict | None = None
    hub_batteries: list[dict] = []
    siren_control_supported: dict[int, bool] = {}
    host_control_cap: dict | None = None
    one_key_alarm_supported: bool | None = None
    siren_ctrl_supported: bool | None = None
    use_sub_systems: bool

    def __init__(
        self,
        hass: HomeAssistant,
        axpro: hikaxpro.HikAxPro,
        mac,
        use_code,
        code_format,
        use_code_arming,
        code,
        update_interval: float,
        use_sub_systems=False,
        config_entry=None,
    ) -> None:
        """Initialize global data updater and AXPro API."""
        self.axpro = axpro
        self.state = None
        self.sub_systems = {}
        self.zone_status = None
        self.host = axpro.host
        self.mac = mac
        self.use_code = use_code
        self.code_format = code_format
        self.use_code_arming = use_code_arming
        self.code = code
        self.use_sub_systems = use_sub_systems
        self.sirens = {}
        self.keypads = {}
        self.repeaters = {}
        self.extensions = {}
        self.host_status = None
        self.ac_power_status = None
        self.hub_batteries = []
        self.siren_control_supported = {}
        self.host_control_cap = None
        self.one_key_alarm_supported = None
        self.siren_ctrl_supported = None
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            config_entry=config_entry,
            update_interval=timedelta(seconds=lite_scan_interval(update_interval)),
        )

    def _get_device_info(self):
        endpoint = self.axpro.build_url(
            f"http://{self.host}" + hikaxpro.consts.Endpoints.SystemDeviceInfo, False
        )
        response = self.axpro.make_request(endpoint, "GET", None, True)

        if response.status_code != 200:
            raise hikaxpro.errors.UnexpectedResponseCodeError(
                response.status_code, response.text
            )
        _LOGGER.debug(response.text)
        return xmltodict.parse(response.text)

    def init_device(self):
        """Init device information."""
        self.device_info = self._get_device_info()
        self.device_name = self.device_info["DeviceInfo"]["deviceName"]
        self.device_model = self.device_info["DeviceInfo"]["model"]
        _LOGGER.debug(self.device_info)

    def load_host_control_capabilities(self) -> None:
        """Load HostControlCap (siren / one-key alarm support flags)."""
        try:
            endpoint = self.axpro.build_url(
                f"http://{self.host}" + hikaxpro.consts.Endpoints.HostCapabilities,
                True,
            )
            response = self.axpro.make_request(endpoint, "GET", None, True)
            if response.status_code != 200:
                _LOGGER.debug(
                    "HostControlCap unavailable: HTTP %s", response.status_code
                )
                return
            payload = response.json()
            cap = payload.get("HostControlCap") if isinstance(payload, dict) else None
            if not isinstance(cap, dict):
                return
            self.host_control_cap = cap
            if "isSptOneKeyAlarmCtrl" in cap:
                self.one_key_alarm_supported = bool(cap.get("isSptOneKeyAlarmCtrl"))
            if "isSptSirenCtrl" in cap:
                self.siren_ctrl_supported = bool(cap.get("isSptSirenCtrl"))
            _LOGGER.debug(
                "HostControlCap one_key=%s siren_ctrl=%s",
                self.one_key_alarm_supported,
                self.siren_ctrl_supported,
            )
        except Exception:  # noqa: BLE001 - firmware varies
            _LOGGER.debug("HostControlCap load failed", exc_info=True)

    def load_relays(self):
        """Load relays."""
        devices = self._load_relays()
        if devices is not None:
            self.relays = {}
            for item in devices.list:
                self.relays[item.output.id] = item.output

    def _load_relays(self) -> OutputConfList:
        endpoint = self.axpro.build_url(
            f"http://{self.host}" + hikaxpro.consts.Endpoints.OutputConfig, True
        )
        response = self.axpro.make_request(endpoint, "GET", None, True)

        if response.status_code != 200:
            raise hikaxpro.errors.UnexpectedResponseCodeError(
                response.status_code, response.text
            )
        _LOGGER.debug(response.text)
        return OutputConfList.from_dict(response.json())

    def load_ext_devices_status(self):
        """Load status of external devices."""
        statuses = self._load_ext_devices_status()
        if statuses is not None:
            self.relays_status = {}
            self.sirens = {}
            self.keypads = {}
            self.repeaters = {}
            self.extensions = {}
            if statuses.ex_dev_status is not None:
                if statuses.ex_dev_status.output_list is not None:
                    for item in statuses.ex_dev_status.output_list:
                        if item.output is not None and item.output.id is not None:
                            self.relays_status[item.output.id] = item.output
                if statuses.ex_dev_status.siren_list is not None:
                    for item in statuses.ex_dev_status.siren_list:
                        if item.siren is not None and item.siren.id is not None:
                            self.sirens[item.siren.id] = item.siren
                if statuses.ex_dev_status.keypad_list is not None:
                    for item in statuses.ex_dev_status.keypad_list:
                        if item.keypad is not None and item.keypad.id is not None:
                            self.keypads[item.keypad.id] = item.keypad
                if statuses.ex_dev_status.repeater_list is not None:
                    for item in statuses.ex_dev_status.repeater_list:
                        if item.repeater is not None and item.repeater.id is not None:
                            self.repeaters[item.repeater.id] = item.repeater
                if statuses.ex_dev_status.extension_list is not None:
                    for item in statuses.ex_dev_status.extension_list:
                        if (
                            item.extension_module is not None
                            and item.extension_module.id is not None
                        ):
                            self.extensions[item.extension_module.id] = (
                                item.extension_module
                            )

    def _load_ext_devices_status(self) -> ExDevStatusResponse:
        endpoint = self.axpro.build_url(
            f"http://{self.host}" + "/ISAPI/SecurityCP/status/exDevStatus", True
        )
        response = self.axpro.make_request(endpoint, "GET", None, True)

        if response.status_code != 200:
            raise hikaxpro.errors.UnexpectedResponseCodeError(
                response.status_code, response.text
            )
        _LOGGER.debug(response.text)
        return ExDevStatusResponse.from_dict(response.json())

    def load_devices(self):
        """Load devices from Zone Config."""
        devices = self._load_devices()
        if devices is not None:
            self.devices = {}
            for item in devices.list:
                self.devices[item.zone.id] = item.zone

    def _load_devices(self) -> ZonesConf:
        endpoint = self.axpro.build_url(
            f"http://{self.host}" + hikaxpro.consts.Endpoints.ZonesConfig, True
        )
        response = self.axpro.make_request(endpoint, "GET", None, True)

        if response.status_code != 200:
            raise hikaxpro.errors.UnexpectedResponseCodeError(
                response.status_code, response.text
            )
        _LOGGER.debug(response.text)
        return ZonesConf.from_dict(response.json())

    def _update_relays_status(self) -> RelayStatusSearchResponse:
        endpoint = self.axpro.build_url(
            f"http://{self.host}" + hikaxpro.consts.Endpoints.OutputStatus, True
        )
        response = self.axpro.make_request(
            endpoint,
            "POST",
            {
                "OutputCond": {
                    "searchID": "homeassistant",
                    "searchResultPosition": 1,
                    "maxResults": 50,
                    "moduleType": "localWired",
                }
            },
            True,
        )

        if response.status_code != 200:
            raise hikaxpro.errors.UnexpectedResponseCodeError(
                response.status_code, response.text
            )
        _LOGGER.debug(response.text)
        return RelayStatusSearchResponse.from_dict(response.json())

    def _update_data(self) -> None:
        """Fetch only subsystem status; never poll zones or peripherals."""
        status_json = self.axpro.subsystem_status()
        try:
            response = SubSystemResponse.from_dict(status_json)
            systems = [item.sub_sys for item in response.sub_sys_list]
            if not systems or any(system.arming is None for system in systems):
                raise ValueError("Missing or unknown subsystem state")
            if len({system.id for system in systems}) != len(systems):
                raise ValueError("Duplicate subsystem IDs")
            enabled = [system for system in systems if system.enabled]
            if not enabled:
                raise ValueError("No enabled subsystems")
        except (
            AssertionError,
            AttributeError,
            TypeError,
            ValueError,
            KeyError,
        ) as error:
            raise UpdateFailed("Invalid AX Pro subsystem status") from error

        # Aggregate all enabled areas, independent of response order or entity options.
        # Triggered > exit delay > away > vacation > home > all disarmed.
        state = AlarmControlPanelState.DISARMED
        if any(system.alarm for system in enabled):
            state = AlarmControlPanelState.TRIGGERED
        else:
            for arming, candidate in (
                (Arming.ARMING, AlarmControlPanelState.ARMING),
                (Arming.AWAY, AlarmControlPanelState.ARMED_AWAY),
                (Arming.VACATION, AlarmControlPanelState.ARMED_VACATION),
                (Arming.STAY, AlarmControlPanelState.ARMED_HOME),
            ):
                if any(system.arming == arming for system in enabled):
                    state = candidate
                    break
        self.sub_systems = {system.id: system for system in enabled}
        self.state = state

    def _update_host_diagnostics(self) -> None:
        """Best-effort poll of host / AC / hub battery status APIs."""
        try:
            self.host_status = self.axpro.host_status()
        except Exception:  # noqa: BLE001 - panel firmware varies
            _LOGGER.debug("host status unavailable", exc_info=True)
            self.host_status = None

        try:
            endpoint = self.axpro.build_url(
                f"http://{self.host}/ISAPI/SecurityCP/status/acPowerStatus", True
            )
            response = self.axpro.make_request(endpoint, "GET", None, True)
            if response.status_code == 200:
                self.ac_power_status = response.json()
            else:
                self.ac_power_status = None
        except Exception:  # noqa: BLE001
            _LOGGER.debug("AC power status unavailable", exc_info=True)
            self.ac_power_status = None

        try:
            endpoint = self.axpro.build_url(
                f"http://{self.host}" + hikaxpro.consts.Endpoints.BatteriesStatus,
                True,
            )
            response = self.axpro.make_request(endpoint, "GET", None, True)
            batteries: list[dict] = []
            if response.status_code == 200:
                payload = response.json()
                for item in payload.get("BatteryList") or []:
                    battery = item.get("Battery") if isinstance(item, dict) else None
                    if isinstance(battery, dict):
                        batteries.append(battery)
            self.hub_batteries = batteries
        except Exception:  # noqa: BLE001
            _LOGGER.debug("hub batteries unavailable", exc_info=True)
            self.hub_batteries = []

    async def _async_update_data(self) -> None:
        """Fetch data from Axpro."""
        try:
            await self.hass.async_add_executor_job(self._update_data)
        except (
            AuthenticationError,
            requests.RequestException,
            ConnectionError,
        ) as error:
            raise UpdateFailed(error) from error

    async def async_arm_home(self, sub_id: int | None = None):
        """Arm alarm panel in home state."""
        is_success = await self.hass.async_add_executor_job(self.axpro.arm_home, sub_id)

        if is_success:
            await self.async_refresh()

    async def async_arm_away(self, sub_id: int | None = None):
        """Arm alarm panel in away state."""
        is_success = await self.hass.async_add_executor_job(self.axpro.arm_away, sub_id)

        if is_success:
            await self.async_refresh()

    async def async_disarm(self, sub_id: int | None = None):
        """Disarm alarm control panel."""
        is_success = await self.hass.async_add_executor_job(self.axpro.disarm, sub_id)

        if is_success:
            await self.async_refresh()

    def _zones_blocking_arm(self) -> list[int]:
        """Return zone IDs that typically prevent arming when left open/triggered."""
        if not self.zones:
            return []
        blocking: list[int] = []
        for zone_id, zone in self.zones.items():
            if zone.bypassed:
                continue
            open_magnet = zone.magnet_open_status is True
            triggered = zone.status is Status.TRIGGER
            alarming = zone.alarm is True
            if open_magnet or triggered or alarming:
                blocking.append(zone_id)
        return blocking

    async def async_bypass_blocking_zones(self) -> None:
        """Bypass zones that look open/triggered before arming."""
        for zone_id in self._zones_blocking_arm():
            await self.async_bypass_zone(zone_id)

    async def async_bypass_zone(self, zone_id: int) -> bool:
        """Bypass a single zone."""
        is_success = await self.hass.async_add_executor_job(
            self.axpro.bypass_zone, zone_id
        )
        if is_success:
            await self._async_update_data()
            await self.async_request_refresh()
        return is_success

    async def async_recover_bypass_zone(self, zone_id: int) -> bool:
        """Clear bypass on a single zone."""
        is_success = await self.hass.async_add_executor_job(
            self.axpro.recover_bypass_zone, zone_id
        )
        if is_success:
            await self._async_update_data()
            await self.async_request_refresh()
        return is_success

    def _relay_call(self, relay_id: int, is_enabled: bool) -> JSONResponseStatus:
        endpoint = self.axpro.build_url(
            f"http://{self.host}"
            + hikaxpro.consts.Endpoints.OutputControl.replace("{}", str(relay_id)),
            True,
        )
        response = self.axpro.make_request(
            endpoint,
            "PUT",
            {"OutputsCtrl": {"switch": "open" if is_enabled else "close"}},
            True,
        )
        if response.status_code != 200:
            raise hikaxpro.errors.UnexpectedResponseCodeError(
                response.status_code, response.text
            )
        _LOGGER.debug(response.text)
        return JSONResponseStatus.from_dict(response.json())

    async def relay_on(self, relay_id: int):
        """Turn on relay by ID."""
        response: JSONResponseStatus = await self.hass.async_add_executor_job(
            self._relay_call, relay_id, True
        )
        return response.status_code == 1

    async def relay_off(self, relay_id: int):
        """Turn off relay by ID."""
        response: JSONResponseStatus = await self.hass.async_add_executor_job(
            self._relay_call, relay_id, False
        )
        return response.status_code == 1

    def _siren_call(self, siren_id: int, is_enabled: bool) -> JSONResponseStatus:
        endpoint = self.axpro.build_url(
            f"http://{self.host}/ISAPI/SecurityCP/control/siren/{siren_id}",
            True,
        )
        response = self.axpro.make_request(
            endpoint,
            "PUT",
            {"SirenCtrl": {"switch": "open" if is_enabled else "close"}},
            True,
        )
        if response.status_code != 200:
            raise hikaxpro.errors.UnexpectedResponseCodeError(
                response.status_code, response.text
            )
        _LOGGER.debug(response.text)
        return JSONResponseStatus.from_dict(response.json())

    async def siren_on(self, siren_id: int) -> bool:
        """Turn on / open a siren by ID."""
        try:
            response: JSONResponseStatus = await self.hass.async_add_executor_job(
                self._siren_call, siren_id, True
            )
            ok = response.status_code == 1
            if ok:
                self.siren_control_supported[siren_id] = True
            return ok
        except hikaxpro.errors.UnexpectedResponseCodeError as err:
            if "notSupport" in str(err):
                self.siren_control_supported[siren_id] = False
                _LOGGER.warning(
                    "Siren %s control not supported by this panel/device", siren_id
                )
                return False
            raise

    async def siren_off(self, siren_id: int) -> bool:
        """Turn off / close a siren by ID."""
        try:
            response: JSONResponseStatus = await self.hass.async_add_executor_job(
                self._siren_call, siren_id, False
            )
            ok = response.status_code == 1
            if ok:
                self.siren_control_supported[siren_id] = True
            return ok
        except hikaxpro.errors.UnexpectedResponseCodeError as err:
            if "notSupport" in str(err):
                self.siren_control_supported[siren_id] = False
                _LOGGER.warning(
                    "Siren %s control not supported by this panel/device", siren_id
                )
                return False
            raise

    def _one_key_alarm_call(self, is_enabled: bool) -> JSONResponseStatus:
        """PUT /ISAPI/SecurityCP/control/oneKeyAlarm with OneKeyAlarm.switch."""
        endpoint = self.axpro.build_url(
            f"http://{self.host}/ISAPI/SecurityCP/control/oneKeyAlarm",
            True,
        )
        response = self.axpro.make_request(
            endpoint,
            "PUT",
            {"OneKeyAlarm": {"switch": "open" if is_enabled else "close"}},
            True,
        )
        if response.status_code != 200:
            raise hikaxpro.errors.UnexpectedResponseCodeError(
                response.status_code, response.text
            )
        _LOGGER.debug(response.text)
        return JSONResponseStatus.from_dict(response.json())

    async def one_key_alarm_on(self) -> bool:
        """Trigger panel one-key / panic alarm when supported."""
        if self.one_key_alarm_supported is False:
            _LOGGER.warning("One-key alarm not supported by this panel")
            return False
        response: JSONResponseStatus = await self.hass.async_add_executor_job(
            self._one_key_alarm_call, True
        )
        ok = response.status_code == 1
        if ok:
            self.one_key_alarm_supported = True
        return ok

    async def one_key_alarm_off(self) -> bool:
        """Clear / close one-key alarm when supported."""
        if self.one_key_alarm_supported is False:
            _LOGGER.warning("One-key alarm not supported by this panel")
            return False
        response: JSONResponseStatus = await self.hass.async_add_executor_job(
            self._one_key_alarm_call, False
        )
        ok = response.status_code == 1
        if ok:
            self.one_key_alarm_supported = True
        return ok

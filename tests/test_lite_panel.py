"""Exercise lite setup and alarm commands with real HA and a mocked panel."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call, patch

import pytest
import voluptuous as vol
from homeassistant.components.alarm_control_panel import AlarmControlPanelState
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant

from custom_components.hikvision_axpro import (
    HikAxProDataUpdateCoordinator,
    async_setup,
    async_setup_entry,
    async_unload_entry,
)
from custom_components.hikvision_axpro import alarm_control_panel as panel
from custom_components.hikvision_axpro.config_flow import (
    CONFIGURE_SCHEMA,
    STEP_USER_DATA_SCHEMA,
    AxProOptionsFlowHandler,
    validate_input,
)
from custom_components.hikvision_axpro.const import DATA_COORDINATOR, DOMAIN

CONFIG = {
    "host": "panel.test",
    "username": "operator",
    "password": "secret",
    "enabled": True,
    "code_format": "NUMBER",
    "code": "1234",
    "use_code_arming": True,
}


def payload(arming="disarm", alarm=False, sub_id=1):
    return {
        "SubSysList": [
            {
                "SubSys": {
                    "id": sub_id,
                    "arming": arming,
                    "alarm": alarm,
                    "enabled": True,
                    "name": "Area",
                    "delayTime": 0,
                }
            }
        ]
    }


def make_coordinator(hass, interval=120, subsystems=False):
    # A strict API mock makes unexpected discovery/status methods fail the test.
    api = Mock(spec=["host", "subsystem_status", "arm_home", "arm_away", "disarm"])
    api.host = CONFIG["host"]
    api.subsystem_status.return_value = payload()
    coordinator = HikAxProDataUpdateCoordinator(
        hass,
        api,
        "00:11:22:33:44:55",
        True,
        "NUMBER",
        True,
        "1234",
        interval,
        subsystems,
    )
    return coordinator, api


@pytest.mark.parametrize(
    "interval,expected",
    [(30, 120), (0, 120), (59, 120), (60, 60), (120, 120), (300, 300)],
)
def test_runtime_interval(interval, expected, tmp_path):
    async def run():
        coordinator, _ = make_coordinator(HomeAssistant(str(tmp_path)), interval)
        assert coordinator.update_interval.total_seconds() == expected

    asyncio.run(run())


@pytest.mark.parametrize(
    "arming,alarm,expected",
    [
        ("disarm", False, "disarmed"),
        ("stay", False, "armed_home"),
        ("away", False, "armed_away"),
        ("vacation", False, "armed_vacation"),
        ("disarm", True, "triggered"),
        ("away", True, "triggered"),
    ],
)
def test_poll_and_state(arming, alarm, expected, tmp_path):
    async def run():
        coordinator, api = make_coordinator(HomeAssistant(str(tmp_path)))
        api.subsystem_status.return_value = payload(arming, alarm)
        await coordinator.async_refresh()
        assert coordinator.state == AlarmControlPanelState(expected)
        assert api.mock_calls == [call.subsystem_status()]

    asyncio.run(run())


@pytest.mark.parametrize("command", ["arm_home", "arm_away", "disarm"])
@pytest.mark.parametrize("sub_id", [None, 2])
@pytest.mark.parametrize("success", [True, False])
def test_command_one_refresh(command, sub_id, success, tmp_path):
    async def run():
        coordinator, api = make_coordinator(HomeAssistant(str(tmp_path)))
        getattr(api, command).return_value = success
        await getattr(coordinator, "async_" + command)(sub_id)
        expected = [getattr(call, command)(sub_id)]
        if success:
            expected.append(call.subsystem_status())
        assert api.mock_calls == expected

    asyncio.run(run())


def test_setup_and_unload(tmp_path):
    async def run():
        hass = HomeAssistant(str(tmp_path))
        hass.config_entries = Mock()
        api = Mock(
            spec=[
                "host",
                "get_interface_mac_address",
                "build_url",
                "make_request",
                "subsystem_status",
            ]
        )
        api.host = CONFIG["host"]
        api.get_interface_mac_address.return_value = "00:11:22:33:44:55"
        api.build_url.return_value = "device-info"
        api.make_request.return_value = SimpleNamespace(
            status_code=200,
            text="<DeviceInfo><deviceName>AX Pro</deviceName><model>DS-PWA96-M-WB</model></DeviceInfo>",
        )
        api.subsystem_status.return_value = payload()
        entry = SimpleNamespace(
            entry_id="test",
            state=ConfigEntryState.SETUP_IN_PROGRESS,
            pref_disable_polling=False,
            add_update_listener=Mock(),
            async_on_unload=Mock(),
            data={
                **CONFIG,
                "scan_interval": 30,
                "auto_bypass_on_arm": True,
                "debug": True,
            },
        )
        with (
            patch(
                "custom_components.hikvision_axpro.LiteHikAxPro", return_value=api
            ) as constructor,
            patch("custom_components.hikvision_axpro.migrate_invalid_entity_ids"),
            patch.object(
                hass.config_entries,
                "async_forward_entry_setups",
                new_callable=AsyncMock,
            ) as forward,
            patch.object(
                hass.config_entries,
                "async_unload_platforms",
                new_callable=AsyncMock,
                return_value=True,
            ) as unload,
        ):
            assert await async_setup_entry(hass, entry)
            entry.add_update_listener.assert_called_once()
            entry.async_on_unload.assert_called_with(
                entry.add_update_listener.return_value
            )
            assert constructor.call_args.args == ("panel.test", "operator", "secret")
            assert forward.call_args.args[1] == ["alarm_control_panel"]
            coordinator = hass.data[DOMAIN][entry.entry_id][DATA_COORDINATOR]
            assert coordinator.update_interval.total_seconds() == 120
            assert coordinator.code == "1234"
            assert coordinator.device_model == "DS-PWA96-M-WB"
            assert [c[0] for c in api.mock_calls] == [
                "get_interface_mac_address",
                "build_url",
                "make_request",
                "subsystem_status",
            ]
            assert await async_unload_entry(hass, entry)
            assert unload.call_args.args[1] == ["alarm_control_panel"]

    asyncio.run(run())


def test_only_reload_service(tmp_path):
    async def run():
        hass = HomeAssistant(str(tmp_path))
        await async_setup(hass, {})
        assert set(hass.services.async_services()[DOMAIN]) == {"reload"}

    asyncio.run(run())


@pytest.mark.parametrize("subsystems", [False, True])
def test_entities_codes_and_subsystems(subsystems, tmp_path):
    async def run():
        hass = HomeAssistant(str(tmp_path))
        coordinator, api = make_coordinator(hass, subsystems=subsystems)
        api.subsystem_status.return_value = payload("stay")
        coordinator._update_data()
        hass.data[DOMAIN] = {"test": {DATA_COORDINATOR: coordinator}}
        entry = SimpleNamespace(entry_id="test", data={"allow_subsystems": subsystems})
        add = Mock()
        with patch.object(panel.dr, "async_get", return_value=Mock()):
            await panel.async_setup_entry(hass, entry, add)
        entities = add.call_args.args[0]
        assert len(entities) == (2 if subsystems else 1)
        for entity in entities:
            assert entity.alarm_state == AlarmControlPanelState.ARMED_HOME
            for method in [
                "async_alarm_arm_home",
                "async_alarm_arm_away",
                "async_alarm_disarm",
            ]:
                api.reset_mock()
                await getattr(entity, method)("wrong")
                assert api.mock_calls == []
                await getattr(entity, method)("1234")
                assert len(api.mock_calls) == 2
                assert api.mock_calls[-1] == call.subsystem_status()

    asyncio.run(run())


@pytest.mark.parametrize("schema", [STEP_USER_DATA_SCHEMA, CONFIGURE_SCHEMA])
def test_config_schema(schema):
    data = schema(CONFIG)
    assert data["scan_interval"] == 120
    assert data["code"] == "1234"
    for key in ["auto_bypass_on_arm", "debug"]:
        with pytest.raises(vol.Invalid):
            schema({**CONFIG, key: True})
    with pytest.raises(vol.Invalid):
        schema({**CONFIG, "scan_interval": 30})


def test_options_legacy_interval_and_credentials(tmp_path):
    async def run():
        flow = AxProOptionsFlowHandler()
        entry = SimpleNamespace(data={**CONFIG, "scan_interval": 30})
        with patch.object(
            AxProOptionsFlowHandler, "config_entry", new=property(lambda self: entry)
        ):
            result = await flow.async_step_init()
        defaults = result["data_schema"]({})
        assert defaults["scan_interval"] == 120
        assert all(defaults[key] == value for key, value in CONFIG.items())
        hass = HomeAssistant(str(tmp_path))
        with patch(
            "custom_components.hikvision_axpro.config_flow.LiteHikAxPro"
        ) as client:
            client.return_value.connect.return_value = True
            await validate_input(hass, defaults)
            client.assert_called_once_with("panel.test", "operator", "secret")

    asyncio.run(run())


def test_manifest_and_json():
    component = (
        Path(__file__).resolve().parents[1] / "custom_components/hikvision_axpro"
    )
    for path in component.rglob("*.json"):
        json.loads(path.read_text())
    manifest = json.loads((component / "manifest.json").read_text())
    assert manifest["domain"] == DOMAIN
    assert manifest["requirements"] == ["hikaxpro==2.3.0"]
    assert manifest["version"] == "3.4.1"


@pytest.mark.parametrize(
    "bad",
    [
        {},
        {"SubSysList": []},
        payload("invalid"),
        {"SubSysList": [{"SubSys": {"id": 1, "arming": "away"}}]},
        {
            "SubSysList": [
                {
                    "SubSys": {
                        "id": 1,
                        "arming": "disarm",
                        "alarm": False,
                        "enabled": False,
                    }
                }
            ]
        },
    ],
)
def test_invalid_status_preserves_state_and_marks_unavailable(bad, tmp_path):
    async def run():
        coordinator, api = make_coordinator(HomeAssistant(str(tmp_path)))
        api.subsystem_status.return_value = payload("away")
        await coordinator.async_refresh()
        old_systems = coordinator.sub_systems
        api.subsystem_status.return_value = bad
        await coordinator.async_refresh()
        assert not coordinator.last_update_success
        assert not panel.HikAxProPanel(coordinator).available
        assert coordinator.state == AlarmControlPanelState.ARMED_AWAY
        assert coordinator.sub_systems is old_systems
        api.subsystem_status.return_value = payload()
        await coordinator.async_refresh()
        assert coordinator.last_update_success

    asyncio.run(run())


@pytest.mark.parametrize("subsystems", [False, True])
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize(
    "first,alarm,second,expected",
    [
        ("disarm", True, "away", "triggered"),
        ("arming", False, "away", "arming"),
        ("disarm", False, "stay", "armed_home"),
        ("stay", False, "away", "armed_away"),
    ],
)
def test_aggregate_states(
    first, alarm, second, expected, reverse, subsystems, tmp_path
):
    async def run():
        coordinator, api = make_coordinator(
            HomeAssistant(str(tmp_path)), subsystems=subsystems
        )
        rows = (
            payload(first, alarm)["SubSysList"]
            + payload(second, sub_id=2)["SubSysList"]
        )
        api.subsystem_status.return_value = {
            "SubSysList": rows[::-1] if reverse else rows
        }
        await coordinator.async_refresh()
        assert coordinator.state == AlarmControlPanelState(expected)
        if first == "arming":
            entity = panel.HikAxProSubPanel(coordinator, coordinator.sub_systems[1])
            assert entity.alarm_state == AlarmControlPanelState.ARMING

    asyncio.run(run())


def test_bad_initial_status_prevents_setup(tmp_path):
    from homeassistant.exceptions import ConfigEntryNotReady

    async def run():
        coordinator, api = make_coordinator(HomeAssistant(str(tmp_path)))
        coordinator.config_entry = SimpleNamespace(
            state=ConfigEntryState.SETUP_IN_PROGRESS
        )
        api.subsystem_status.return_value = {}
        with pytest.raises(ConfigEntryNotReady):
            await coordinator.async_config_entry_first_refresh()
        assert not coordinator.last_update_success
        assert coordinator.state is None

    asyncio.run(run())


def test_options_listener_reloads_entry(tmp_path):
    from custom_components.hikvision_axpro import update_listener

    async def run():
        hass = HomeAssistant(str(tmp_path))
        hass.config_entries = SimpleNamespace(async_reload=AsyncMock())
        await update_listener(hass, SimpleNamespace(entry_id="configured-panel"))
        hass.config_entries.async_reload.assert_awaited_once_with("configured-panel")

    asyncio.run(run())

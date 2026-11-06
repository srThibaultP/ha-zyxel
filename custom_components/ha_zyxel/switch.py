"""Switches for Zyxel NWA access points."""
from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import ZyxelAuthenticationError, ZyxelConnectionError
from .const import DOMAIN

BAND_LABELS = {"slot1": "2.4 GHz", "slot2": "5 GHz", "slot3": "6 GHz"}
SLOT_BANDS = {"slot1": "radio_2g", "slot2": "radio_5g", "slot3": "radio_6g"}


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up the access point switches."""
    if entry.data.get("device_type") != "nwa50ax":
        return
    stored = hass.data[DOMAIN][entry.entry_id]
    coordinator, router = stored["coordinator"], stored["router"]
    added: set[str] = set()

    @callback
    def sync_entities() -> None:
        """Add a switch for anything the AP reports for the first time."""
        data = coordinator.data or {}
        # A removed SSID is no longer reported by the AP: remember where each
        # one goes, so its switch survives a restart and can put it back.
        known = entry.options.get("ssid_slots", {})
        ssid_slots = {**known, **data.get("ssid_slots", {})}
        if ssid_slots != known:
            hass.config_entries.async_update_entry(
                entry, options={**entry.options, "ssid_slots": ssid_slots}
            )

        new: list[SwitchEntity] = []
        for slot, band in SLOT_BANDS.items():
            if slot not in added and "Active" in data.get("radios", {}).get(band, {}):
                added.add(slot)
                new.append(ZyxelRadioSwitch(coordinator, entry, router, slot))
        for key in ssid_slots:
            if key not in added:
                added.add(key)
                new.append(ZyxelSSIDSwitch(coordinator, entry, router, key))
        if "led_locator" not in added and "led_locator" in data.get("device", {}):
            added.add("led_locator")
            new.append(ZyxelLocatorSwitch(coordinator, entry, router))
        if new:
            async_add_entities(new)

    sync_entities()
    entry.async_on_unload(coordinator.async_add_listener(sync_entities))


class ZyxelAPSwitch(CoordinatorEntity, SwitchEntity):
    """Base class of the access point switches."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator, entry: ConfigEntry, router, key: str) -> None:
        """Initialize the switch."""
        super().__init__(coordinator)
        self._router = router
        self._attr_unique_id = f"{entry.entry_id}_switch_{key}"
        device = coordinator.data.get("device", {})
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=f"Zyxel {device.get('name') or device.get('model', 'AP')}",
            manufacturer="Zyxel",
            model=device.get("model", ""),
            sw_version=device.get("firmware"),
        )

    async def _send(self, method, *args) -> None:
        """Send a change to the AP, then read its state back."""
        try:
            await self.hass.async_add_executor_job(method, *args)
        except (ZyxelAuthenticationError, ZyxelConnectionError) as err:
            raise HomeAssistantError(f"Access point change failed: {err}") from err
        await self.coordinator.async_request_refresh()


class ZyxelRadioSwitch(ZyxelAPSwitch):
    """Turn one radio on or off."""

    _attr_icon = "mdi:access-point"

    def __init__(self, coordinator, entry, router, slot: str) -> None:
        """Initialize the switch."""
        super().__init__(coordinator, entry, router, f"radio_{slot}")
        self._slot = slot
        self._attr_name = f"{BAND_LABELS[slot]} radio"

    @property
    def is_on(self) -> bool | None:
        """Return whether the radio is active."""
        radios = self.coordinator.data.get("radios", {})
        return radios.get(SLOT_BANDS[self._slot], {}).get("Active")

    async def async_turn_on(self, **kwargs) -> None:
        """Turn the radio on."""
        await self._send(self._router.set_radio, self._slot, True)

    async def async_turn_off(self, **kwargs) -> None:
        """Turn the radio off."""
        await self._send(self._router.set_radio, self._slot, False)


class ZyxelSSIDSwitch(ZyxelAPSwitch):
    """Put one SSID on a radio, or take it off."""

    _attr_icon = "mdi:wifi"

    def __init__(self, coordinator, entry, router, key: str) -> None:
        """Initialize the switch."""
        super().__init__(coordinator, entry, router, f"ssid_{key}")
        self._entry = entry
        self._key = key
        self._slot, position = key.split(":")
        self._position = int(position)

    @property
    def _details(self) -> dict:
        """Return the latest profile and SSID name known for this position."""
        return self._entry.options["ssid_slots"][self._key]

    @property
    def name(self) -> str:
        """Return the name, which follows an SSID renamed on the AP."""
        return f"{BAND_LABELS[self._slot]} {self._details['ssid']}"

    @property
    def is_on(self) -> bool | None:
        """Return whether this SSID is on the radio."""
        slots = self.coordinator.data.get("ssid_slots")
        if slots is None:
            return None
        return self._key in slots

    async def async_turn_on(self, **kwargs) -> None:
        """Put the SSID on the radio."""
        await self._send(
            self._router.set_ssid, self._slot, self._position, self._details["profile"]
        )

    async def async_turn_off(self, **kwargs) -> None:
        """Take the SSID off the radio."""
        await self._send(self._router.set_ssid, self._slot, self._position, None)


class ZyxelLocatorSwitch(ZyxelAPSwitch):
    """Blink the locator LED. The AP stops it by itself after its timer."""

    _attr_icon = "mdi:led-on"
    _attr_name = "LED locator"

    def __init__(self, coordinator, entry, router) -> None:
        """Initialize the switch."""
        super().__init__(coordinator, entry, router, "led_locator")

    @property
    def is_on(self) -> bool | None:
        """Return whether the locator LED is blinking."""
        state = self.coordinator.data.get("device", {}).get("led_locator")
        return None if state is None else state.upper() == "ON"

    async def async_turn_on(self, **kwargs) -> None:
        """Start blinking."""
        await self._send(self._router.set_led_locator, True)

    async def async_turn_off(self, **kwargs) -> None:
        """Stop blinking."""
        await self._send(self._router.set_led_locator, False)

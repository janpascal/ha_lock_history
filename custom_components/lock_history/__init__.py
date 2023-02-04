import asyncio
from collections import OrderedDict, deque
import logging
import re

import voluptuous as vol

from homeassistant.const import (ATTR_ID, CONF_ID, CONF_NAME, CONF_ENTITY_ID)

from homeassistant.components import websocket_api
from homeassistant.core import callback, Event, State
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.entity_component import EntityComponent
from homeassistant.helpers.storage import Store
from homeassistant.helpers.typing import ConfigType, HomeAssistantType
from homeassistant.helpers.restore_state import RestoreEntity
import homeassistant.helpers.event
import homeassistant.util.dt as dt_util

_LOGGER = logging.getLogger(__name__)

DOMAIN = "lock_manager"
HISTORY_UPDATED_EVENT = "{}.history_updated".format(DOMAIN)

DEPENDENCIES = ["lock", "zwave_js"]

# REQUIREMENTS = ["aiofiles==0.4.0"]

SAVE_DELAY = 10
MAX_HISTORY = 99

STORAGE_KEY = DOMAIN
STORAGE_VERSION = 1

USERCODE_TYPE_BLANK = 0
USERCODE_TYPE_PIN = 1
USERCODE_TYPE_TAG = 2

CONF_USER_CODE = 'user_code'
CONF_TAGS = 'tags'
CONF_INDEX = 'index'
CONF_ZWAVE_NODE_ID = 'zwave_node_id'
CONF_ALARM_PANEL = 'alarm_panel'

ATTR_EDITABLE = 'editable'
ATTR_NAME = 'name'

USER_CODE_SCHEMA = vol.All(cv.string, cv.matches_regex(r'[0-9a-fA-F][0-9a-fA-F]( [0-9a-fA-F][0-9a-fA-F]){9}'))

CONFIG_SCHEMA = vol.Schema({
    DOMAIN: vol.Schema({
        vol.Required(CONF_ZWAVE_NODE_ID): cv.positive_int,
        vol.Required(CONF_ALARM_PANEL): cv.entity_id,
        vol.Required(CONF_TAGS): vol.Schema([ 
            vol.Schema({
                vol.Required(CONF_NAME): cv.string,
                vol.Required(CONF_USER_CODE): USER_CODE_SCHEMA,
                vol.Required(CONF_INDEX): cv.positive_int
            })
        ])
    })
}, extra=vol.ALLOW_EXTRA)

class LockHistory:
    """Manage lock history."""

    def __init__(self, hass: HomeAssistantType, component: EntityComponent,
                 lock_node_id, alarm_entity_id, config_tags):
        """Initialize lock history storage."""
        self.hass = hass
        self.component = component
        self.node_id = lock_node_id
        self.alarm_entity_id = alarm_entity_id
        self.config_tags = config_tags
        self.store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        _LOGGER.info(f"self.config_tags: {self.config_tags}")
        self._used_tags = { tag['index']: tag for tag in self.config_tags } # indexed by tag index number
        self._history = []


    async def zwave_notification_handler(self, event):
        _LOGGER.info(f"zwave notification event: {event}")
        _LOGGER.info(f"event.data: {event.data}")

        node_id = event.data.get('node_id')
        if node_id != self.node_id:
            _LOGGER.debug(f"Ignoring zwave event from node {node_id} ({type(node_id)})")
            return

        event_type = event.data.get('type')
        if event_type != 6:
            _LOGGER.debug(f"Ignoring zwave event type {event_type} ({type(event_type)})")
            return

        event_code = event.data.get('event')
        if event_code == 6:
            _LOGGER.info("New state is 6 (HOME)")
            state_string = "Home"
        elif event_code == 5:
            _LOGGER.info("New state is 5 (AWAY)")
            state_string = "Away"
        else:
            _LOGGER.info(f"Unknown new state {event_code} ({type(event_code)}), ignoring")
            return

        parameters = event.data.get('parameters') 
        user_id = parameters["userId"]
        _LOGGER.info(f"Access operation: {state_string} by {user_id} ({type(user_id)})")

        used_tag = self._used_tags[user_id]
        _LOGGER.info("Adding to history: used tag {} ({}) for new state {}".format(user_id, used_tag[CONF_NAME], state_string))
        self._history.append({
            "tagno": user_id,
            "name": used_tag[CONF_NAME],
            "user_code": used_tag[CONF_USER_CODE],
            "state": state_string,
            "date": dt_util.now().strftime("%d/%m/%Y %H:%M:%S"),
            "timestamp": dt_util.as_timestamp(dt_util.now()),
        })

        _LOGGER.debug("Current history: {}".format(self._history))
        self.hass.bus.fire(HISTORY_UPDATED_EVENT, {})
        self._async_schedule_save()


    async def alarm_trigger_handler(self, entity, old_state, new_state):
        _LOGGER.info(f"Alarm trigger event: {entity}")

        _LOGGER.info("Adding to history: alarm triggered event")
        self._history.append({
            "tagno": -1,
            "name": "Alarm!",
            "user_code": "",
            "state": "Triggered",
            "date": dt_util.now().strftime("%d/%m/%Y %H:%M:%S"),
            "timestamp": dt_util.as_timestamp(dt_util.now()),
        })

        _LOGGER.debug("Current history: {}".format(self._history))
        self.hass.bus.fire(HISTORY_UPDATED_EVENT, {})
        self._async_schedule_save()

    async def async_initialize(self):
        """Get the usercode data."""

        raw_storage = await self.store.async_load()

        try:
            self._history = raw_storage["history"]
        except KeyError:
            self._history = []

        cancel = self.hass.bus.async_listen('zwave_js_notification', self.zwave_notification_handler)
        homeassistant.helpers.event.async_track_state_change(self.hass,
            self.alarm_entity_id, self.alarm_trigger_handler, to_state="triggered")

        _LOGGER.info("async_initialize finished")

    @callback
    def _async_schedule_save(self) -> None:
        """Schedule saving the area registry."""
        self.store.async_delay_save(self._data_to_save, SAVE_DELAY)

    @callback
    def _data_to_save(self) -> dict:
        """Return data of area registry to store in a file."""
        return {
            'history': self._history[-MAX_HISTORY:]
        }

async def async_setup(hass, config):
    """Setup the hello_world component."""
    # States are in the format DOMAIN.OBJECT_ID.
    # hass.states.async_set('lock_manager.Hello_World', 'Works!')

    conf = config[DOMAIN]
    _LOGGER.debug("conf: {}".format(conf))
    tags = conf[CONF_TAGS]

    lock_node_id = conf[CONF_ZWAVE_NODE_ID]
    _LOGGER.info("lock_node_id: {}".format(lock_node_id))

    alarm_entity = conf[CONF_ALARM_PANEL]
    _LOGGER.info("Alarm control panel entity: {}".format(alarm_entity))

    component = EntityComponent(_LOGGER, DOMAIN, hass)
    manager = hass.data[DOMAIN] = LockHistory(hass, component, lock_node_id, alarm_entity, tags)
    await manager.async_initialize()

    # Register websocket APIs
    websocket_api.async_register_command(hass, ws_usercode_history)

    # Return boolean to indicate that initialization was successfully.
    return True


@websocket_api.websocket_command({
    vol.Required('type'): 'usercode/history',
})
def ws_usercode_history(hass: HomeAssistantType,
                   connection: websocket_api.ActiveConnection, msg):
    """History of usage of user code tags."""
    manager = hass.data[DOMAIN]  # type: LockHistory
    connection.send_result(msg['id'], {
        'history': list(reversed(manager._history)),
    })

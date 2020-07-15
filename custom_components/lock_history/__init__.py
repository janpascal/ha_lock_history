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

import homeassistant.components.zwave.const as zwave_const

_LOGGER = logging.getLogger(__name__)

DOMAIN = "lock_history"
HISTORY_UPDATED_EVENT = "{}.history_updated".format(DOMAIN)

DEPENDENCIES = ["lock", "zwave"]

REQUIREMENTS = ["aiofiles==0.4.0"]

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
CONF_OZW_LOG = 'ozw_log'

DEFAULT_OZW_LOG_FILENAME = "OZW_Log.txt"

ATTR_EDITABLE = 'editable'
ATTR_NAME = 'name'

USER_CODE_SCHEMA = vol.All(cv.string, cv.matches_regex(r'[0-9a-fA-F][0-9a-fA-F]( [0-9a-fA-F][0-9a-fA-F]){9}'))

CONFIG_SCHEMA = vol.Schema({
    DOMAIN: vol.Schema({
        vol.Required(CONF_ZWAVE_NODE_ID): cv.positive_int,
        vol.Optional(CONF_OZW_LOG): cv.string,
        vol.Required(CONF_TAGS): vol.Schema([ 
            vol.Schema({
                vol.Required(CONF_NAME): cv.string,
                vol.Required(CONF_USER_CODE): USER_CODE_SCHEMA
            })
        ])
    })
}, extra=vol.ALLOW_EXTRA)

class LockHistory:
    """Manage lock history."""

    def __init__(self, hass: HomeAssistantType, component: EntityComponent,
                 lock_node_id, config_tags, config_ozw_log):
        """Initialize lock history storage."""
        self.hass = hass
        self.component = component
        self.node_id = lock_node_id
        self.config_tags = config_tags
        self.store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self.ozw_log = config_ozw_log 
        self.ozw_file_offset = 0
        self.ozw_file_inode = None
        self._used_tags = {} # indexed by tag index number
        self._history = []

    def get_user_by_code(self, usercode_string):
        """ Find the user name for a given user code from the configuration.
	Return None if user code not found """

        if not self.config_tags:
            return None

        for tag in self.config_tags:
            if tag[CONF_USER_CODE] == usercode_string:
                return tag[CONF_NAME]

        return None

    # USERCODE_TYPE_BLANK: usercode is empty, or 
    # not a string, or consists of only \0 characters
    def parse_usercode(self, usercode):
        if not isinstance(usercode, str):
            return (None,USERCODE_TYPE_BLANK)
        all_blank = True
        all_digit = True
        num_digits = 0
        reading_digits = True

        for c in usercode:
            if c != '\0':
                all_blank = False
            if reading_digits:
                if c >= '0' and c <= '9':
                    num_digits += 1
                elif c == '\0':
                    reading_digits = False
                else:
                    all_digit = False
            if not reading_digits and c != '\0':
                all_digit = False

        if all_blank:
            return (None, USERCODE_TYPE_BLANK)
        if all_digit:
            return (usercode[0:num_digits], USERCODE_TYPE_PIN)
        tag_string = " ".join(["{:02x}".format(ord(c)) for c in usercode])
        return (tag_string, USERCODE_TYPE_TAG)

    async def get_last_tag_from_ozw_log(self):
        """ Parse the openzwave log file te determine the last
            tag used. Return (tag_number, 5|6).
            Both tag_number and event_code may be None
            if no event was found """

        import aiofiles
        import aiofiles.os

        s = await aiofiles.os.stat(self.ozw_log)
        _LOGGER.debug("current inode: {}; last inode: {}".format(s.st_ino, self.ozw_file_inode))
        if s.st_ino != self.ozw_file_inode:
            _LOGGER.info("New OZW log file detected")
            self.ozw_file_offset = 0
            self.ozw_file_inode = s.st_ino

        p = re.compile("Node{:03}.*Received:.*0x06, 0x0(?P<event>\\d), 0x01, 0x(?P<tag>[0-9a-fA-F][0-9a-fA-F]), 0x[0-9a-fA-F][0-9a-fA-F]".format(self.node_id))

        last_event = None
        last_tag = None
        async with aiofiles.open(self.ozw_log, mode='r') as f:
            await f.seek(self.ozw_file_offset)
            i = 0
            while True:
                line = await f.readline()
                if len(line) == 0:
                    break
                self.ozw_file_offset = await f.tell()
                line = line.strip()
                i += 1
                m = p.search(line)
                if not m:
                    continue
                last_event = m.group('event')
                last_tag = int(m.group('tag'), 16)
                _LOGGER.debug("MATCH {}: event {}, tag {}".format(i, last_event, last_tag))
            _LOGGER.debug("Last line read: {}".format(i))

        return last_tag, last_event

    async def async_initialize(self):
        """Get the usercode data."""

        try:
            raw_storage = await self.store.async_load()
            self._history = raw_storage["history"]
        except:
            self._history = []

        async def zwave_ready(event):
            network = self.hass.data[zwave_const.DATA_NETWORK]
            lock_node = network.nodes[self.node_id]

            raw_usercodes = {}
            entities = []
            for value in lock_node.get_values(
                    class_id=zwave_const.COMMAND_CLASS_USER_CODE).values():
                if value.index == 0:
                    _LOGGER.debug("Ignoring user code #0")
                    continue
                user_code = value.data
                #_LOGGER.info("Usercode at slot %s is: %s", value.index, value.data)
                if not isinstance(value.data, str):
                    continue
                usercode_string, usercode_type = self.parse_usercode(value.data)
                if usercode_type != USERCODE_TYPE_BLANK:
                    _LOGGER.debug("Adding usercode at slot %s: %s", value.index, usercode_string)
                    name = self.get_user_by_code(usercode_string)
                    if not name:
                        _LOGGER.info("Usercode %s has unknown name", usercode_string)
                        name = "unknown"
                    self._used_tags[value.index] = {
                        CONF_INDEX: value.index,
                        CONF_NAME: name,
                        CONF_USER_CODE: user_code,
                    }

                        
        async def async_access_control_changed(entity_id, old_state, new_state):
            #print("Access control changed: entity {}, old {}, new {}".format(entity_id, old_state, new_state))
            _LOGGER.info("Access control changed")
            if new_state.state == "6":
                _LOGGER.info("New state is 6 (HOME)")
                state_string = "Home"
            elif new_state.state == "5":
                _LOGGER.info("New state is 5 (AWAY)")
                state_string = "Away"
            else:
                _LOGGER.info("Unknown new state, ignoring")
                return

            last_tag, last_event = await self.get_last_tag_from_ozw_log()

            if not last_event:
                _LOGGER.info("No tag event found in OZW log")
                return

            if last_event != new_state.state:
                _LOGGER.info("Last event in OZW log not equal to access control event")
                return

            used_tag = self._used_tags[last_tag]
            _LOGGER.info("Adding to history: used tag {} ({}) for new state {}".format(last_tag, used_tag[CONF_NAME], state_string))
            self._history.append({
                "tagno": last_tag,
                "name": used_tag[CONF_NAME],
                "user_code": used_tag[CONF_USER_CODE],
                "state": state_string,
                "date": dt_util.now().strftime("%d/%m/%Y %H:%M:%S"),
                "timestamp": dt_util.as_timestamp(dt_util.now()),
            })

            _LOGGER.debug("Current history: {}".format(self._history))
            self.hass.bus.fire(HISTORY_UPDATED_EVENT, {})
            self._async_schedule_save()

        # Listen for when example_component_my_cool_event is fired
        self.hass.bus.async_listen('zwave.network_ready', zwave_ready)
        homeassistant.helpers.event.async_track_state_change(self.hass, 'sensor.alarm_keypad_access_control', action=async_access_control_changed)

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
    """Setup the lock history component."""

    conf = config[DOMAIN]
    tags = conf[CONF_TAGS]

    lock_node_id = conf[CONF_ZWAVE_NODE_ID]

    if CONF_OZW_LOG in conf:
        ozw_log = conf[CONF_OZW_LOG]
    else:
        ozw_log = hass.config.path(DEFAULT_OZW_LOG_FILENAME)
        _LOGGER.info("Using default OpenZWave log location: {}".format(ozw_log))

    component = EntityComponent(_LOGGER, DOMAIN, hass)
    manager = hass.data[DOMAIN] = LockHistory(hass, component, lock_node_id, tags, ozw_log)
    await manager.async_initialize()

    # Register websocket API
    websocket_api.async_register_command(hass, ws_usercode_history)

    # Return boolean to indicate that initialization was successfully.
    return True


@websocket_api.websocket_command({
    vol.Required('type'): 'lock_history/history',
})
def ws_usercode_history(hass: HomeAssistantType,
                   connection: websocket_api.ActiveConnection, msg):
    """History of usage of user code tags."""
    manager = hass.data[DOMAIN]  # type: LockHistory
    connection.send_result(msg['id'], {
        'history': list(reversed(manager._history)),
    })

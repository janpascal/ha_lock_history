# Home Assistant Lock History

This is a  Home Assistant custom component that keeps track of which RFID tag was used when to lock or unlock a Z-Wave lock. 
There is also a Lovelace custom UI component to show the history in the Lovelace UI.
Up to now, this component has only been tested with a Zipato Mini RFiD Keypad.

This component has been updated to work with the zwave-js integration

Exaìple config in `configuration.yml`:

```yml
lock_manager:
  zwave_node_id: 3
  alarm_panel: 'alarm_control_panel.home_alarm'
  tags:
    - name: John
      user_code: !secret user_code_john
      index: 1
    - name: Mary
      user_code: !secret user_code_mary
      index: 2
```

With `secrets.yml` containing e.g.:

```yml
user_code_john:     "8f aa bb cc dd ee 01 04 00 00"
user_code_mary:     "8f bb cc dd ee ff 01 04 00 00"
```

User codes are actually legacy, they're ignored right now but still required in
the `configuration.yml`.

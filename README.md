a python script to send service and docker container infomation to home assistant.


<img width="401" height="930" alt="image" src="https://github.com/user-attachments/assets/bb371c9a-865c-4a25-b1a6-05c08eca72d5" />


debian install

`apt install python3-paho-mqtt python3-dotenv python3-dateutil`

dynamic card to show failing services. (replace doorman with the name of your server)
```
type: custom:auto-entities
card:
  type: entities
filter:
  include:
    - options:
        type: custom:template-entity-row
        name: >
          {{ state_attr(config.entity, 'service_name') if
          state_attr(config.entity, 'service_name') else config.entity }}
          {{ '(' ~state_attr(config.entity, 'scope') ~ ')'  if
           state_attr(config.entity, 'scope') }}        
        secondary: |
          {% set logs = state_attr(config.entity, 'LastLogs') %} {% if logs %}           
            {{ logs | regex_replace(find='(^.*]:)', replace='')}}
          {% else %}
            No information available
          {% endif %}
        tap_action:
          action: more-info
      entity_id: sensor.doorman_service_*
    - type: section
  exclude:
    - options: {}
      state: running
show_empty: false

```

and the one for containers

```
type: custom:auto-entities
card:
  type: entities
filter:
  include:
    - options:
        type: custom:template-entity-row
        name: >
          {{ state_attr(config.entity, 'container_name') if
          state_attr(config.entity, 'container_name') else config.entity }}
        secondary: >
          {% set logs = state_attr(config.entity, 'LastLogs') %} {% if logs
          %}           
            {{ logs | regex_replace(find='(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3} \w+ \d+\s+)', replace='')}}
          {% else %}
            No information available
          {% endif %}
        tap_action:
          action: more-info
      entity_id: sensor.doorman_container_*
    - type: section
  exclude:
    - options: {}
      state: running
show_empty: false
```

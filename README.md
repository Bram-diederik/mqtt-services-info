a python script to send service infomation to home assistant.
for system services and services of the user running the script. 

<img width="388" height="676" alt="image" src="https://github.com/user-attachments/assets/90e7cfa6-ede2-457e-807a-5670adea1cc1" />



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

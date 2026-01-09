a python scripts to send sytem info to mqtt


# ENV settings
```
MQTT_BROKER=your.broker.address       # IP address or hostname of your MQTT broker
MQTT_PORT=1883                         # Port number (1883 for non-SSL, 8883 for SSL)
MQTT_USER=username                     # MQTT username (optional, leave empty if none)
MQTT_PASSWORD=password                 # MQTT password (optional, leave empty if none)
MQTT_SSL=0                             # Enable SSL/TLS (1 = enabled, 0 = disabled)
SERVER_NAME=yourserver                   # Unique identifier for this server/device

#=== APT-SPECIFIC SETTINGS ===
LOG_UPDATE_INTERVAL=3                  # Seconds between log updates (float)
TAIL_LINES=30                          # Number of tail lines to display
LOG_MEMORY_LIMIT=1000                  # Maximum lines to keep in memory buffer

# === NETWORK-SPECIFIC SETTINGS ===
NETWORK_UPDATE_INTERVAL=30            # Seconds between network status updates
NETWORK_INTERFACES=wlo1,eth0          # Comma-separated interfaces to monitor
WIFI_MONITOR_AP=                      # Specific WiFi AP to monitor (optional)
SUDO_USER=username                    # Default user for service operations

# === SERVICE-SPECIFIC SETTINGS ===
UPDATE_INTERVAL=60                    # Seconds between service status checks
MEM_SENSORS=1                         # Enable memory usage sensors (1 = yes, 0 = no)
LOG_LINES_TO_FETCH=2                  # Number of log lines to retrieve per service

# Monitored Services (comma-separated)
MONITORED_SERVICES=ssh,nginx,mariadb  # System services to monitor
MONITORED_USER_SERVICES=              # User services (format: username:service)
MONITORED_DOCKER_CONTAINERS=          # Docker containers to monitor
```


<img width="401" height="930" alt="image" src="https://github.com/user-attachments/assets/bb371c9a-865c-4a25-b1a6-05c08eca72d5" />


debian install (old)

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


here is one for apt updates

```
type: vertical-stack
cards:
  - type: conditional
    conditions:
      - condition: numeric_state
        entity: sensor.apt_update_hushhush_available_upgrades
        above: 0
    card:
      type: conditional
      conditions:
        - condition: state
          entity: sensor.apt_update_hushhush_apt_status
          state: "Off"
      card:
        type: entities
        entities:
          - entity: sensor.apt_update_hushhush_available_upgrades
            name: upgrades
          - entity: button.apt_update_hushhush_apt_upgrade
            name: apt upgrade
          - entity: button.apt_update_hushhush_apt_upgrade_y
            name: apt upgrade -y
  - type: conditional
    conditions:
      - condition: state
        entity: sensor.apt_update_hushhush_apt_status
        state_not: "Off"
    card:
      type: vertical-stack
      cards:
        - type: entity
          entity: sensor.apt_update_hushhush_apt_status
          state_color: false
        - type: conditional
          conditions:
            - condition: state
              entity: sensor.apt_update_hushhush_apt_status
              state: Upgrade Complete
          card:
            type: vertical-stack
            cards:
              - type: entities
                entities:
                  - entity: button.apt_update_hushhush_clear
                    name: Clear Upgrade logs
                    action_name: CLEAR
                    type: button
                    tap_action:
                      action: call-service
                      service: mqtt.publish
                      data:
                        topic: apt_update/hushhush/command
                        payload: clear
              - type: markdown
                content: >
                  # Changelog

                  <pre>

                  {% set log =
                  state_attr('sensor.apt_update_hushhush_apt_status',
                  'changelog') %}

                  {% if log -%}

                  {{ log }}

                  {%- else -%}

                  NO output

                  {%- endif %}

                  </pre>
                card_mod:
                  style:
                    ha-markdown:
                      $: >
                        /* Direct selection of the pre element inside the shadow
                        DOM */

                        pre {
                          max-height: 300px; /* Approximately 6-8 lines */
                          overflow-y: scroll !important;
                          overflow-x: auto;
                          white-space: pre-wrap; /* Ensures lines wrap if they are too long */
                          word-break: break-all;
                          background-color: rgba(0,0,0,0.2);
                          padding: 8px;
                          display: block;
                        }
              - type: markdown
                content: >
                  # Apt-upgrade output

                  <pre>

                  {% set log =
                  state_attr('sensor.apt_update_hushhush_apt_status',
                  'full_log') %}

                  {% if log -%}

                  {{ log }}

                  {%- else -%}

                  NO output

                  {%- endif %}

                  </pre>
                card_mod:
                  style:
                    ha-markdown:
                      $: >
                        /* Direct selection of the pre element inside the shadow
                        DOM */

                        pre {
                          max-height: 300px; /* Approximately 6-8 lines */
                          overflow-y: scroll !important;
                          overflow-x: auto;
                          white-space: pre-wrap; /* Ensures lines wrap if they are too long */
                          word-break: break-all;
                          background-color: rgba(0,0,0,0.2);
                          padding: 8px;
                          display: block;
                        }
        - type: markdown
          content: >
            <pre id="apt-log"> {% set log =
            state_attr('sensor.apt_update_hushhush_apt_status', 'full_log') %} 
            {% if log %} {{ log.split('\n')[-18:] | join('\n') }} {% else %}
            Waiting for output... {% endif %} </pre>
        - type: conditional
          conditions:
            - condition: state
              entity: binary_sensor.apt_update_hushhush_config_ask
              state: "on"
          card:
            type: horizontal-stack
            cards:
              - type: entities
                entities:
                  - entity: button.apt_update_hushhush_confirm_yes
                    name: "YES"
              - type: entities
                entities:
                  - entity: button.apt_update_hushhush_confirm_no
                    name: "NO"
        - type: conditional
          conditions:
            - condition: state
              entity: binary_sensor.apt_update_hushhush_conflict
              state: "on"
          card:
            type: entities
            entities:
              - entity: button.apt_update_hushhush_show_diff
                name: Examine Diff (D)

```

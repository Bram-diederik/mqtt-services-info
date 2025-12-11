#!/usr/bin/env python3
import os
import json
import time
import socket
import subprocess
import ssl
import re
import pwd
from datetime import datetime, timezone
import dateutil.parser
from dotenv import load_dotenv
import paho.mqtt.client as mqtt

# Load environment variables from the .env file
load_dotenv()

# --- MQTT Configuration & Environment Settings ---
MQTT_BROKER = os.getenv("MQTT_BROKER")
MQTT_PORT = int(os.getenv("MQTT_PORT", 1883))
MQTT_USER = os.getenv("MQTT_USER")
MQTT_PASSWORD = os.getenv("MQTT_PASSWORD")
SERVER_NAME = os.getenv("SERVER_NAME", socket.gethostname())
MQTT_SSL = os.getenv("MQTT_SSL", "0") == "1"
UPDATE_INTERVAL = int(os.getenv("UPDATE_INTERVAL", 60)) 
MEM_SENSORS = os.getenv("MEM_SENSORS", "0") == "1" 

# --- Log Settings ---
# Fetch the last 2 log lines for both Docker and systemd
LOG_LINES_TO_FETCH = 2 

# Read and filter monitored entities from environment variables
MONITORED_SERVICES = [s.strip() for s in os.getenv("MONITORED_SERVICES", "").split(',') if s.strip()]
MONITORED_USER_SERVICES_RAW = [s.strip() for s in os.getenv("MONITORED_USER_SERVICES", "").split(',') if s.strip()]
MONITORED_DOCKER_CONTAINERS = [c.strip() for c in os.getenv("MONITORED_DOCKER_CONTAINERS", "").split(',') if c.strip()]

# The base MQTT topic path for all monitor data
MQTT_BASE_TOPIC = f"{SERVER_NAME}/monitor" 

# Global dictionaries to store the last collected memory values for publishing
service_memory_cache = {} 
docker_memory_cache = {}

# --- Helper Functions ---

def sanitize_for_ha_id(name):
    """Replaces characters that cause issues in Home Assistant (HA) entity_id's with underscores."""
    sanitized = re.sub(r'[^a-zA-Z0-9_]', '_', name).lower()
    return sanitized

def parse_user_services(raw_list):
    """
    Processes raw user service strings into (service_name, username) tuples.
    It defaults to the SUDO_USER if the format is just 'service_name'.
    """
    parsed = []
    default_user = os.getenv("SUDO_USER") or "root" 
    
    for item in raw_list:
        if not item:
            continue

        if ':' in item:
            username, service_name = item.split(':', 1)
        else:
            username = default_user
            service_name = item
        
        try:
            pwd.getpwnam(username)
            parsed.append((service_name, username))
        except KeyError:
            print(f"Warning: User '{username}' for service '{service_name}' not found on the system. Skipping this service.")
    return parsed

MONITORED_USER_SERVICES = parse_user_services(MONITORED_USER_SERVICES_RAW)

# --- Docker Functions ---

def get_docker_memory_usage():
    """
    Retrieves the memory usage for all running containers using 'docker stats --no-stream'.
    Updates the global docker_memory_cache.
    """
    global docker_memory_cache
    
    try:
        cmd = ["docker", "stats", "--no-stream", "--format", "{{.Name}}\t{{.MemUsage}}"]
        output = subprocess.check_output(cmd, text=True, timeout=10, encoding='utf-8')
        
        # Clear cache to only include containers currently in 'docker stats' output
        docker_memory_cache.clear() 

        for line in output.strip().split('\n'):
            if not line:
                continue
            try:
                name, usage_str = line.split('\t')
                used_mem_str = usage_str.split('/')[0].strip()
                
                match = re.match(r'(\d+\.?\d*)\s*([KMGT]?i?B)', used_mem_str, re.IGNORECASE)
                if match:
                    value = float(match.group(1))
                    unit = match.group(2).upper()
                    
                    if unit in ('KIB', 'KB'):
                        mem_mib = value / 1024
                    elif unit in ('MIB', 'MB'):
                        mem_mib = value
                    elif unit in ('GIB', 'GB'):
                        mem_mib = value * 1024
                    elif unit in ('TIB', 'TB'):
                        mem_mib = value * 1024 * 1024
                    else:
                        continue 

                    # Only running containers from stats update the cache with a value > 0
                    docker_memory_cache[name] = mem_mib
            except Exception as e:
                print(f"Warning: Could not parse Docker stats line '{line.strip()}'. Error: {e}")
                
    except subprocess.CalledProcessError as e:
        print(f"Error executing 'docker stats'. Is Docker running and is the user authorized? {e}")
    except Exception as e:
        print(f"General error during Docker memory retrieval: {e}")


def get_docker_container_details(container_name):
    """
    Retrieves the status, attributes, and last log entries for a single Docker container.
    """
    global docker_memory_cache # Must be declared here to write to it later

    details = {
        "status": "unknown",
        "attributes": {
            "container_name": container_name,
        },
        "logs_raw": []
    }
    
    # Default memory value: 0.0 MiB, updated only if running/found
    mem_mib = 0.0 
    
    # 1. Retrieve Status, State, and Uptime 
    try:
        cmd_inspect = ["docker", "inspect", "--format", "{{.State.Status}}\t{{.State.StartedAt}}\t{{.Config.Image}}", container_name]
        output = subprocess.check_output(
            cmd_inspect,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
            encoding='utf-8' 
        )
        
        status, started_at_str, image_name = output.strip().split('\t')
        
        if status == "running":
            details["status"] = "running"
            # If running, try to get memory from the cache populated by docker stats
            cached_mem = docker_memory_cache.get(container_name)
            if cached_mem is not None:
                mem_mib = cached_mem
            
        elif status in ["exited", "dead"]:
            details["status"] = "stopped"
        else:
            details["status"] = status
            
        details["attributes"]["State"] = status.capitalize()
        details["attributes"]["Image"] = image_name
        
        if started_at_str and started_at_str != "0001-01-01T00:00:00Z":
            try:
                dt_started = dateutil.parser.isoparse(started_at_str)
                time_diff = datetime.now(timezone.utc) - dt_started.astimezone(timezone.utc)

                total_seconds = int(time_diff.total_seconds())
                days = total_seconds // 86400
                hours = (total_seconds % 86400) // 3600
                minutes = (total_seconds % 3600) // 60
                
                details["attributes"]["ActiveSince"] = f"{days}d {hours}h {minutes}m"
                details["attributes"]["StartedAt"] = started_at_str
            except Exception:
                details["attributes"]["ActiveSince"] = "N/A"
                details["attributes"]["StartedAt"] = started_at_str
        else:
            details["attributes"]["ActiveSince"] = "N/A"
            details["attributes"]["StartedAt"] = "N/A"
            
    except subprocess.CalledProcessError:
        details["status"] = "not_found"
        details["attributes"]["LastLogs"] = "Container not found or Docker not running."
    except Exception:
        details["status"] = "error"
        details["attributes"]["LastLogs"] = "General error retrieving Docker details."
        pass
        
    # 2. Add Memory Usage (Robust logic: uses mem_mib which is 0.0 unless successfully retrieved)
    details["attributes"]["MemoryUsage"] = f"{mem_mib:.2f} MB"
    
    # Update memory cache for the separate sensor with the determined value (0.0 if not running)
    docker_memory_cache[container_name] = mem_mib
        
    # 3. Retrieve the last LOG_LINES_TO_FETCH log lines
    try:
        # Using shell=True for potentially cleaner execution on Debian/Docker
        cmd_logs = f"docker logs --tail {LOG_LINES_TO_FETCH} {container_name}"
        
        result = subprocess.run(
            cmd_logs,
            capture_output=True,
            text=True,
            timeout=5,
            encoding='utf-8',
            shell=True 
        )

        log_output = result.stdout.strip()
        
        if not log_output and result.stderr.strip():
            log_output = result.stderr.strip()
            
        details["attributes"]["LastLogs"] = log_output
        
    except subprocess.CalledProcessError as e:
        error_message = e.stderr.strip() if e.stderr else "Unknown Docker logs error."
        details["attributes"]["LastLogs"] = f"Logs could not be retrieved. Error: {error_message}"
    except Exception as e:
        if details["status"] not in ["not_found", "error"]:
            details["attributes"]["LastLogs"] = f"General error retrieving logs: {e}"
        
    return details

def collect_all_docker_details():
    """Retrieves details for all configured Docker containers."""
    all_details = {}
    # First, populate the cache with memory usage of *running* containers
    get_docker_memory_usage()

    for container in MONITORED_DOCKER_CONTAINERS:
        key = f"{container}_docker"
        details = get_docker_container_details(container)
        details["scope"] = "docker"
        all_details[key] = details

    return all_details
    
# --- Service Functions ---

def get_service_details(service_name, scope="system", username=None):
    """
    Retrieves the status, structured attributes, and last log entries for a systemd unit
    using 'systemctl' and 'journalctl'. Handles both system and user scopes.
    """
    # FIX: Move global declaration to the top
    global service_memory_cache 
    
    details = {
        "status": "unknown",
        "attributes": {
            "service_name": service_name,
        },
        "logs_raw": []
    }

    cmd_show = ["systemctl", "show", service_name]
    cmd_logs = ["journalctl", "-u", service_name, "-n", str(LOG_LINES_TO_FETCH), "--no-pager", "--output=short-iso"]

    if scope == "user" and username:
        try:
            user_info = pwd.getpwnam(username)
            user_uid = user_info.pw_uid

            xdg_runtime_dir = f"/run/user/{user_uid}"
            dbus_address = f"unix:path={xdg_runtime_dir}/bus"

            shell_command_show = (
                f"XDG_RUNTIME_DIR='{xdg_runtime_dir}' "
                f"DBUS_SESSION_BUS_ADDRESS='{dbus_address}' "
                f"systemctl --user show {service_name}"
            )
            shell_command_logs = (
                f"XDG_RUNTIME_DIR='{xdg_runtime_dir}' "
                f"DBUS_SESSION_BUS_ADDRESS='{dbus_address}' "
                f"journalctl --user -u {service_name} -n {LOG_LINES_TO_FETCH} --no-pager --output=short-iso"
            )

            cmd_show = ["sudo", "-u", username, "sh", "-c", shell_command_show]
            cmd_logs = ["sudo", "-u", username, "sh", "-c", shell_command_logs]

        except KeyError:
             details["status"] = "user_not_found_on_system"
             details["attributes"]["LastLogs"] = f"User '{username}' does not exist on the system."
             details["attributes"]["MemoryUsage"] = "0.00 MB" 
             service_memory_cache[f"{service_name}_{username}_user"] = 0.0
             return details
        except Exception as e:
             details["status"] = "user_env_error"
             details["attributes"]["LastLogs"] = f"Error setting up environment for user '{username}': {e}"
             details["attributes"]["MemoryUsage"] = "0.00 MB"
             service_memory_cache[f"{service_name}_{username}_user"] = 0.0
             return details

    # Default memory value: 0.0 MiB, updated only if running/found
    mem_mib = 0.0 
    
    try:
        output = subprocess.check_output(
            cmd_show,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
            encoding='utf-8'
        )

        systemctl_data = {}
        for line in output.strip().split('\n'):
            if '=' in line:
                key, value = line.split('=', 1)
                systemctl_data[key] = value

        active_state = systemctl_data.get("ActiveState", "unknown")

        if active_state == "active":
            details["status"] = "running"
            
            # Try to get memory from systemctl only if active
            mem_bytes = systemctl_data.get("MemoryCurrent", None)
            if mem_bytes and mem_bytes.isdigit():
                mem_mib = int(mem_bytes) / (1024*1024)
                
        elif active_state in ["inactive", "failed"]:
            details["status"] = "stopped"
        else:
            details["status"] = active_state
            
        details["attributes"]["UnitFileState"] = systemctl_data.get("UnitFileState", "N/A")
        details["attributes"]["SubState"] = systemctl_data.get("SubState", "N/A")
        details["attributes"]["MainPID"] = systemctl_data.get("ExecMainPID", "N/A")
        
        if systemctl_data.get("ActiveEnterTimestamp"):
            try:
                dt_active = dateutil.parser.parse(systemctl_data["ActiveEnterTimestamp"])
                dt_active_utc_aware = dt_active.astimezone(timezone.utc).replace(tzinfo=None)
                time_diff = datetime.now(timezone.utc).replace(tzinfo=None) - dt_active_utc_aware

                total_seconds = int(time_diff.total_seconds())
                days = total_seconds // 86400
                hours = (total_seconds % 86400) // 3600
                minutes = (total_seconds % 3600) // 60
                details["attributes"]["ActiveSince"] = f"{days}d {hours}h {minutes}m"
            except Exception:
                details["attributes"]["ActiveSince"] = systemctl_data["ActiveEnterTimestamp"]
        else:
            details["attributes"]["ActiveSince"] = "N/A"

    except subprocess.CalledProcessError:
        details["status"] = "not_found"
        details["attributes"]["LastLogs"] = "Unit could not be found or accessed."
    except Exception:
        details["status"] = "error"
        details["attributes"]["LastLogs"] = "General error retrieving details."
        pass

    # Memory logic (Robust: uses mem_mib which is 0.0 unless successfully retrieved above)
    cache_key = f"{service_name}_{scope}"
    if scope == "user" and username:
         cache_key = f"{service_name}_{username}_user"
         
    details["attributes"]["MemoryUsage"] = f"{mem_mib:.2f} MB"

    service_memory_cache[cache_key] = mem_mib

    # Logs ophalen
    try:
        log_output = subprocess.check_output(
            cmd_logs,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
            encoding='utf-8'
        )
        details["attributes"]["LastLogs"] = log_output.strip()
    except Exception:
        if details["status"] not in ["not_found", "error"]:
            details["attributes"]["LastLogs"] = "Logs could not be retrieved (journalctl error)."

    return details


def collect_all_service_details():
    """Retrieves details for all configured system and user services."""
    all_details = {}

    for service in MONITORED_SERVICES:
        key = f"{service}_system"
        details = get_service_details(service, scope="system")
        details["scope"] = "system" 
        all_details[key] = details

    for service_name, username in MONITORED_USER_SERVICES:
        key = f"{service_name}_{username}_user"
        details = get_service_details(service_name, scope="user", username=username)
        details["scope"] = "user" 
        details["username"] = username 
        all_details[key] = details

    return all_details


# --- Publish/Discovery Functions ---

def publish_auto_discovery(client):
    """
    Publishes Home Assistant MQTT Auto Discovery configuration for all
    individual service and docker sensors.
    """
    if not MONITORED_SERVICES and not MONITORED_USER_SERVICES and not MONITORED_DOCKER_CONTAINERS:
        return

    print("Publishing Home Assistant Auto Discovery configurations...")

    def create_discovery_payload(name, scope, icon, username=None, is_memory_sensor=False):
        
        sanitized_name = sanitize_for_ha_id(name)
        
        if scope == "docker":
            unique_slug = "container"
            name_prefix = "Container"
            name_suffix = " (Docker)"
            topic_suffix = "_docker"
            service_id_part = f"{sanitized_name}_docker"
        else: # system or user (Retain 'service' slug for existing entities)
            unique_slug = "service"
            name_prefix = "Service"
            name_suffix = f" ({username.capitalize()})" if scope == "user" else ""
            topic_suffix = f"_{username}_user" if scope == "user" else ""
            service_id_part = f"{sanitized_name}_{username}" if scope == "user" else sanitized_name
        
        
        base_topic = f"{MQTT_BASE_TOPIC}/{name}{topic_suffix}"
        
        if is_memory_sensor:
            unique_id_slug = f"{SERVER_NAME}_mem_{unique_slug}_{service_id_part}" 
            object_id = f"{SERVER_NAME}_mem_{unique_slug}_{service_id_part}"
            name_display = f"{name.capitalize()} {name_prefix} Memory{name_suffix}"
            state_topic = f"{base_topic}/memory" 
            
            payload = {
                "name": name_display,
                "state_topic": state_topic,
                "unit_of_measurement": "MiB",
                "icon": "mdi:memory",
                "device_class": "data_size",
                "unique_id": unique_id_slug,
                "object_id": object_id,
                "retain": True,
            }
        else:
            unique_id_slug = f"{SERVER_NAME}_{unique_slug}_{service_id_part}" 
            object_id = f"{SERVER_NAME}_{unique_slug}_{service_id_part}"
            
            name_display = f"{name.capitalize()} {name_prefix}{name_suffix}"
            state_topic = base_topic
            
            payload = {
                "name": name_display,
                "state_topic": state_topic,
                "value_template": "{{ value_json.status }}",
                "icon": icon,
                "unique_id": unique_id_slug,
                "object_id": object_id,
                "json_attributes_template": "{{ value_json.attributes | tojson }}",
                "json_attributes_topic": state_topic,
                "retain": True,
            }

        payload["device"] = {
            "identifiers": [SERVER_NAME],
            "name": SERVER_NAME,
            "manufacturer": "Linux/Docker Monitor",
            "model": "MQTT Service Watcher",
        }
        
        return payload

    all_monitored_entities = (
        [(s, "system", None, "mdi:cogs") for s in MONITORED_SERVICES] +
        [(s, "user", u, "mdi:account-circle") for s, u in MONITORED_USER_SERVICES] +
        [(c, "docker", None, "mdi:docker") for c in MONITORED_DOCKER_CONTAINERS]
    )

    total_entities = 0
    for name, scope, username, icon in all_monitored_entities:
        # 1. Status/Attribute Sensor
        payload_status = create_discovery_payload(name, scope, icon, username, is_memory_sensor=False)
        topic_status = f"homeassistant/sensor/{payload_status['object_id']}/config"
        client.publish(topic_status, json.dumps(payload_status), retain=True)
        total_entities += 1

        # 2. Memory Sensor (if enabled)
        if MEM_SENSORS:
            payload_mem = create_discovery_payload(name, scope, icon, username, is_memory_sensor=True)
            topic_mem = f"homeassistant/sensor/{payload_mem['object_id']}/config"
            client.publish(topic_mem, json.dumps(payload_mem), retain=True)
            total_entities += 1
            
    print(f"Published {total_entities} discovery topics for HA.")


def publish_current_data(client, all_details):
    """Publishes the current status and attributes, and memory data to MQTT."""
    
    print(f"Publishing {len(all_details)} services/containers data to MQTT...")
    
    # Publish main status and attributes
    for key, data in all_details.items():
        # key is like 'service_system' or 'container_docker'
        
        # 1. Determine base topic for status/attributes
        name = data["attributes"]["service_name"] if data["scope"] != "docker" else data["attributes"]["container_name"]
        scope = data["scope"]
        username = data.get("username")
        
        if scope == "docker":
            topic_suffix = "_docker"
            cache_key = name 
        else:
            topic_suffix = f"_{username}_user" if scope == "user" else ""
            cache_key = f"{name}_{username}_user" if scope == "user" else f"{name}_system"

        base_topic = f"{MQTT_BASE_TOPIC}/{name}{topic_suffix}"
        
        # Publish the full status/attributes JSON
        client.publish(base_topic, json.dumps(data), retain=True)

        # 2. Publish separate memory sensor data if enabled
        if MEM_SENSORS:
            mem_value = service_memory_cache.get(cache_key)
            if mem_value is None:
                mem_value = docker_memory_cache.get(cache_key)

            if mem_value is not None:
                topic_mem = f"{base_topic}/memory"
                # Publish as a raw float/string value
                client.publish(topic_mem, f"{mem_value:.2f}", retain=True)
                

# --- Main Logic ---

def on_connect(client, userdata, flags, rc):
    """Callback function when the client connects to the MQTT broker."""
    if rc == 0:
        print(f"Connected successfully to MQTT Broker: {MQTT_BROKER}:{MQTT_PORT}")
        # Publish discovery payload on connect
        publish_auto_discovery(client)
    else:
        print(f"Connection failed with code {rc}")

def setup_mqtt_client():
    """Initializes and configures the MQTT client."""
    client = mqtt.Client(client_id=f"service_monitor_{SERVER_NAME}")
    client.on_connect = on_connect
    
    if MQTT_USER and MQTT_PASSWORD:
        client.username_pw_set(MQTT_USER, MQTT_PASSWORD)
    
    if MQTT_SSL:
        try:
            # Setting TLS version is often necessary for compatibility
            client.tls_set(tls_version=ssl.PROTOCOL_TLS_CLIENT) 
        except Exception as e:
            print(f"Error setting up SSL/TLS: {e}")
            return None
            
    try:
        client.connect(MQTT_BROKER, MQTT_PORT, 60)
    except Exception as e:
        print(f"Failed to connect to MQTT broker at {MQTT_BROKER}:{MQTT_PORT}. Error: {e}")
        return None
        
    client.loop_start()
    return client

def main():
    """Main execution loop."""
    if not MQTT_BROKER:
        print("Error: MQTT_BROKER environment variable is not set.")
        return

    client = setup_mqtt_client()
    if not client:
        return
    
    # Wait for successful connection and discovery publication
    time.sleep(2) 

    print(f"Starting service monitoring loop (Update Interval: {UPDATE_INTERVAL} seconds)...")

    while True:
        try:
            # 1. Collect all service details (system/user)
            service_details = collect_all_service_details()

            # 2. Collect all docker container details
            docker_details = collect_all_docker_details()
            
            # 3. Combine and publish
            all_details = {**service_details, **docker_details}
            publish_current_data(client, all_details)

        except Exception as e:
            print(f"An unexpected error occurred during data collection/publishing: {e}")
            
        time.sleep(UPDATE_INTERVAL)

if __name__ == "__main__":
    main()

#!/usr/bin/env python3
import os
import json
import time
import socket
import subprocess
import ssl
from datetime import datetime, timezone, timedelta
import dateutil.parser
from dotenv import load_dotenv
import paho.mqtt.client as mqtt
import pwd

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

# Read both system and user services from the environment file, filtering empty entries
MONITORED_SERVICES = [s.strip() for s in os.getenv("MONITORED_SERVICES", "").split(',') if s.strip()]
MONITORED_USER_SERVICES_RAW = [s.strip() for s in os.getenv("MONITORED_USER_SERVICES", "").split(',') if s.strip()]

# The base MQTT topic path for all service data
MQTT_BASE_TOPIC = f"{SERVER_NAME}/service"

# Global dictionary to store the last collected memory values for publishing
# Key: 'service_name_scope' -> Value: 'memory_in_mib'
service_memory_cache = {} 

# Parse user services: 'user:service' or 'service' -> (service, user)
def parse_user_services(raw_list):
    """
    Processes raw user service strings into (service_name, username) tuples.
    If 'username:service_name' format is used, it takes the specified user.
    If only 'service_name' is used, it defaults to the SUDO_USER environment variable.
    """
    parsed = []
    # Determine the default user if none is specified (the user who executed 'sudo')
    default_user = os.getenv("SUDO_USER") or "root" 
    
    for item in raw_list:
        if not item:
            continue

        if ':' in item:
            # Format: username:service_name
            username, service_name = item.split(':', 1)
        else:
            # Format: service_name (must default to SUDO_USER)
            username = default_user
            service_name = item
        
        # Verify the user exists on the system before attempting to monitor
        try:
            pwd.getpwnam(username)
            parsed.append((service_name, username))
        except KeyError:
            print(f"Warning: User '{username}' for service '{service_name}' not found on the system. Skipping this service.")
    return parsed

# The processed list of user services: [(service_name, username), ...]
MONITORED_USER_SERVICES = parse_user_services(MONITORED_USER_SERVICES_RAW)


# --- Core Functions: Service Details Retrieval ---

def get_service_details(service_name, scope="system", username=None):
    """
    Retrieves the status, structured attributes, and last log entries for a systemd unit.

    For 'user' scope, this function now explicitly sets the XDG_RUNTIME_DIR 
    to ensure systemctl --user can connect to the user's running session.

    Args:
        service_name (str): The name of the systemd unit (e.g., 'nginx').
        scope (str): The scope ('system' or 'user').
        username (str): The user of the user service (only relevant for scope='user').

    Returns:
        dict: A dictionary containing 'status' and 'attributes'.
    """
    details = {
        "status": "unknown",
        "attributes": {
            "service_name": service_name,
        },
        "logs_raw": []
    }

    # Default commands for system scope
    cmd_show = ["systemctl", "show", service_name]
    cmd_logs = ["journalctl", "-u", service_name, "-n", "5", "--no-pager", "--output=short-iso"]

    if scope == "user" and username:
        try:
            # Get the numerical User ID (UID)
            user_info = pwd.getpwnam(username)
            user_uid = user_info.pw_uid

            # Construct the essential environment variables (DBus/Systemd User Session)
            # The XDG_RUNTIME_DIR is typically /run/user/<UID>
            xdg_runtime_dir = f"/run/user/{user_uid}"

            # CRUCIAL FIX: Explicitly set the environment variables within the shell command
            # The systemctl --user needs these to connect to the session of the user (PID 1120 in your case)
            env_vars = (
                f"XDG_RUNTIME_DIR='{xdg_runtime_dir}' "
                f"DBUS_SESSION_BUS_ADDRESS='unix:path={xdg_runtime_dir}/bus' "
            )

            # Build shell commands including the --user flag and environment variables
            shell_command_show = f"{env_vars} systemctl --user show {service_name}"
            shell_command_logs = f"{env_vars} journalctl --user -u {service_name} -n 5 --no-pager --output=short-iso"
            
            # The actual command list passed to subprocess.check_output (wrapped in sudo -u)
            cmd_show = ["sudo", "-u", username, "sh", "-c", shell_command_show]
            cmd_logs = ["sudo", "-u", username, "sh", "-c", shell_command_logs]

        except KeyError:
             details["status"] = "user_not_found_on_system"
             details["attributes"]["LastLogs"] = f"User '{username}' does not exist on the system."
             return details
        except Exception as e:
             details["status"] = "user_env_error"
             details["attributes"]["LastLogs"] = f"Error setting up environment for user '{username}': {e}"
             return details


    # 1. Retrieve structured attributes using systemctl show
    try:
        output = subprocess.check_output(
            cmd_show,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5
        )

        systemctl_data = {}
        for line in output.strip().split('\n'):
            if '=' in line:
                key, value = line.split('=', 1)
                systemctl_data[key] = value

        # Determine the primary status from systemd ActiveState
        active_state = systemctl_data.get("ActiveState", "unknown")

        if active_state == "active":
            details["status"] = "running"
        elif active_state in ["inactive", "failed"]:
            details["status"] = "stopped"
        else:
            details["status"] = active_state

        # Populate attributes dictionary
        details["attributes"]["UnitFileState"] = systemctl_data.get("UnitFileState", "N/A")
        details["attributes"]["SubState"] = systemctl_data.get("SubState", "N/A")
        details["attributes"]["MainPID"] = systemctl_data.get("ExecMainPID", "N/A")
        
        # Uptime / Active Since calculation
        if systemctl_data.get("ActiveEnterTimestamp"):
            try:
                dt_active = dateutil.parser.parse(systemctl_data["ActiveEnterTimestamp"])
                # Calculate the difference between now (UTC) and activation time (UTC)
                time_diff = datetime.now(timezone.utc) - dt_active.astimezone(timezone.utc).replace(tzinfo=None)

                total_seconds = int(time_diff.total_seconds())
                days = total_seconds // 86400
                hours = (total_seconds % 86400) // 3600
                minutes = (total_seconds % 3600) // 60
                details["attributes"]["ActiveSince"] = f"{days}d {hours}h {minutes}m"
            except Exception:
                details["attributes"]["ActiveSince"] = systemctl_data["ActiveEnterTimestamp"]
        else:
            details["attributes"]["ActiveSince"] = "N/A"

        # Memory usage (reported by systemd in bytes)
        mem_bytes = systemctl_data.get("MemoryCurrent", None)
        if mem_bytes and mem_bytes.isdigit():
            # Convert bytes to megabytes (MiB)
            mem_mib = int(mem_bytes) / (1024*1024)
            details["attributes"]["MemoryUsage"] = f"{mem_mib:.2f} MB"
            
            # --- Cache the memory value for the dedicated sensor ---
            global service_memory_cache
            cache_key = f"{service_name}_{scope}"
            if scope == "user" and username:
                 cache_key = f"{service_name}_{username}_user"

            service_memory_cache[cache_key] = mem_mib
            # -----------------------------------------------------------
        else:
            details["attributes"]["MemoryUsage"] = "N/A"

    except subprocess.CalledProcessError:
        # Service unit could not be found or accessed (exit code > 0)
        details["status"] = "not_found"
        details["attributes"]["LastLogs"] = "Unit could not be found or accessed."
        return details
    except Exception:
        # General error during retrieval
        details["status"] = "error"
        details["attributes"]["LastLogs"] = "General error retrieving details."
        pass

    # 2. Retrieve the last 5 log lines using journalctl
    try:
        log_output = subprocess.check_output(
            cmd_logs,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5
        )
        details["attributes"]["LastLogs"] = log_output.strip()
    except Exception:
        # Only set log error if primary status retrieval succeeded
        if details["status"] not in ["not_found", "error"]:
            details["attributes"]["LastLogs"] = "Logs could not be retrieved (journalctl error)."

    return details


def collect_all_service_details():
    """Retrieves details for all configured system and user services."""
    all_details = {}

    # Collect details for System services
    for service in MONITORED_SERVICES:
        key = f"{service}_system"
        details = get_service_details(service, scope="system")
        details["scope"] = "system" # Add scope outside of attributes for internal logic
        all_details[key] = details

    # Collect details for User services
    for service_name, username in MONITORED_USER_SERVICES:
        key = f"{service_name}_{username}_user"
        details = get_service_details(service_name, scope="user", username=username)
        details["scope"] = "user" # Add scope outside of attributes for internal logic
        details["username"] = username # Add username for publishing logic
        all_details[key] = details

    return all_details


def publish_auto_discovery(client):
    """
    Publishes Home Assistant MQTT Auto Discovery configuration for all
    individual service sensors and the global failure counter sensor.
    """
    if not MONITORED_SERVICES and not MONITORED_USER_SERVICES:
        return

    print("Publishing Home Assistant Auto Discovery configurations...")

    def create_discovery_payload(service_name, scope, icon="mdi:watch", username=None, is_memory_sensor=False):
        
        # Determine unique identifiers
        topic_suffix = f"_{username}_user" if scope == "user" else ""
        service_id_part = f"{service_name}_{username}" if scope == "user" else service_name
        name_suffix = f" ({username.capitalize()})" if scope == "user" else ""
        
        # Sensor specific configuration
        if is_memory_sensor:
            unique_id_slug = f"{SERVER_NAME}_mem_service_{service_id_part}_{scope}"
            object_id = f"{SERVER_NAME}_mem_service_{service_id_part}"
            name = f"{service_name.capitalize()} Memory{name_suffix}"
            state_topic = f"{MQTT_BASE_TOPIC}/{service_name}{topic_suffix}/memory" 
            
            payload = {
                "name": name,
                "state_topic": state_topic,
                "unit_of_measurement": "MiB",
                "icon": "mdi:memory",
                "device_class": "data_size",
                "unique_id": unique_id_slug,
                "object_id": object_id,
            }
        else:
            unique_id_slug = f"{SERVER_NAME}_{service_name}_{username}_{scope}" if scope == "user" else f"{SERVER_NAME}_{service_name}_{scope}"
            object_id = f"{SERVER_NAME}_service_{service_name}_{username}_user" if scope == "user" else f"{SERVER_NAME}_service_{service_name}_system"
            state_topic = f"{MQTT_BASE_TOPIC}/{service_name}{topic_suffix}"
            
            payload = {
                "name": f"{service_name.capitalize()} Service{name_suffix}",
                "state_topic": state_topic,
                "value_template": "{{ value_json.status }}",
                "icon": icon,
                "unique_id": unique_id_slug,
                "object_id": object_id,
                "json_attributes_template": "{{ value_json.attributes | tojson }}",
                "json_attributes_topic": state_topic,
            }

        # Common Device Information
        payload["device"] = {
            "identifiers": [SERVER_NAME],
            "name": SERVER_NAME,
            "manufacturer": "Linux Service Monitor",
            "model": "MQTT Service Watcher",
        }
        
        return payload

    # Consolidated list of all monitored services (system and user)
    all_monitored_services = (
        [(s, "system", None) for s in MONITORED_SERVICES] +
        [(s, "user", u) for s, u in MONITORED_USER_SERVICES]
    )

    total_entities = 0
    for service, scope, username in all_monitored_services:
        # 1. Publish Status Sensor
        payload_status = create_discovery_payload(service, scope, "mdi:cogs" if scope == "system" else "mdi:account-circle", username, is_memory_sensor=False)
        discovery_topic_status = f"homeassistant/sensor/{payload_status['unique_id']}/config"
        client.publish(discovery_topic_status, json.dumps(payload_status), retain=True)
        total_entities += 1
        
        # 2. Publish Memory Sensor (if enabled)
        if MEM_SENSORS:
            payload_mem = create_discovery_payload(service, scope, username=username, is_memory_sensor=True)
            discovery_topic_mem = f"homeassistant/sensor/{payload_mem['unique_id']}/config"
            client.publish(discovery_topic_mem, json.dumps(payload_mem), retain=True)
            total_entities += 1
            
    # Discovery for Failed Services Count Sensor (Geen wijziging)
    failed_sensor_id = f"{SERVER_NAME}_failed_services_count"
    failed_sensor_topic = f"homeassistant/sensor/{failed_sensor_id}/config"
    failed_payload = {
        "name": f"{SERVER_NAME} Failed Services Count",
        "state_topic": f"{MQTT_BASE_TOPIC}/failed_services_count",
        "value_template": "{{ value_json.count }}",
        "unit_of_measurement": "services",
        "icon": "mdi:alert-circle",
        "unique_id": failed_sensor_id,
        "object_id": failed_sensor_id,
        "json_attributes_template": "{{ value_json.attributes | tojson }}",
        "json_attributes_topic": f"{MQTT_BASE_TOPIC}/failed_services_count",
        "device": {
            "identifiers": [SERVER_NAME],
            "name": SERVER_NAME,
            "manufacturer": "Linux Service Monitor",
            "model": "MQTT Service Watcher",
        }
    }
    client.publish(failed_sensor_topic, json.dumps(failed_payload), retain=True)
    total_entities += 1

    print(f"Auto Discovery complete. {total_entities} entities configured.")


def publish_values(client):
    """
    Retrieves all service details, publishes the full JSON payloads to MQTT,
    updates the failed service counter, and publishes the memory values.
    """

    all_details = collect_all_service_details()
    failed_services = []

    if not all_details:
        print("No services configured to monitor.")
        return

    print("-" * 30)
    for key, details in all_details.items():
        service = details["attributes"]["service_name"]
        scope = details["scope"] # Scope (user/system) is used for topic construction
        
        # Determine the unique topic suffix and add the username if it's a user service
        if scope == "user":
            username = details["username"]
            topic_suffix = f"_{username}_user"
            service_display = f"{service} (user:{username})"
            # Use the full key for memory cache lookup
            mem_cache_key = key 
        else:
            topic_suffix = ""
            service_display = f"{service} (system)"
            mem_cache_key = key

        # --- 1. Publish Status/Attribute Sensor (Main Sensor) ---
        topic = f"{MQTT_BASE_TOPIC}/{service}{topic_suffix}"

        # Track failed/stopped services for the counter sensor
        if details["status"] not in ["running", "not_found", "unknown"]:
            failed_services.append(service_display)

        payload = {
            "status": details["status"],
            "attributes": details["attributes"]
        }

        # Add scope and username to the attributes for Home Assistant context
        payload["attributes"]["scope"] = scope.capitalize()
        if scope == "user":
            payload["attributes"]["username"] = username

        json_payload = json.dumps(payload, indent=None)

        print(f"Service: {service_display.ljust(25)} -> Status: {details['status'].ljust(8)} -> Topic: {topic}")

        client.publish(topic, json_payload, retain=True)

        # --- 2. Publish Memory Sensor Value (Separate Sensor) ---
        if MEM_SENSORS:
            mem_topic = f"{topic}/memory"
            mem_value = service_memory_cache.get(mem_cache_key)
            
            if mem_value is not None:
                # Value is a float (MiB), publish it directly as the state payload
                client.publish(mem_topic, f"{mem_value:.2f}", retain=True)
                print(f"  Memory: {mem_value:.2f} MiB -> Topic: {mem_topic}")
            else:
                # Publish 'unavailable' if value is not set (e.g., service stopped or error)
                client.publish(mem_topic, "unavailable", retain=True)
                print(f"  Memory: Unavailable -> Topic: {mem_topic}")


    # --- 3. Publish Failed Services Count Sensor ---
    failed_count_payload = {
        "count": len(failed_services),
        "attributes": {
            "fail_services": failed_services
        }
    }
    failed_count_topic = f"{MQTT_BASE_TOPIC}/failed_services_count"

    client.publish(failed_count_topic, json.dumps(failed_count_payload), retain=True)
    print(f"\nPublished: Total {len(failed_services)} failed services.")


def main():
    """Main execution entry point: connects to MQTT and starts the monitoring loop."""

    if not MQTT_BROKER:
        print("Error: MQTT_BROKER is not set in the .env file.")
        return

    print(f"--- Service Monitor Initializing ---")
    print(f"Server Hostname: {SERVER_NAME}")
    print(f"MQTT Broker: {MQTT_BROKER}:{MQTT_PORT}")
    print(f"Secure Connection (TLS): {'Yes' if MQTT_SSL else 'No'}")
    print(f"Base Topic Path (Services): {MQTT_BASE_TOPIC}/<service>[_<user>_user]")
    
    print(f"Update Interval: {UPDATE_INTERVAL} seconds") # Print the interval
    
    user_services_list = [f'{u}:{s}' if ':' not in s else s for s, u in MONITORED_USER_SERVICES]
    print(f"Monitored Services (System): {', '.join(MONITORED_SERVICES) if MONITORED_SERVICES else 'None'}")
    print(f"Monitored Services (User): {', '.join(user_services_list) if user_services_list else 'None'}")
    print(f"Service Memory Sensors (Home Assistant): {'Enabled' if MEM_SENSORS else 'Disabled'}")


    # Initialize MQTT Client (using VERSION1 for compatibility with older paho-mqtt/Debian)
    client = mqtt.Client(
        client_id=f"{SERVER_NAME}_service_monitor",
        callback_api_version=mqtt.CallbackAPIVersion.VERSION1
    )

    if MQTT_USER:
        client.username_pw_set(MQTT_USER, MQTT_PASSWORD)

    # --- Configure SSL/TLS ---
    if MQTT_SSL:
        try:
            print("Configuring secure connection (TLS/SSL)...")
            client.tls_set(
                # Setting cert_reqs to ssl.CERT_NONE for self-signed or unverified certificates
                cert_reqs=ssl.CERT_NONE,
                tls_version=ssl.PROTOCOL_TLS
            )
        except Exception as e:
            print(f"Error configuring TLS: {e}")
            return

    # Establish Connection
    try:
        client.connect(MQTT_BROKER, MQTT_PORT, 60)
    except Exception as e:
        print(f"Error connecting to MQTT broker: {e}")
        return

    client.loop_start()

    # Publish Home Assistant Auto Discovery messages once
    publish_auto_discovery(client)

    # Start the continuous monitoring loop
    try:
        while True:
            publish_values(client)
            time.sleep(UPDATE_INTERVAL) 
    except KeyboardInterrupt:
        print("\nScript terminated by user (KeyboardInterrupt).")
    finally:
        client.loop_stop()
        client.disconnect()
        print("MQTT connection closed. Exiting application.")

if __name__ == "__main__":
    try:
        # Check for root/sudo execution context for user services to work properly
        if MONITORED_USER_SERVICES and os.geteuid() != 0:
            print("--- WARNING ---")
            print("User services (MONITORED_USER_SERVICES) are configured, but the script is not running as root.")
            print("The 'sudo -u <username>' command will fail unless you run this script via 'sudo python3 your_script.py'.")
            print("-----------------")
            time.sleep(2) 

        main()
    except Exception as e:
        print(f"Fatal error in main program execution: {e}")

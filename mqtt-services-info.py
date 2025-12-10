#!/usr/bin/env python3
import os
import json
import time
import socket
import subprocess
import ssl
from datetime import datetime, timezone
import dateutil.parser 
from dotenv import load_dotenv
import paho.mqtt.client as mqtt

# Load variables from .env file
load_dotenv()

# --- MQTT Config & Environment ---
MQTT_BROKER = os.getenv("MQTT_BROKER")
MQTT_PORT = int(os.getenv("MQTT_PORT", 1883))
MQTT_USER = os.getenv("MQTT_USER")
MQTT_PASSWORD = os.getenv("MQTT_PASSWORD")
SERVER_NAME = os.getenv("SERVER_NAME", socket.gethostname())
MQTT_SSL = os.getenv("MQTT_SSL", "0") == "1"

# Read both system and user services from the environment file
MONITORED_SERVICES = [s.strip() for s in os.getenv("MONITORED_SERVICES", "").split(',') if s.strip()]
MONITORED_USER_SERVICES = [s.strip() for s in os.getenv("MONITORED_USER_SERVICES", "").split(',') if s.strip()]

# The base MQTT topic path
MQTT_BASE_TOPIC = f"{SERVER_NAME}/service"

# --- Core Functions (Details retrieval remains unchanged) ---

def get_service_details(service_name, scope="system"):
    """
    Retrieves the status, structured attributes (PID, memory, uptime), and 
    last log entries (5 lines) for a given service.
    Works for both 'system' (default) and 'user' scopes.
    """
    details = {
        "status": "unknown",
        "attributes": {},
        "logs_raw": []
    }
    
    # Determine the systemctl command base based on the scope
    cmd_base = ["systemctl"]
    if scope == "user":
        cmd_base.append("--user") # Add --user flag for user services

    # 1. Retrieve structured attributes using systemctl show
    try:
        cmd_show = cmd_base + ["show", service_name]
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

        # Determine the primary status
        active_state = systemctl_data.get("ActiveState", "unknown")
        
        if active_state == "active":
            details["status"] = "running"
        elif active_state in ["inactive", "failed"]:
            details["status"] = "stopped"
        else:
             details["status"] = active_state

        # Populate attributes
        details["attributes"]["UnitFileState"] = systemctl_data.get("UnitFileState", "N/A")
        details["attributes"]["SubState"] = systemctl_data.get("SubState", "N/A")
        details["attributes"]["MainPID"] = systemctl_data.get("ExecMainPID", "N/A")
        
        # Uptime / Active Since calculation
        if systemctl_data.get("ActiveEnterTimestamp"):
            try:
                dt_active = dateutil.parser.parse(systemctl_data["ActiveEnterTimestamp"])
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

        # Memory (reported by systemd in bytes)
        mem_bytes = systemctl_data.get("MemoryCurrent", None)
        if mem_bytes and mem_bytes.isdigit():
            details["attributes"]["MemoryUsage"] = f"{int(mem_bytes) / (1024*1024):.2f} MB"
        else:
            details["attributes"]["MemoryUsage"] = "N/A"
            
    except subprocess.CalledProcessError as e:
        # Service could not be found (exit code > 0)
        details["status"] = "not_found"
        details["attributes"]["LastLogs"] = "Unit could not be found."
        return details
    except Exception:
        # General error retrieving details
        details["status"] = "error"
        details["attributes"]["LastLogs"] = "General error retrieving details."
        pass 

    # 2. Retrieve the last 5 log lines using journalctl
    try:
        cmd_logs = ["journalctl", "-u", service_name, "-n", "5", "--no-pager", "--output=short-iso"]
        if scope == "user":
            cmd_logs.insert(1, "--user") # Add --user flag for user journalctl

        log_output = subprocess.check_output(
            cmd_logs,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5
        )
        details["attributes"]["LastLogs"] = log_output.strip()
    except Exception:
        if details["status"] not in ["not_found", "error"]:
            details["attributes"]["LastLogs"] = "Logs could not be retrieved (journalctl error)."
    
    return details


def collect_all_service_details():
    """Retrieves details for all configured system and user services."""
    all_details = {}
    
    # System services
    for service in MONITORED_SERVICES:
        key = f"{service}_system" 
        details = get_service_details(service, scope="system")
        details["service_name"] = service
        details["scope"] = "system"
        all_details[key] = details

    # User services
    for service in MONITORED_USER_SERVICES:
        key = f"{service}_user"
        details = get_service_details(service, scope="user")
        details["service_name"] = service
        details["scope"] = "user"
        all_details[key] = details
        
    return all_details


def publish_auto_discovery(client):
    """
    Publishes Home Assistant MQTT Auto Discovery configuration for all
    individual services and the failure counter sensor.
    """
    if not MONITORED_SERVICES and not MONITORED_USER_SERVICES:
        return

    print("Publishing Home Assistant Auto Discovery...")

    def create_discovery_payload(service_name, scope, icon="mdi:watch"):
        # The MQTT topic used for state publishing MUST be unique for user/system
        topic_suffix = f"_{scope}" if scope == "user" else ""

        # De meest unieke identifier voor MQTT (om conflicten te voorkomen)
        discovery_slug = f"{SERVER_NAME}_{service_name}"
        
        # !!! WIJZIGING VOOR GEWENSTE ENTITEITS-ID !!!
        # sensor.doorman_service_libvirtd
        object_id = f"{SERVER_NAME}_service_{service_name}" 

        return {
            # Display naam bevat nu de scope voor duidelijkheid
            "name": f"Service {service_name.capitalize()}", 
            "state_topic": f"{MQTT_BASE_TOPIC}/{service_name}{topic_suffix}",
            "value_template": "{{ value_json.status }}", 
            "icon": icon,
            "unique_id": discovery_slug, # Unieke ID (interne HA sleutel): doorman_libvirtd_system
            "object_id": object_id,      # Entiteits ID (gebruikersnaam): doorman_service_libvirtd
            "json_attributes_template": "{{ value_json.attributes | tojson }}",
            "json_attributes_topic": f"{MQTT_BASE_TOPIC}/{service_name}{topic_suffix}", 
            "device": {
                "identifiers": [SERVER_NAME],
                "name": SERVER_NAME,
                "manufacturer": "Linux Service Monitor",
                "model": "MQTT Service Watcher",
            }
        }
    
    # Discovery for System Services
    for service in MONITORED_SERVICES:
        payload = create_discovery_payload(service, "system", "mdi:cogs")
        # De Discovery Topic moet de UNIQUE SLUG gebruiken om overschrijving te voorkomen
        discovery_topic = f"homeassistant/sensor/{payload['unique_id']}/config" 
        client.publish(discovery_topic, json.dumps(payload), retain=True)

    # Discovery for User Services
    for service in MONITORED_USER_SERVICES:
        payload = create_discovery_payload(service, "user", "mdi:account-circle")
        discovery_topic = f"homeassistant/sensor/{payload['unique_id']}/config" 
        client.publish(discovery_topic, json.dumps(payload), retain=True)

    # Discovery for Failed Services Count Sensor
    failed_sensor_topic = f"homeassistant/sensor/{SERVER_NAME}_failed_services_count/config"
    failed_payload = {
        "name": f"{SERVER_NAME} Failed Services Count",
        "state_topic": f"{MQTT_BASE_TOPIC}/failed_services_count",
        "value_template": "{{ value_json.count }}", 
        "unit_of_measurement": "services",
        "icon": "mdi:alert-circle",
        "unique_id": f"{SERVER_NAME}_failed_services_count",
        "object_id": f"{SERVER_NAME}_failed_services_count",
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

    print("Auto Discovery complete. Final Naming Convention: sensor.doorman_service_service_name")


def publish_values(client):
    """Retrieves all details and publishes the full JSON payloads, including the failed count."""
    
    all_details = collect_all_service_details()
    failed_services = []
    
    if not all_details:
        print("No services configured to monitor.")
        return

    print("-" * 30)
    for key, details in all_details.items():
        service = details["service_name"]
        scope = details["scope"] # Hier is de scope (user/system) al bekend
        
        # Publish topic is nog steeds uniek (service of service_user)
        topic_suffix = f"_{scope}" if scope == "user" else ""
        topic = f"{MQTT_BASE_TOPIC}/{service}{topic_suffix}"
        
        if details["status"] not in ["running", "not_found", "unknown"]:
            failed_services.append(f"{service} ({scope})")

        payload = {
            "status": details["status"],
            "attributes": details["attributes"]
        }
        
        # === DE VEREISTE WIJZIGING: VOEG SCOPE TOE ALS ATTRIBUUT ===
        payload["attributes"]["scope"] = scope.capitalize() 
        # ==========================================================
        
        json_payload = json.dumps(payload, indent=None) 

        print(f"Service: {service.ljust(15)} ({scope}) -> Status: {details['status'].ljust(8)} -> Topic: {topic} (JSON)")

        client.publish(topic, json_payload, retain=True)

    # --- Publish Failed Services Count Sensor ---
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
    """ Main program loop """

    if not MQTT_BROKER:
        print("Error: MQTT_BROKER is not set in the .env file.")
        return

    print(f"--- Service Monitor Start ---")
    print(f"Server: {SERVER_NAME}")
    print(f"Broker: {MQTT_BROKER}:{MQTT_PORT}")
    print(f"SSL Enabled: {'Yes' if MQTT_SSL else 'No'}")
    print(f"Topics: {MQTT_BASE_TOPIC}/<service>_<scope>")
    print(f"Services (System): {', '.join(MONITORED_SERVICES) if MONITORED_SERVICES else 'None'}")
    print(f"Services (User): {', '.join(MONITORED_USER_SERVICES) if MONITORED_USER_SERVICES else 'None'}")

    # Fix for the paho-mqtt ValueError on Debian/older versions:
    client = mqtt.Client(
        client_id=f"{SERVER_NAME}_service_monitor",
        callback_api_version=mqtt.CallbackAPIVersion.VERSION1
    )
    
    if MQTT_USER:
        client.username_pw_set(MQTT_USER, MQTT_PASSWORD)

    # --- SSL/TLS Implementation ---
    if MQTT_SSL:
        try:
            print("Secure connection (TLS) enabled.")
            client.tls_set(
                cert_reqs=ssl.CERT_NONE, 
                tls_version=ssl.PROTOCOL_TLS
            )
        except Exception as e:
            print(f"Error setting up TLS: {e}")
            return

    # Connect
    try:
        client.connect(MQTT_BROKER, MQTT_PORT, 60)
    except Exception as e:
        print(f"Error connecting to MQTT broker: {e}")
        return

    client.loop_start()

    publish_auto_discovery(client)

    try:
        while True:
            publish_values(client)
            time.sleep(20) 
    except KeyboardInterrupt:
        print("\nScript stopped by user.")
    finally:
        client.loop_stop()
        client.disconnect()
        print("MQTT connection closed. Exiting.")

if __name__ == "__main__":
    from datetime import timedelta 
    try:
        main()
    except Exception as e:
        print(f"Fatal error in main program: {e}")

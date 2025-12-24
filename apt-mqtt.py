"""
Debian Update Handler - MQTT Integration
English comments. Using SERVER_NAME from .env for all IDs.
"""

import os
import json
import ssl
import time
import subprocess
import pty
import re
import threading
from dotenv import load_dotenv
load_dotenv()

# ================= CONFIG =================
MQTT_BROKER = os.getenv("MQTT_BROKER")
MQTT_PORT = int(os.getenv("MQTT_PORT", 8883))
MQTT_USER = os.getenv("MQTT_USER")
MQTT_PW = os.getenv("MQTT_PASSWORD")
MQTT_SSL = os.getenv("MQTT_SSL") == "1"
SERVER_NAME = os.getenv("SERVER_NAME", "hushhush")
AUTO_RESTART_SERVICES = os.getenv("AUTO_RESTART_SERVICES") == "1"

TOPIC_BASE = f"apt_update/{SERVER_NAME}"
TOPIC_STATUS = f"{TOPIC_BASE}/apt_status"
TOPIC_ATTR = f"{TOPIC_BASE}/attributes"
TOPIC_COUNT = f"{TOPIC_BASE}/available_upgrades"
TOPIC_CMD = f"{TOPIC_BASE}/command"
TOPIC_CONFIG_STATE = f"{TOPIC_BASE}/config_state"
TOPIC_CONFLICT = f"{TOPIC_BASE}/conflict"

# ================= STATE =================
upgrade_requested = False
response_input = None
last_check_time = 0
full_log_buffer = ""
changelog_content = ""
waiting_for_answer = False
upgrade_in_progress = False
config_conflict_active = False
apt_process_active = False
last_prompt_line = ""

state_lock = threading.Lock()

# ================= MQTT =================
import paho.mqtt.client as mqtt

def on_message(client, userdata, msg):
    global response_input, upgrade_requested
    payload = msg.payload.decode().strip().lower()
    print(f"MQTT Received: {payload}")
    with state_lock:
        if payload == "start":
            if not upgrade_in_progress: upgrade_requested = True
        elif payload == "clear":
            upgrade_requested = False
            threading.Thread(target=reset_to_idle_state, daemon=True).start()
        elif payload in ["y", "n", "d"]:
            response_input = payload

client = mqtt.Client()
client.on_message = on_message
if MQTT_USER: client.username_pw_set(MQTT_USER, MQTT_PW)
if MQTT_SSL: client.tls_set(cert_reqs=ssl.CERT_REQUIRED)
client.connect(MQTT_BROKER, MQTT_PORT)
client.subscribe(TOPIC_CMD)
client.loop_start()

# ================= HELPERS =================
def reset_to_idle_state():
    """Reset all MQTT states and clear buffers."""
    global changelog_content, full_log_buffer, upgrade_requested, upgrade_in_progress
    global response_input, waiting_for_answer, config_conflict_active, last_prompt_line
    with state_lock:
        changelog_content = ""; full_log_buffer = ""; last_prompt_line = ""
        upgrade_requested = False; upgrade_in_progress = False; response_input = None
        waiting_for_answer = False; config_conflict_active = False; apt_process_active = False
    
    client.publish(TOPIC_STATUS, "0", retain=True)
    client.publish(TOPIC_CONFIG_STATE, "OFF", retain=True)
    client.publish(TOPIC_CONFLICT, "false", retain=True)
    publish_attr(full_log="", changelog_available="false", changelog="")
    print("UI State Cleared")

def publish_attr(**kwargs):
    if changelog_content and 'changelog' not in kwargs: kwargs['changelog'] = changelog_content
    if 'changelog_available' not in kwargs: kwargs['changelog_available'] = "true" if changelog_content else "false"
    client.publish(TOPIC_ATTR, json.dumps(kwargs), retain=True)

def update_percentage_in_buffer(buffer, new_line):
    lines = buffer.split('\n')
    if not re.search(r'(\d+)%', new_line): return (buffer + '\n' + new_line) if buffer else new_line
    for i in range(len(lines) - 1, -1, -1):
        if re.search(r'\d+%', lines[i]):
            lines[i] = new_line
            return '\n'.join(lines)
    return (buffer + '\n' + new_line) if buffer else new_line

def wait_for_response(prompt_text=""):
    global response_input, full_log_buffer, last_prompt_line
    with state_lock:
        response_input = None
        last_prompt_line = prompt_text
        if prompt_text and prompt_text not in full_log_buffer:
            full_log_buffer = update_percentage_in_buffer(full_log_buffer, prompt_text)
            publish_attr(full_log=full_log_buffer)
    client.publish(TOPIC_CONFIG_STATE, "yes-no", retain=True)
    start_time = time.time()
    while time.time() - start_time < 60:
        with state_lock:
            if response_input is not None:
                ans = response_input; response_input = None
                client.publish(TOPIC_CONFIG_STATE, "OFF", retain=True)
                return ans
        time.sleep(0.1)
    return "n"

def is_config_conflict_prompt(line): return "(y/i/n/o/d/z)" in line.lower()
def is_continue_prompt(line): return "[y/n]" in line.lower() and "continue" in line.lower()
def is_restart_services_prompt(line): return "restart services" in line.lower()
def is_yes_no_prompt(line): return any(x in line.lower() for x in ["[y/n]", "(y/n)", "[yes/no]"])

# ================= HOME ASSISTANT DISCOVERY =================
def setup_discovery():
    """Register entities using SERVER_NAME from .env."""
    device = {
        "identifiers": [f"apt_update_{SERVER_NAME}"],
        "name": f"Apt Update ({SERVER_NAME})",
        "manufacturer": "Debian/Kali"
    }

    # Configuration for all entities
    configs = [
        ("sensor", "available_upgrades", {"name": "Available Upgrades", "state_topic": TOPIC_COUNT, "unique_id": f"apt_{SERVER_NAME}_upgrades"}),
        ("sensor", "apt_status", {"name": "Apt Status", "state_topic": TOPIC_STATUS, "json_attributes_topic": TOPIC_ATTR, "unique_id": f"apt_{SERVER_NAME}_status"}),
        ("binary_sensor", "config_ask", {"name": "Config Ask", "state_topic": TOPIC_CONFIG_STATE, "payload_on": "yes-no", "payload_off": "OFF", "unique_id": f"apt_{SERVER_NAME}_ask"}),
        ("binary_sensor", "conflict", {"name": "Config Conflict", "state_topic": TOPIC_CONFLICT, "payload_on": "true", "payload_off": "false", "unique_id": f"apt_{SERVER_NAME}_conflict"}),
        ("button", "start_upgrade", {"name": "Start Upgrade", "command_topic": TOPIC_CMD, "payload_press": "start", "unique_id": f"apt_{SERVER_NAME}_start"}),
        ("button", "confirm_yes", {"name": "Confirm YES", "command_topic": TOPIC_CMD, "payload_press": "y", "unique_id": f"apt_{SERVER_NAME}_yes"}),
        ("button", "confirm_no", {"name": "Confirm NO", "command_topic": TOPIC_CMD, "payload_press": "n", "unique_id": f"apt_{SERVER_NAME}_no"}),
        ("button", "show_diff", {"name": "Show Diff", "command_topic": TOPIC_CMD, "payload_press": "d", "unique_id": f"apt_{SERVER_NAME}_diff"}),
        ("button", "clear", {"name": "Clear", "command_topic": TOPIC_CMD, "payload_press": "clear", "unique_id": f"apt_{SERVER_NAME}_clear"})
    ]

    for comp, sub, cfg in configs:
        cfg["device"] = device
        cfg["object_id"] = f"apt_update_{SERVER_NAME}_{sub}"
        topic = f"homeassistant/{comp}/{SERVER_NAME}_{sub}/config"
        print(f"Registering {sub} on topic: {topic}")
        client.publish(topic, json.dumps(cfg), retain=True)

    client.publish(TOPIC_STATUS, "0", retain=True)
    client.publish(TOPIC_COUNT, "0", retain=True)
    client.publish(TOPIC_CONFIG_STATE, "OFF", retain=True)
    client.publish(TOPIC_CONFLICT, "false", retain=True)
    publish_attr(full_log="")

setup_discovery()

# ================= PTY PROMPT HANDLER =================
def master_read(fd):
    global full_log_buffer, config_conflict_active, apt_process_active
    try:
        data = os.read(fd, 8192)
    except OSError: return b""
    if not data: return b""
    
    os.write(1, data)
    chunk = data.decode(errors="ignore")
    lines = chunk.splitlines(keepends=True)
    for raw_line in lines:
        line = raw_line.rstrip('\n\r')
        if line: full_log_buffer = update_percentage_in_buffer(full_log_buffer, line)
    
    publish_attr(full_log=full_log_buffer)
    
    for raw_line in lines:
        line = raw_line.rstrip('\n\r')
        if not line: continue
        if is_continue_prompt(line):
            ans = wait_for_response(line)
            os.write(fd, (ans + "\n").encode())
        elif is_config_conflict_prompt(line):
            client.publish(TOPIC_CONFLICT, "true", retain=True)
            ans = wait_for_response(line)
            client.publish(TOPIC_CONFLICT, "false", retain=True)
            os.write(fd, (ans + "\n").encode())
        elif is_restart_services_prompt(line):
            if AUTO_RESTART_SERVICES: os.write(fd, b"yes\n")
            else:
                ans = wait_for_response(line)
                os.write(fd, (ans + "\n").encode())
        elif is_yes_no_prompt(line) and apt_process_active:
            ans = wait_for_response(line)
            os.write(fd, (ans + "\n").encode())
    return data

# ================= MAIN LOOP =================
try:
    while True:
        now = time.time()
        if not upgrade_requested and now - last_check_time > 60:
            try:
                res = subprocess.check_output("apt list --upgradable 2>/dev/null", shell=True).decode()
                count = max(0, len(res.splitlines()) - 1)
                client.publish(TOPIC_COUNT, str(count), retain=True)
            except: pass
            last_check_time = now

        if upgrade_requested and not upgrade_in_progress:
            with state_lock:
                upgrade_in_progress = True; apt_process_active = True
            full_log_buffer = ""
            client.publish(TOPIC_STATUS, "Upgrading...", retain=True)
            client.publish(TOPIC_CONFIG_STATE, "processing", retain=True)
            
            env = {"DEBIAN_FRONTEND": "readline", "LC_ALL": "C", "APT_LISTCHANGES_FRONTEND": "none"}
            old_env = os.environ.copy(); os.environ.update(env)
            try:
                subprocess.run(["sudo", "apt-get", "update"], check=True)
                pty.spawn(["/usr/bin/sudo", "-E", "apt-get", "dist-upgrade"], master_read)
            finally:
                os.environ.clear(); os.environ.update(old_env)
                with state_lock:
                    apt_process_active = False; upgrade_in_progress = False; upgrade_requested = False
            client.publish(TOPIC_STATUS, "Upgrade complete", retain=True)
            client.publish(TOPIC_CONFIG_STATE, "OFF", retain=True)
            last_check_time = 0
        time.sleep(0.5)
except KeyboardInterrupt:
    client.disconnect()

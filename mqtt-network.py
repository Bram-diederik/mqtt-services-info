#!/usr/bin/env python3
import os
import json
import time
import socket
import subprocess
import re
from datetime import datetime, timezone
from dotenv import load_dotenv
import paho.mqtt.client as mqtt
import ssl

# Load environment variables from the .env file
load_dotenv()

# --- MQTT Configuration & Environment Settings ---
MQTT_BROKER = os.getenv("MQTT_BROKER")
MQTT_PORT = int(os.getenv("MQTT_PORT", 1883))
MQTT_USER = os.getenv("MQTT_USER")
MQTT_PASSWORD = os.getenv("MQTT_PASSWORD")
SERVER_NAME = os.getenv("SERVER_NAME", socket.gethostname())
MQTT_SSL = os.getenv("MQTT_SSL", "0") == "1"
UPDATE_INTERVAL = int(os.getenv("NETWORK_UPDATE_INTERVAL", 30))

# Network interface to monitor (comma-separated)
NETWORK_INTERFACES = [iface.strip() for iface in os.getenv("NETWORK_INTERFACES", "wlo1,vuurvliegje").split(',') if iface.strip()]

# Wifi AP to monitor (leave empty to scan all)
WIFI_MONITOR_AP = os.getenv("WIFI_MONITOR_AP", "")

# The base MQTT topic path for all network monitor data
MQTT_BASE_TOPIC = f"{SERVER_NAME}/network"

# Store for WiFi connection status
wifi_connection_status = {"status": "idle", "message": "", "ssid": "", "timestamp": ""}

# --- Helper Functions ---

def sanitize_for_ha_id(name):
    """Replaces characters that cause issues in Home Assistant (HA) entity_id's with underscores."""
    sanitized = re.sub(r'[^a-zA-Z0-9_]', '_', name).lower()
    return sanitized

def execute_command(cmd, timeout=10, sudo=False):
    """Execute shell command and return output."""
    try:
        if sudo:
            cmd = f"sudo {cmd}"
        
        result = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding='utf-8'
        )
        return result.returncode, result.stdout.strip(), result.stderr.strip()
    except subprocess.TimeoutExpired:
        return -1, "", "Command timed out"
    except Exception as e:
        return -1, "", str(e)

def get_wireless_interfaces():
    """Get list of wireless interfaces."""
    wireless_interfaces = []
    try:
        # Use ip link to find wireless interfaces
        code, output, _ = execute_command("ip link show 2>/dev/null | grep -E '^[0-9]+: (wlan|wlo|wlp|wlx)' | awk -F': ' '{print $2}' | awk '{print $1}'")
        if code == 0 and output:
            wireless_interfaces = output.strip().split()
    except:
        pass
    return wireless_interfaces

def calculate_signal_strength_and_icon(dbm):
    """Calculate signal strength percentage and appropriate icon from dBm value."""
    if dbm >= -50:
        signal_pct = 100
        icon = "mdi:wifi-strength-4"
    elif dbm >= -60:
        signal_pct = 80
        icon = "mdi:wifi-strength-4"
    elif dbm >= -67:
        signal_pct = 70
        icon = "mdi:wifi-strength-3"
    elif dbm >= -70:
        signal_pct = 60
        icon = "mdi:wifi-strength-3"
    elif dbm >= -80:
        signal_pct = 40
        icon = "mdi:wifi-strength-2"
    elif dbm >= -90:
        signal_pct = 20
        icon = "mdi:wifi-strength-1"
    else:
        signal_pct = 0
        icon = "mdi:wifi-strength-outline"
    
    return signal_pct, icon

def get_wireless_info(interface):
    """Get wireless-specific information for an interface."""
    wifi_info = {
        "connected_ssid": "Not connected",
        "connected_bssid": "N/A",
        "signal_strength": 0,
        "signal_dbm": "N/A",
        "frequency": "N/A",
        "frequency_band": "unknown",
        "bitrate": "N/A",
        "security": "N/A",
        "channel": "N/A",
        "ip_address": "N/A",
        "wifi_icon": "mdi:wifi-off"
    }
    
    # Try iw first (most accurate for wireless info)
    code, output, _ = execute_command(f"iw dev {interface} link 2>/dev/null")
    if code == 0 and output:
        # Parse SSID
        ssid_match = re.search(r'SSID: (.+)', output)
        if ssid_match:
            wifi_info["connected_ssid"] = ssid_match.group(1).strip()
        
        # Parse signal strength from iw (most accurate)
        signal_match = re.search(r'signal: (-\d+) dBm', output)
        if signal_match:
            dbm = int(signal_match.group(1))
            wifi_info["signal_dbm"] = f"{dbm} dBm"
            # Calculate signal strength percentage and icon
            signal_pct, wifi_icon = calculate_signal_strength_and_icon(dbm)
            wifi_info["signal_strength"] = signal_pct
            wifi_info["wifi_icon"] = wifi_icon
        
        # Parse frequency from iw
        freq_match = re.search(r'freq: (\d+)', output)
        if freq_match:
            freq_hz = int(freq_match.group(1))
            freq_ghz = freq_hz / 1000.0
            if 2400 <= freq_hz <= 2500:
                wifi_info["frequency"] = f"{freq_ghz:.3f} GHz"
                wifi_info["frequency_band"] = "2.4ghz"
            elif 5000 <= freq_hz <= 6000:
                wifi_info["frequency"] = f"{freq_ghz:.3f} GHz"
                wifi_info["frequency_band"] = "5ghz"
            else:
                wifi_info["frequency"] = f"{freq_ghz:.3f} GHz"
                wifi_info["frequency_band"] = "unknown"
    
    # Get IP address for this interface
    code, ip_output, _ = execute_command(f"ip -j addr show {interface} 2>/dev/null")
    if code == 0 and ip_output:
        try:
            ip_data = json.loads(ip_output)
            if ip_data and isinstance(ip_data, list) and len(ip_data) > 0 and 'addr_info' in ip_data[0]:
                for addr in ip_data[0]['addr_info']:
                    if addr.get('family') == 'inet':
                        wifi_info["ip_address"] = addr.get('local', 'N/A')
                        break
        except:
            pass
    
    # Clean up values
    for key, value in wifi_info.items():
        if isinstance(value, str):
            # Remove any weird characters or backslashes
            value = value.replace('\\', '').strip()
            if value == '' or value.isspace():
                if key == "connected_ssid":
                    wifi_info[key] = "Not connected"
                elif key == "signal_dbm":
                    wifi_info[key] = "N/A"
                else:
                    wifi_info[key] = "N/A"
            else:
                wifi_info[key] = value
    
    return wifi_info

# Cache for WiFi scan results - ONLY used for dropdown persistence
wifi_scan_cache = {"timestamp": 0, "networks": []}

def scan_wifi_networks():
    """Scan for available WiFi networks using nmcli - ALWAYS FRESH SCAN."""
    networks = []
    
    code, output, _ = execute_command("which nmcli 2>/dev/null")
    if code == 0:
        # Trigger fresh scan
        execute_command("nmcli device wifi rescan 2>/dev/null", sudo=True)
        time.sleep(3)  # Wait for scan to complete
        
        # Get scan results
        code, output, _ = execute_command("LANG=en_US.UTF-8 nmcli -t -f ssid,bssid,signal,freq,security,chan dev wifi 2>/dev/null | grep -v '^--'")
        if code == 0 and output:
            for line in output.strip().split('\n'):
                if line and not line.startswith('*'):
                    parts = line.split(':')
                    if len(parts) >= 6:
                        # Clean each part
                        cleaned_parts = []
                        for part in parts:
                            # Remove any backslashes and clean up
                            cleaned = part.replace('\\', '').strip()
                            if cleaned == '':
                                cleaned = "N/A"
                            cleaned_parts.append(cleaned)
                        
                        network = {
                            "ssid": cleaned_parts[0] if cleaned_parts[0] else "Hidden",
                            "bssid": cleaned_parts[1],
                            "signal": int(cleaned_parts[2]) if cleaned_parts[2].isdigit() else 0,
                            "frequency": cleaned_parts[3],
                            "frequency_band": "unknown",
                            "security": cleaned_parts[4],
                            "channel": cleaned_parts[5]
                        }
                        
                        # Determine frequency band
                        if "5" in network["frequency"]:
                            network["frequency_band"] = "5ghz"
                        elif "2.4" in network["frequency"] or "2." in network["frequency"]:
                            network["frequency_band"] = "2.4ghz"
                        
                        networks.append(network)
    
    # Update cache with fresh results
    global wifi_scan_cache
    wifi_scan_cache = {
        "timestamp": time.time(),
        "networks": networks
    }
    
    return networks

def get_cached_network_options():
    """Get cached network options for dropdown."""
    global wifi_scan_cache
    
    # Get cached networks
    cached_networks = wifi_scan_cache.get("networks", [])
    ssid_options = [network["ssid"] for network in cached_networks if network.get("ssid")]
    
    # Also include current SSID if not in cached results
    current_ssids = set()
    for interface in NETWORK_INTERFACES:
        status = get_interface_status(interface)
        if status["attributes"].get("is_wireless", False):
            current_ssid = status["attributes"].get("connected_ssid", "")
            if current_ssid and current_ssid != "Not connected":
                current_ssids.add(current_ssid)
    
    for ssid in current_ssids:
        if ssid not in ssid_options:
            ssid_options.append(ssid)
    
    return ssid_options

# --- WiFi Connection Functions ---

def connect_to_wifi(ssid, password, interface=None):
    """Connect to a WiFi network using nmcli."""
    global wifi_connection_status
    
    wifi_connection_status = {
        "status": "connecting",
        "message": f"Attempting to connect to {ssid}...",
        "ssid": ssid,
        "timestamp": datetime.now(timezone.utc).isoformat()
    }
    
    # Get wireless interface if not specified
    if not interface:
        wireless_interfaces = get_wireless_interfaces()
        if not wireless_interfaces:
            wifi_connection_status["status"] = "error"
            wifi_connection_status["message"] = "No wireless interfaces found"
            return {"success": False, "message": "No wireless interfaces found", "action": "connect_wifi"}
        interface = wireless_interfaces[0]
    
    result = {"success": False, "message": "", "action": "connect_wifi"}
    
    # Check if network is already known and configured
    code, list_output, _ = execute_command(f"nmcli -t -f NAME,TYPE connection show | grep -i ':802-11-wireless' | grep -i '{re.escape(ssid)}'")
    
    if code == 0 and list_output.strip():
        # Network is known and configured, try to connect (password not needed)
        connection_name = list_output.strip().split(':')[0]
        cmd = f"nmcli connection up '{connection_name}'"
        code, output, error = execute_command(cmd, sudo=True, timeout=30)
        
        if code == 0:
            wifi_connection_status["status"] = "connected"
            wifi_connection_status["message"] = f"Successfully connected to existing network {ssid}"
            result["success"] = True
            result["message"] = f"Connected to existing network {ssid}"
        else:
            # If existing connection fails, try to connect with new credentials
            if password:
                cmd = f"nmcli device wifi connect '{ssid}' password '{password}'"
            else:
                cmd = f"nmcli device wifi connect '{ssid}'"
            
            code, output, error = execute_command(cmd, sudo=True, timeout=30)
            
            if code == 0:
                wifi_connection_status["status"] = "connected"
                wifi_connection_status["message"] = f"Successfully connected to {ssid}"
                result["success"] = True
                result["message"] = f"Connected to {ssid}"
            else:
                wifi_connection_status["status"] = "error"
                wifi_connection_status["message"] = f"Failed to connect to {ssid}: {error or output}"
                result["message"] = error or output
    else:
        # Network is not known, create new connection
        if password:
            cmd = f"nmcli device wifi connect '{ssid}' password '{password}'"
        else:
            # Open network (no password)
            cmd = f"nmcli device wifi connect '{ssid}'"
        
        code, output, error = execute_command(cmd, sudo=True, timeout=30)
        
        if code == 0:
            wifi_connection_status["status"] = "connected"
            wifi_connection_status["message"] = f"Successfully connected to {ssid}"
            result["success"] = True
            result["message"] = f"Connected to {ssid}"
        else:
            wifi_connection_status["status"] = "error"
            wifi_connection_status["message"] = f"Failed to connect to {ssid}: {error or output}"
            result["message"] = error or output
    
    return result

# --- Network Interface Functions ---

def is_vpn_interface(interface):
    """Check if an interface is a VPN tunnel."""
    # Check for link/none in ip link output (typical for VPN tunnels)
    code, output, _ = execute_command(f"ip link show {interface} 2>/dev/null")
    if code == 0 and "link/none" in output:
        return True
    
    # Check if it's in the list of known VPN interface names
    vpn_patterns = ['tun', 'tap', 'wg', 'vpn', 'vtun', 'utun']
    interface_lower = interface.lower()
    for pattern in vpn_patterns:
        if pattern in interface_lower:
            return True
    
    # Check if interface name matches server name (common for WireGuard)
    if interface == SERVER_NAME:
        return True
    
    return False

def get_interface_status(interface):
    """Get detailed status of a network interface."""
    status = {
        "name": interface,
        "status": "unknown",
        "type": "network_interface",
        "attributes": {
            "interface": interface,
            "mac_address": "N/A",
            "ip_addresses": [],
            "ipv4_address": "N/A",
            "ipv6_address": "N/A",
            "gateway": "N/A",
            "dns_servers": [],
            "mtu": 1500,
            "speed": "N/A",
            "duplex": "N/A",
            "carrier": "down",
            "rx_bytes": 0,
            "tx_bytes": 0,
            "rx_packets": 0,
            "tx_packets": 0,
            "rx_errors": 0,
            "tx_errors": 0,
            "last_seen": datetime.now(timezone.utc).isoformat(),
            "is_wireless": False,
            "is_vpn_tunnel": False,
            "icon": "mdi:lan"
        }
    }
    
    # Check if interface exists
    code, output, error = execute_command(f"ip link show {interface} 2>/dev/null")
    if code != 0 or "does not exist" in error or "Cannot find device" in error:
        status["status"] = "not_found"
        status["attributes"]["icon"] = "mdi:lan-disconnect"
        return status
    
    # Check if it's a wireless interface
    wireless_ifs = get_wireless_interfaces()
    if interface in wireless_ifs:
        status["attributes"]["is_wireless"] = True
        # Get wireless info using more reliable methods
        wifi_info = get_wireless_info(interface)
        status["attributes"].update(wifi_info)
        # Use the WiFi icon from wireless info
        status["attributes"]["icon"] = wifi_info.get("wifi_icon", "mdi:wifi-off")
    
    # Check if it's a VPN tunnel
    if is_vpn_interface(interface):
        status["attributes"]["is_vpn_tunnel"] = True
        # Check if interface has link/none (typical for VPN tunnels)
        if "link/none" in output:
            status["attributes"]["mac_address"] = "VPN Tunnel"
    
    # Check carrier status - VPN tunnels often don't have carrier
    try:
        with open(f"/sys/class/net/{interface}/carrier", 'r') as f:
            carrier = f.read().strip()
            status["attributes"]["carrier"] = "up" if carrier == "1" else "down"
    except:
        # VPN tunnels often don't have carrier, check if interface is administratively up
        if status["attributes"]["is_vpn_tunnel"]:
            status["attributes"]["carrier"] = "n/a"
    
    # Check if interface is administratively up - FIXED FOR VPN TUNNELS
    # VPN tunnels show as "state UNKNOWN" when they're up
    if "state UP" in output or "state UNKNOWN" in output:
        status["status"] = "up"
        # For VPN tunnels with link/none, UP/LOWER_UP indicates active
        if status["attributes"]["is_vpn_tunnel"] and "LOWER_UP" in output:
            status["status"] = "up"
    else:
        status["status"] = "down"
    
    # Get MAC address (if not already set as VPN Tunnel)
    if status["attributes"]["mac_address"] == "N/A":
        mac_match = re.search(r'link/(?:ether|loopback)\s+([0-9a-f:]+)', output, re.IGNORECASE)
        if mac_match:
            status["attributes"]["mac_address"] = mac_match.group(1).upper()
    
    # Get IP addresses using ip command
    code, ip_output, _ = execute_command(f"ip -j addr show {interface} 2>/dev/null")
    if code == 0 and ip_output and ip_output.strip():
        try:
            ip_data = json.loads(ip_output)
            if ip_data and isinstance(ip_data, list) and len(ip_data) > 0 and 'addr_info' in ip_data[0]:
                for addr in ip_data[0]['addr_info']:
                    if addr.get('family') == 'inet':
                        ip_addr = f"{addr.get('local', '')}/{addr.get('prefixlen', '')}"
                        status["attributes"]["ip_addresses"].append(ip_addr)
                        status["attributes"]["ipv4_address"] = addr.get('local', 'N/A')
                    elif addr.get('family') == 'inet6' and addr.get('scope') == 'global':
                        ip_addr = f"{addr.get('local', '')}/{addr.get('prefixlen', '')}"
                        status["attributes"]["ip_addresses"].append(ip_addr)
                        status["attributes"]["ipv6_address"] = addr.get('local', 'N/A')
        except json.JSONDecodeError:
            # Fallback to text parsing
            code, ip_output, _ = execute_command(f"ip addr show {interface} 2>/dev/null")
            ip_pattern = r'inet\s+([0-9.]+)/(\d+)'
            for match in re.finditer(ip_pattern, ip_output):
                ip_addr = f"{match.group(1)}/{match.group(2)}"
                status["attributes"]["ip_addresses"].append(ip_addr)
                status["attributes"]["ipv4_address"] = match.group(1)
            
            ipv6_pattern = r'inet6\s+([0-9a-f:]+)/(\d+)'
            for match in re.finditer(ipv6_pattern, ip_output, re.IGNORECASE):
                ip_addr = f"{match.group(1)}/{match.group(2)}"
                status["attributes"]["ip_addresses"].append(ip_addr)
                status["attributes"]["ipv6_address"] = match.group(1)
    
    # Get default gateway
    code, route_output, _ = execute_command(f"ip -j route show default dev {interface} 2>/dev/null")
    if code == 0 and route_output and route_output.strip():
        try:
            route_data = json.loads(route_output)
            if route_data and isinstance(route_data, list) and len(route_data) > 0 and 'gateway' in route_data[0]:
                status["attributes"]["gateway"] = route_data[0]['gateway']
        except:
            # Fallback to text parsing
            code, route_output, _ = execute_command(f"ip route show default dev {interface} 2>/dev/null")
            if code == 0 and route_output:
                gw_match = re.search(r'default via\s+([0-9.]+)', route_output)
                if gw_match:
                    status["attributes"]["gateway"] = gw_match.group(1)
    
    # Get DNS servers from systemd-resolve or resolv.conf
    code, dns_output, _ = execute_command("systemd-resolve --status 2>/dev/null || cat /etc/resolv.conf 2>/dev/null || echo ''")
    if code == 0 and dns_output:
        if "Current DNS Server" in dns_output:
            # systemd-resolve format
            dns_matches = re.findall(r'Current DNS Server:\s+([0-9.]+)', dns_output)
            dns_matches.extend(re.findall(r'DNS Servers:\s+([0-9.]+)', dns_output))
            status["attributes"]["dns_servers"] = list(set(dns_matches))
        else:
            # resolv.conf format
            for line in dns_output.split('\n'):
                if line.startswith('nameserver'):
                    dns = line.split()[1]
                    if dns not in status["attributes"]["dns_servers"]:
                        status["attributes"]["dns_servers"].append(dns)
    
    # Get MTU
    try:
        with open(f"/sys/class/net/{interface}/mtu", 'r') as f:
            mtu = f.read().strip()
            if mtu.isdigit():
                status["attributes"]["mtu"] = int(mtu)
    except:
        pass
    
    # Get interface statistics
    stats_files = {
        'rx_bytes': 'rx_bytes',
        'tx_bytes': 'tx_bytes',
        'rx_packets': 'rx_packets',
        'tx_packets': 'tx_packets',
        'rx_errors': 'rx_errors',
        'tx_errors': 'tx_errors'
    }
    
    for attr, filename in stats_files.items():
        try:
            with open(f"/sys/class/net/{interface}/statistics/{filename}", 'r') as f:
                value = f.read().strip()
                if value.isdigit():
                    status["attributes"][attr] = int(value)
        except:
            pass
    
    # Get speed and duplex (for eth interfaces)
    try:
        with open(f"/sys/class/net/{interface}/speed", 'r') as f:
            speed = f.read().strip()
            if speed.isdigit():
                status["attributes"]["speed"] = f"{speed} Mbps"
    except:
        pass
    
    try:
        with open(f"/sys/class/net/{interface}/duplex", 'r') as f:
            duplex = f.read().strip()
            status["attributes"]["duplex"] = duplex
    except:
        pass
    
    # Determine dynamic icon - SIMPLIFIED AND FIXED
    if status["attributes"]["is_vpn_tunnel"]:
        # VPN tunnel icon
        if status["status"] == "up":
            status["attributes"]["icon"] = "mdi:shield-check"
        elif status["status"] == "not_found":
            status["attributes"]["icon"] = "mdi:lan-disconnect"
        else:
            status["attributes"]["icon"] = "mdi:shield-off"
    elif status["attributes"]["is_wireless"]:
        # WiFi icon (already set from wifi_info)
        pass
    else:
        # Wired interface icon
        if status["status"] == "up":
            status["attributes"]["icon"] = "mdi:ethernet"
        elif status["status"] == "not_found":
            status["attributes"]["icon"] = "mdi:lan-disconnect"
        else:
            status["attributes"]["icon"] = "mdi:ethernet-off"
    
    # Clean all string attributes
    for key, value in status["attributes"].items():
        if isinstance(value, str):
            # Remove backslashes and clean up
            cleaned = value.replace('\\', '').strip()
            if cleaned == '':
                cleaned = "N/A"
            status["attributes"][key] = cleaned
    
    return status

def get_all_interfaces_status():
    """Get status for all configured network interfaces."""
    all_status = {}
    
    for interface in NETWORK_INTERFACES:
        key = f"{interface}_net"
        status = get_interface_status(interface)
        all_status[key] = status
    
    return all_status

# --- MQTT Functions ---

def create_network_discovery_payload(interface_name):
    """Create MQTT discovery payload for network entities."""
    
    sanitized_name = sanitize_for_ha_id(interface_name)
    
    unique_id = f"{SERVER_NAME}_net_{sanitized_name}"
    object_id = f"{SERVER_NAME}_net_{sanitized_name}"
    name = f"{interface_name}"  # JUST the interface name
    
    # Get current status to determine initial icon
    status_data = get_interface_status(interface_name)
    initial_icon = status_data["attributes"].get("icon", "mdi:lan")
    
    payload = {
        "name": name,
        "state_topic": f"{MQTT_BASE_TOPIC}/{interface_name}",
        "value_template": "{{ value_json.status }}",
        "icon": initial_icon,  # Use dynamic icon
        "unique_id": unique_id,
        "object_id": object_id,
        "json_attributes_template": "{{ value_json.attributes | tojson }}",
        "json_attributes_topic": f"{MQTT_BASE_TOPIC}/{interface_name}",
        "retain": True,
    }
    
    payload["device"] = {
        "identifiers": [f"{SERVER_NAME}_network"],
        "name": f"{SERVER_NAME} Network",
        "manufacturer": "Linux Network Monitor",
        "model": "MQTT Network Manager",
    }
    
    return payload

def create_wifi_management_discovery():
    """Create MQTT discovery payloads for WiFi management entities."""
    discovery_payloads = []
    
    # 1. WiFi Scan Button
    scan_button = {
        "name": f"{SERVER_NAME} WiFi Scan",
        "command_topic": f"{MQTT_BASE_TOPIC}/action/scan_wifi",
        "payload_press": '{"scan": true}',
        "unique_id": f"{SERVER_NAME}_wifi_scan_button",
        "object_id": f"{SERVER_NAME}_wifi_scan_button",
        "device": {
            "identifiers": [f"{SERVER_NAME}_network"],
            "name": f"{SERVER_NAME} Network",
            "manufacturer": "Linux Network Monitor",
            "model": "MQTT Network Manager",
        },
        "icon": "mdi:wifi-sync"
    }
    discovery_payloads.append(("homeassistant/button", "wifi_scan_button", scan_button))
    
    # 2. WiFi SSID Select (dropdown)
    ssid_select = {
        "name": f"{SERVER_NAME} WiFi SSID",
        "command_topic": f"{MQTT_BASE_TOPIC}/action/set_wifi_ssid",
        "state_topic": f"{MQTT_BASE_TOPIC}/wifi_management/ssid",
        "options": [],  # Will be populated with scan results
        "unique_id": f"{SERVER_NAME}_wifi_ssid_select",
        "object_id": f"{SERVER_NAME}_wifi_ssid_select",
        "device": {
            "identifiers": [f"{SERVER_NAME}_network"],
            "name": f"{SERVER_NAME} Network",
            "manufacturer": "Linux Network Monitor",
            "model": "MQTT Network Manager",
        },
        "icon": "mdi:wifi"
    }
    discovery_payloads.append(("homeassistant/select", "wifi_ssid_select", ssid_select))
    
    # 3. WiFi Password Input
    password_input = {
        "name": f"{SERVER_NAME} WiFi Password",
        "command_topic": f"{MQTT_BASE_TOPIC}/action/set_wifi_password",
        "state_topic": f"{MQTT_BASE_TOPIC}/wifi_management/password",
        "unique_id": f"{SERVER_NAME}_wifi_password_input",
        "object_id": f"{SERVER_NAME}_wifi_password_input",
        "device": {
            "identifiers": [f"{SERVER_NAME}_network"],
            "name": f"{SERVER_NAME} Network",
            "manufacturer": "Linux Network Monitor",
            "model": "MQTT Network Manager",
        },
        "icon": "mdi:form-textbox-password"
    }
    discovery_payloads.append(("homeassistant/text", "wifi_password_input", password_input))
    
    # 4. WiFi Connect Button
    connect_button = {
        "name": f"{SERVER_NAME} Connect WiFi",
        "command_topic": f"{MQTT_BASE_TOPIC}/action/connect_wifi",
        "payload_press": '{"connect": true}',
        "unique_id": f"{SERVER_NAME}_wifi_connect_button",
        "object_id": f"{SERVER_NAME}_wifi_connect_button",
        "device": {
            "identifiers": [f"{SERVER_NAME}_network"],
            "name": f"{SERVER_NAME} Network",
            "manufacturer": "Linux Network Monitor",
            "model": "MQTT Network Manager",
        },
        "icon": "mdi:wifi-arrow-right"
    }
    discovery_payloads.append(("homeassistant/button", "wifi_connect_button", connect_button))
    
    # 5. WiFi Connection Status Sensor
    status_sensor = {
        "name": f"{SERVER_NAME} WiFi Connection Status",
        "state_topic": f"{MQTT_BASE_TOPIC}/wifi_management/status",
        "value_template": "{{ value_json.status }}",
        "json_attributes_topic": f"{MQTT_BASE_TOPIC}/wifi_management/status",
        "json_attributes_template": "{{ value_json | tojson }}",
        "unique_id": f"{SERVER_NAME}_wifi_status_sensor",
        "object_id": f"{SERVER_NAME}_wifi_status_sensor",
        "device": {
            "identifiers": [f"{SERVER_NAME}_network"],
            "name": f"{SERVER_NAME} Network",
            "manufacturer": "Linux Network Monitor",
            "model": "MQTT Network Manager",
        },
        "icon": "mdi:wifi"
    }
    discovery_payloads.append(("homeassistant/sensor", "wifi_status_sensor", status_sensor))
    
    return discovery_payloads

def publish_network_discovery(client):
    """Publish Home Assistant MQTT Auto Discovery for network entities."""
    
    print("Publishing Network Auto Discovery configurations...")
    
    # Network interfaces
    for interface in NETWORK_INTERFACES:
        payload = create_network_discovery_payload(interface)
        topic = f"homeassistant/sensor/{payload['object_id']}/config"
        client.publish(topic, json.dumps(payload), retain=True)
        
        # Get current status for logging
        status_data = get_interface_status(interface)
        icon = status_data["attributes"].get("icon", "mdi:lan")
        signal = status_data["attributes"].get("signal_strength", "N/A")
        ssid = status_data["attributes"].get("connected_ssid", "N/A")
        status = status_data["status"]
        
        print(f"  Published discovery for {interface}: Status={status}, Icon={icon}, Signal={signal}%, SSID={ssid}")
    
    # Publish WiFi management discovery
    print("Publishing WiFi Management Auto Discovery...")
    wifi_discovery_payloads = create_wifi_management_discovery()
    for base_topic, entity_type, payload in wifi_discovery_payloads:
        topic = f"{base_topic}/{payload['object_id']}/config"
        client.publish(topic, json.dumps(payload), retain=True)
        print(f"  Published discovery for {entity_type}")
    
    print(f"Published discovery for {len(NETWORK_INTERFACES)} interfaces and WiFi management")

def publish_wifi_management_status(client):
    """Publish current WiFi management status."""
    global wifi_connection_status
    
    # Publish WiFi connection status
    status_topic = f"{MQTT_BASE_TOPIC}/wifi_management/status"
    client.publish(status_topic, json.dumps(wifi_connection_status), retain=True)

def publish_network_data(client):
    """Publish current network status to MQTT."""
    
    # Get all network data
    interfaces = get_all_interfaces_status()
    
    # Publish interface status
    for key, data in interfaces.items():
        interface_name = data["name"]
        topic = f"{MQTT_BASE_TOPIC}/{interface_name}"
        
        # Refresh VPN interface status
        if data["attributes"].get("is_vpn_tunnel", False):
            # Re-check the interface status to get current state
            data = get_interface_status(interface_name)
        
        client.publish(topic, json.dumps(data), retain=True)
        
        # Debug info
        icon = data["attributes"].get("icon", "mdi:lan")
        status = data["status"]
        
        # Show interface specific info
        if data["attributes"].get("is_wireless", False):
            ssid = data["attributes"].get("connected_ssid", "Not connected")
            signal = data["attributes"].get("signal_strength", 0)
            signal_dbm = data["attributes"].get("signal_dbm", "N/A")
            print(f"  Published {interface_name}: {status}, SSID: {ssid}, Signal: {signal}% ({signal_dbm}), Icon: {icon}")
        elif data["attributes"].get("is_vpn_tunnel", False):
            print(f"  Published {interface_name}: {status} (VPN), Icon: {icon}")
        else:
            print(f"  Published {interface_name}: {status} (Ethernet), Icon: {icon}")
    
    # Publish WiFi management status
    publish_wifi_management_status(client)
    
    print(f"Published network data: {len(interfaces)} interfaces")

# --- MQTT Callbacks ---

def on_connect(client, userdata, flags, rc, properties=None):
    """Callback when connected to MQTT broker - compatible with both V1 and V2 API."""
    if rc == 0:
        print(f"Connected to MQTT broker: {MQTT_BROKER}:{MQTT_PORT}")
        # Subscribe to action topics
        client.subscribe(f"{MQTT_BASE_TOPIC}/action/#")
        # Wait a moment before publishing discovery
        time.sleep(2)
        # Publish discovery
        publish_network_discovery(client)
    else:
        print(f"Connection failed with code {rc}")

def on_message(client, userdata, msg):
    """Callback for incoming MQTT messages."""
    try:
        topic_parts = msg.topic.split('/')
        if len(topic_parts) >= 4 and topic_parts[-2] == "action":
            action = topic_parts[-1]
            
            # Handle the payload
            payload = {}
            
            if msg.payload:
                payload_str = msg.payload.decode()
                
                # Always try to parse as JSON first
                try:
                    payload = json.loads(payload_str)
                except json.JSONDecodeError:
                    # If it's not valid JSON, check if it's a password or SSID
                    if action == "set_wifi_password":
                        payload = {"password": payload_str}
                    elif action == "set_wifi_ssid":
                        payload = {"ssid": payload_str}
                    else:
                        # For other actions, just use empty payload
                        print(f"Warning: Invalid JSON payload for {action}: {msg.payload}")
                        payload = {}
            
            print(f"Received action: {action}, params: {payload}")
            
            # Execute action
            result = execute_network_action(action, payload)
            
            # Publish result
            result_topic = f"{MQTT_BASE_TOPIC}/action_result/{action}"
            client.publish(result_topic, json.dumps(result), retain=False)
            
            # If scan was performed, publish networks and update SSID dropdown
            if action == "scan_wifi" and result.get("success"):
                scan_topic = f"{MQTT_BASE_TOPIC}/wifi_scan"
                scan_data = {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "networks": result.get("networks", [])
                }
                client.publish(scan_topic, json.dumps(scan_data), retain=True)
                
                # Get fresh scan results
                fresh_networks = result.get("networks", [])
                fresh_ssids = [network["ssid"] for network in fresh_networks if network.get("ssid")]
                
                # Get cached options (for previously known networks)
                cached_options = get_cached_network_options()
                
                # Combine fresh scan with cached options (no duplicates)
                all_ssid_options = list(set(fresh_ssids + cached_options))
                
                # Update select entity with combined options
                update_select_topic = f"homeassistant/select/{SERVER_NAME}_wifi_ssid_select/config"
                ssid_select_config = {
                    "name": f"{SERVER_NAME} WiFi SSID",
                    "command_topic": f"{MQTT_BASE_TOPIC}/action/set_wifi_ssid",
                    "state_topic": f"{MQTT_BASE_TOPIC}/wifi_management/ssid",
                    "options": all_ssid_options,
                    "unique_id": f"{SERVER_NAME}_wifi_ssid_select",
                    "object_id": f"{SERVER_NAME}_wifi_ssid_select",
                    "device": {
                        "identifiers": [f"{SERVER_NAME}_network"],
                        "name": f"{SERVER_NAME} Network",
                        "manufacturer": "Linux Network Monitor",
                        "model": "MQTT Network Manager",
                    },
                    "icon": "mdi:wifi"
                }
                client.publish(update_select_topic, json.dumps(ssid_select_config), retain=True)
                
                print(f"Updated SSID dropdown with {len(all_ssid_options)} networks (fresh: {len(fresh_ssids)}, cached: {len(cached_options)})")
                
            # If WiFi connection was attempted, update status
            elif action == "connect_wifi":
                # Update WiFi connection status
                publish_wifi_management_status(client)
                
                # Force a refresh of network data to show new connection
                time.sleep(3)  # Wait for connection to establish
                publish_network_data(client)
            
    except Exception as e:
        print(f"Error processing message: {e}")

# --- Action Functions ---

def execute_network_action(action, params):
    """Execute network-related actions."""
    global wifi_connection_status
    
    result = {"success": False, "message": "", "action": action}
    
    try:
        if action == "scan_wifi":
            networks = scan_wifi_networks()
            result["success"] = True
            result["message"] = f"Scanned {len(networks)} WiFi networks"
            result["networks"] = networks
            
        elif action == "set_wifi_ssid":
            ssid = params if isinstance(params, str) else params.get("ssid", "")
            if ssid:
                # Store the selected SSID
                result["success"] = True
                result["message"] = f"SSID set to: {ssid}"
                # Store in global state for connection use
                wifi_connection_status["selected_ssid"] = ssid
                
        elif action == "set_wifi_password":
            password = params if isinstance(params, str) else params.get("password", "")
            # Store the password
            result["success"] = True
            result["message"] = "Password received"
            # Store in global state for connection use
            wifi_connection_status["selected_password"] = password
            
        elif action == "connect_wifi":
            # Get SSID and password from global state
            ssid = wifi_connection_status.get("selected_ssid", "")
            password = wifi_connection_status.get("selected_password", "")
            
            if not ssid:
                result["message"] = "No SSID selected"
                return result
            
            # Connect to the WiFi network
            result = connect_to_wifi(ssid, password)
                
        else:
            result["message"] = f"Unknown action: {action}"
            
    except Exception as e:
        result["message"] = str(e)
    
    return result

# --- Main Loop ---

def setup_mqtt_client():
    """Initialize MQTT client."""
    # Try to use V2 API, fall back to V1 if needed
    try:
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"network_monitor_{SERVER_NAME}")
    except:
        client = mqtt.Client(client_id=f"network_monitor_{SERVER_NAME}")
    
    client.on_connect = on_connect
    client.on_message = on_message
    
    if MQTT_USER and MQTT_PASSWORD:
        client.username_pw_set(MQTT_USER, MQTT_PASSWORD)
    
    if MQTT_SSL:
        try:
            client.tls_set(tls_version=ssl.PROTOCOL_TLS_CLIENT)
        except Exception as e:
            print(f"Error setting up SSL/TLS: {e}")
            return None
    
    try:
        client.connect(MQTT_BROKER, MQTT_PORT, 60)
    except Exception as e:
        print(f"Failed to connect to MQTT broker: {e}")
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
    
    # Wait for connection and discovery
    time.sleep(3)
    
    print(f"Starting network monitoring (Update Interval: {UPDATE_INTERVAL} seconds)...")
    print(f"Monitoring interfaces: {', '.join(NETWORK_INTERFACES)}")
    if WIFI_MONITOR_AP:
        print(f"Monitoring WiFi AP: {WIFI_MONITOR_AP}")
    
    # Show initial interface status
    print("\nInitial interface status:")
    for interface in NETWORK_INTERFACES:
        status = get_interface_status(interface)
        icon = status["attributes"].get("icon", "mdi:lan")
        state = status["status"]
        interface_type = "VPN" if status["attributes"].get("is_vpn_tunnel", False) else "WiFi" if status["attributes"].get("is_wireless", False) else "Ethernet"
        print(f"  {interface}: {state} ({interface_type}), Icon: {icon}")
    
    print("\nWiFi Connection Features:")
    print("  - Scan button to discover networks (ALWAYS fresh scan)")
    print("  - SSID dropdown to select network")
    print("  - Password input field")
    print("  - Connect button to join network")
    
    while True:
        try:
            publish_network_data(client)
        except Exception as e:
            print(f"Error publishing network data: {e}")
        
        time.sleep(UPDATE_INTERVAL)

if __name__ == "__main__":
    main()

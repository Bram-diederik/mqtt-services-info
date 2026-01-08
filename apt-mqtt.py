import os
import json
import ssl
import time
import subprocess
import pty
import re
import threading
import tempfile
from collections import deque
from dotenv import load_dotenv
import paho.mqtt.client as mqtt

load_dotenv()

# ================= CONFIG =================
MQTT_BROKER = os.getenv("MQTT_BROKER")
MQTT_PORT = int(os.getenv("MQTT_PORT", 8883))
MQTT_USER = os.getenv("MQTT_USER")
MQTT_PW = os.getenv("MQTT_PASSWORD")
MQTT_SSL = os.getenv("MQTT_SSL") == "1"
SERVER_NAME = os.getenv("SERVER_NAME", "hushhush")

# Configurable log settings from .env
LOG_UPDATE_INTERVAL = float(os.getenv("LOG_UPDATE_INTERVAL", "3"))  # Seconds between log updates
TAIL_LINES = int(os.getenv("TAIL_LINES", "30"))  # Number of lines to show
LOG_MEMORY_LIMIT = int(os.getenv("LOG_MEMORY_LIMIT", "1000"))  # Max lines to keep in memory

TOPIC_BASE = f"apt_update/{SERVER_NAME}"
TOPIC_STATUS = f"{TOPIC_BASE}/apt_status"
TOPIC_COUNT = f"{TOPIC_BASE}/available_upgrades"
TOPIC_CMD = f"{TOPIC_BASE}/command"
TOPIC_CONFIG_STATE = f"{TOPIC_BASE}/config_state"
TOPIC_CONFLICT = f"{TOPIC_BASE}/conflict"
TOPIC_KEYBOARD = f"{TOPIC_BASE}/keyboard"

# Explicit separation of data topics
TOPIC_ATTR_LOG = f"{TOPIC_BASE}/attributes/log"            
TOPIC_ATTR_CHANGELOG = f"{TOPIC_BASE}/attributes/changelog" 
TOPIC_LOG_PROGRESS = f"{TOPIC_BASE}/log_progress"
# New: Special diff attribute topic
TOPIC_DIFF_ATTR = f"{TOPIC_BASE}/diff_attribute"
# New: Changelog state topic (for displaying changelog as state)
TOPIC_CHANGELOG_STATE = f"{TOPIC_BASE}/changelog_state"

# ================= CHANGELOG FILE MANAGEMENT =================
CHANGELOG_TEMP_FILE = None
DIFF_TEMP_FILE = None  # New: For storing diff output
LAST_UPGRADE_CHANGELOG_FILE = None  # NEW: Separate file for last upgrade changelog

def get_changelog_temp_file():
    """Get or create temp file for changelog storage"""
    global CHANGELOG_TEMP_FILE
    if CHANGELOG_TEMP_FILE is None:
        CHANGELOG_TEMP_FILE = tempfile.NamedTemporaryFile(
            mode='w+',
            prefix=f'{SERVER_NAME}_apt_changelog_',
            suffix='.txt',
            delete=False
        )
        CHANGELOG_TEMP_FILE.close()
    return CHANGELOG_TEMP_FILE.name

def get_diff_temp_file():
    """Get or create temp file for diff storage"""
    global DIFF_TEMP_FILE
    if DIFF_TEMP_FILE is None:
        DIFF_TEMP_FILE = tempfile.NamedTemporaryFile(
            mode='w+',
            prefix=f'{SERVER_NAME}_apt_diff_',
            suffix='.txt',
            delete=False
        )
        DIFF_TEMP_FILE.close()
    return DIFF_TEMP_FILE.name

def get_last_upgrade_changelog_file():
    """Get or create file for LAST upgrade changelog (persists through upgrade)"""
    global LAST_UPGRADE_CHANGELOG_FILE
    if LAST_UPGRADE_CHANGELOG_FILE is None:
        LAST_UPGRADE_CHANGELOG_FILE = tempfile.NamedTemporaryFile(
            mode='w+',
            prefix=f'{SERVER_NAME}_last_upgrade_changelog_',
            suffix='.txt',
            delete=False
        )
        LAST_UPGRADE_CHANGELOG_FILE.close()
    return LAST_UPGRADE_CHANGELOG_FILE.name

def write_changelog_to_temp(content):
    """Write current changelog content to temp file"""
    try:
        temp_file = get_changelog_temp_file()
        with open(temp_file, 'w', encoding='utf-8') as f:
            f.write(content)
        return True
    except Exception as e:
        print(f"Error writing changelog to temp file: {e}")
        return False

def write_last_upgrade_changelog(content):
    """Write LAST upgrade changelog to separate file (persists)"""
    try:
        temp_file = get_last_upgrade_changelog_file()
        with open(temp_file, 'w', encoding='utf-8') as f:
            f.write(content)
        return True
    except Exception as e:
        print(f"Error writing last upgrade changelog to file: {e}")
        return False

def write_diff_to_temp(content):
    """Write diff content to temp file"""
    try:
        temp_file = get_diff_temp_file()
        with open(temp_file, 'w', encoding='utf-8') as f:
            f.write(content)
        # Publish diff attribute
        client.publish(TOPIC_DIFF_ATTR, json.dumps({"diff_output": content}), retain=True)
        return True
    except Exception as e:
        print(f"Error writing diff to temp file: {e}")
        return False

def read_changelog_from_temp():
    """Read current changelog content from temp file"""
    try:
        temp_file = get_changelog_temp_file()
        if os.path.exists(temp_file) and os.path.getsize(temp_file) > 0:
            with open(temp_file, 'r', encoding='utf-8') as f:
                return f.read()
    except Exception as e:
        print(f"Error reading changelog from temp file: {e}")
    return ""

def read_last_upgrade_changelog():
    """Read LAST upgrade changelog from separate file"""
    try:
        temp_file = get_last_upgrade_changelog_file()
        if os.path.exists(temp_file) and os.path.getsize(temp_file) > 0:
            with open(temp_file, 'r', encoding='utf-8') as f:
                return f.read()
    except Exception as e:
        print(f"Error reading last upgrade changelog from file: {e}")
    return ""

def read_diff_from_temp():
    """Read diff content from temp file"""
    try:
        temp_file = get_diff_temp_file()
        if os.path.exists(temp_file) and os.path.getsize(temp_file) > 0:
            with open(temp_file, 'r', encoding='utf-8') as f:
                return f.read()
    except Exception as e:
        print(f"Error reading diff from temp file: {e}")
    return ""

def clear_changelog_temp():
    """Clear current changelog temp file"""
    try:
        temp_file = get_changelog_temp_file()
        if os.path.exists(temp_file):
            os.unlink(temp_file)
            # Reset global variable so new file is created next time
            global CHANGELOG_TEMP_FILE
            CHANGELOG_TEMP_FILE = None
    except Exception as e:
        print(f"Error clearing changelog temp file: {e}")

def clear_last_upgrade_changelog():
    """Clear last upgrade changelog file"""
    try:
        temp_file = get_last_upgrade_changelog_file()
        if os.path.exists(temp_file):
            os.unlink(temp_file)
            # Reset global variable so new file is created next time
            global LAST_UPGRADE_CHANGELOG_FILE
            LAST_UPGRADE_CHANGELOG_FILE = None
    except Exception as e:
        print(f"Error clearing last upgrade changelog file: {e}")

def clear_diff_temp():
    """Clear diff temp file"""
    try:
        temp_file = get_diff_temp_file()
        if os.path.exists(temp_file):
            os.unlink(temp_file)
            # Reset global variable so new file is created next time
            global DIFF_TEMP_FILE
            DIFF_TEMP_FILE = None
            # Clear diff attribute
            client.publish(TOPIC_DIFF_ATTR, json.dumps({"diff_output": ""}), retain=True)
    except Exception as e:
        print(f"Error clearing diff temp file: {e}")

# ================= MEMORY EFFICIENT STATE =================
class ThreadSafeState:
    def __init__(self, log_limit=1000, tail_lines=30):
        self.lock = threading.RLock()
        self.upgrade_requested = False
        self.upgrade_with_yes = False
        self.response_input = None
        self.last_check_time = 0
        self.full_log_buffer = deque(maxlen=log_limit)  # Fixed size deque
        self.upgrade_in_progress = False
        self.post_upgrade_viewing = False
        self.apt_process_active = False
        self.direct_input = None
        self.last_packages_hash = ""
        self.waiting_for_prompt = False
        self.current_upgrade_packages = []
        self.last_log_update_time = 0
        self.log_update_count = 0
        self.tail_lines = tail_lines
        self.showing_diff = False  # Track if we're showing diff output
        self.last_upgrade_successful = False  # Track if last upgrade was successful
        self.last_changelog_summary = ""  # Store a brief summary of last changelog
        self.capturing_diff = False  # NEW: Track if we're actively capturing diff output
        self.diff_buffer = []  # NEW: Buffer for diff output
        
    def add_log_line(self, line):
        """Add line to log buffer - automatically manages size"""
        with self.lock:
            self.full_log_buffer.append(line)
    
    def get_log_tail(self):
        """Get last N lines for display"""
        with self.lock:
            if len(self.full_log_buffer) > self.tail_lines:
                return list(self.full_log_buffer)[-self.tail_lines:]
            return list(self.full_log_buffer)
    
    def clear_logs(self):
        """Clear all logs efficiently"""
        with self.lock:
            self.full_log_buffer.clear()

state = ThreadSafeState(log_limit=LOG_MEMORY_LIMIT, tail_lines=TAIL_LINES)

# ================= MQTT HANDLER =================
def on_message(client, userdata, msg):
    payload = msg.payload.decode().strip()
    
    if msg.topic == TOPIC_CMD:
        p_lower = payload.lower()
        with state.lock:
            if p_lower == "start" and not state.upgrade_in_progress and not state.post_upgrade_viewing:
                state.upgrade_requested = True
                state.upgrade_with_yes = False
                # Immediate status update when start is pressed
                client.publish(TOPIC_STATUS, "Starting...", retain=True)
                client.publish(TOPIC_LOG_PROGRESS, "start_requested", retain=True)
            elif p_lower == "start_yes" and not state.upgrade_in_progress and not state.post_upgrade_viewing:
                state.upgrade_requested = True
                state.upgrade_with_yes = True
                # Immediate status update when start_yes is pressed
                client.publish(TOPIC_STATUS, "Starting (-y)...", retain=True)
                client.publish(TOPIC_LOG_PROGRESS, "start_yes_requested", retain=True)
            elif p_lower == "clear":
                threading.Thread(target=reset_to_idle_state, daemon=True).start()
            elif p_lower in ["y", "n", "d"]:
                state.response_input = p_lower
                # Special handling for 'd' (diff)
                if p_lower == "d":
                    with state.lock:
                        state.showing_diff = True
                        state.capturing_diff = True  # Start capturing diff
                        state.diff_buffer = []  # Clear previous diff buffer
    elif msg.topic == TOPIC_KEYBOARD and payload:
        with state.lock:
            state.direct_input = payload + "\n"
        client.publish(TOPIC_KEYBOARD, "", retain=True)

client = mqtt.Client()
client.on_message = on_message
if MQTT_USER: client.username_pw_set(MQTT_USER, MQTT_PW)
if MQTT_SSL: client.tls_set(cert_reqs=ssl.CERT_REQUIRED)
client.connect(MQTT_BROKER, MQTT_PORT)
client.subscribe(TOPIC_CMD)
client.subscribe(TOPIC_KEYBOARD)
client.loop_start()

# ================= HELPERS =================
def reset_to_idle_state():
    """Wipes logs and returns sensors to 'Off' state."""
    with state.lock:
        state.clear_logs()
        state.upgrade_requested = False
        state.upgrade_in_progress = False
        state.post_upgrade_viewing = False
        state.waiting_for_prompt = False
        state.last_packages_hash = ""
        state.last_check_time = 0
        state.last_log_update_time = 0
        state.log_update_count = 0
        state.current_upgrade_packages = []
        state.showing_diff = False
        state.capturing_diff = False
        state.last_upgrade_successful = False
        state.last_changelog_summary = ""
        state.diff_buffer = []
    
    # Clear all temp files
    clear_changelog_temp()
    clear_last_upgrade_changelog()
    clear_diff_temp()
    
    client.publish(TOPIC_STATUS, "Off", retain=True)
    client.publish(TOPIC_CONFIG_STATE, "OFF", retain=True)
    client.publish(TOPIC_CONFLICT, "false", retain=True)
    client.publish(TOPIC_LOG_PROGRESS, "idle", retain=True)
    client.publish(TOPIC_ATTR_LOG, json.dumps({"full_log": ""}), retain=True)
    client.publish(TOPIC_ATTR_CHANGELOG, json.dumps({"changelog": ""}), retain=True)
    client.publish(TOPIC_DIFF_ATTR, json.dumps({"diff_output": ""}), retain=True)
    client.publish(TOPIC_CHANGELOG_STATE, "No changelog available", retain=True)

def update_log_display(force_update=False):
    """Update the log display - shows last TAIL_LINES rows"""
    current_time = time.time()
    
    # Only update every LOG_UPDATE_INTERVAL seconds unless forced
    if not force_update and (current_time - state.last_log_update_time < LOG_UPDATE_INTERVAL):
        return False
    
    # Get display lines
    display_lines = state.get_log_tail()
    display_text = "\n".join(display_lines)
    
    # Publish to MQTT
    client.publish(TOPIC_ATTR_LOG, json.dumps({"full_log": display_text}), retain=True)
    
    # Update progress indicator
    with state.lock:
        state.log_update_count += 1
        state.last_log_update_time = current_time
        log_size = len(state.full_log_buffer)
    
    progress_msg = f"updated:{state.log_update_count},lines:{log_size}"
    client.publish(TOPIC_LOG_PROGRESS, progress_msg, retain=True)
    
    return True

def add_to_log(new_line):
    """Add a line to the full log"""
    state.add_log_line(new_line)
    
    # Force update for progress indicators
    if "%" in new_line:
        update_log_display(force_update=True)
    
    # Update display at regular intervals for non-progress lines
    update_log_display()

def publish_changelog():
    """Publish current changelog content from temp file"""
    # When in post-upgrade viewing mode, show the LAST upgrade changelog
    with state.lock:
        if state.post_upgrade_viewing and state.last_upgrade_successful:
            changelog = read_last_upgrade_changelog()
        else:
            changelog = read_changelog_from_temp()
    
    # Publish to attribute topic
    client.publish(TOPIC_ATTR_CHANGELOG, json.dumps({"changelog": changelog}), retain=True)
    
    # Also publish a summary to the state topic for immediate visibility
    if changelog:
        # Create a brief summary (first 3 packages or so)
        lines = changelog.split('\n')
        package_count = 0
        summary_parts = []
        
        for line in lines:
            if line.startswith("=== ") and line.endswith(" ==="):
                package_count += 1
                if package_count <= 3:
                    package_name = line[4:-4]
                    summary_parts.append(package_name)
        
        if package_count > 0:
            summary = f"{package_count} packages upgraded"
            if summary_parts:
                summary += f": {', '.join(summary_parts)}"
                if package_count > 3:
                    summary += f" and {package_count - 3} more"
        else:
            summary = "Changelog available"
        
        with state.lock:
            state.last_changelog_summary = summary
        
        client.publish(TOPIC_CHANGELOG_STATE, summary, retain=True)
    else:
        client.publish(TOPIC_CHANGELOG_STATE, "No changelog available", retain=True)
    
    return changelog

def update_percentage_line(new_line):
    """Update percentage lines in buffer"""
    if not re.search(r'(\d+)%', new_line):
        return False
    
    # Find and update the most recent percentage line
    with state.lock:
        for i in range(len(state.full_log_buffer) - 1, -1, -1):
            if re.search(r'\d+%', state.full_log_buffer[i]):
                # Convert deque to list for update, then back to deque
                log_list = list(state.full_log_buffer)
                log_list[i] = new_line
                state.full_log_buffer = deque(log_list, maxlen=LOG_MEMORY_LIMIT)
                return True
    return False

def extract_diff_from_output(chunk):
    """Extract only the actual diff output from the chunk, filtering out prompts"""
    lines = chunk.splitlines()
    diff_lines = []
    in_diff = False
    diff_started = False
    
    for line in lines:
        # Look for diff start markers
        if re.match(r'^---\s+/', line) or re.match(r'^\+\+\+\s+/', line) or line.startswith('@@'):
            in_diff = True
            diff_started = True
        
        # If we're in diff mode, collect lines
        if in_diff:
            diff_lines.append(line)
        
        # Check if we should stop capturing (when we see a Configuration file line after diff)
        if diff_started and line.strip().startswith('Configuration file') and in_diff:
            # We've reached the next prompt, stop capturing diff
            # Remove the last line (the prompt line) from diff
            if diff_lines and diff_lines[-1] == line:
                diff_lines.pop()
            in_diff = False
    
    # If we have diff lines, return them
    if diff_lines:
        return '\n'.join(diff_lines)
    
    return None

def process_diff_output(chunk):
    """Process diff output and store it separately"""
    # Try to extract clean diff from the chunk
    clean_diff = extract_diff_from_output(chunk)
    
    if clean_diff:
        # Store the clean diff
        write_diff_to_temp(clean_diff)
        
        # Add to log as well (the actual diff)
        for line in clean_diff.splitlines():
            if line.strip():
                add_to_log(line)
        
        # Clear the diff buffer and stop capturing
        with state.lock:
            state.diff_buffer = []
            state.capturing_diff = False
    else:
        # If we couldn't extract clean diff, check if we should be capturing
        with state.lock:
            if state.capturing_diff:
                # Buffer the output for later processing
                state.diff_buffer.append(chunk)
                
                # Check if the buffer contains enough to process
                if len(state.diff_buffer) > 3:  # Buffer a few chunks
                    # Try to extract diff from buffered content
                    buffered_content = ''.join(state.diff_buffer)
                    clean_diff = extract_diff_from_output(buffered_content)
                    
                    if clean_diff:
                        # Store the clean diff
                        write_diff_to_temp(clean_diff)
                        
                        # Add to log as well
                        for line in clean_diff.splitlines():
                            if line.strip():
                                add_to_log(line)
                        
                        # Clear buffer and stop capturing
                        state.diff_buffer = []
                        state.capturing_diff = False

# ================= PACKAGE COUNT FUNCTIONS (FIXED FOR PHASING) =================
def get_installable_packages_count():
    """
    Get the number of packages that will actually be installed,
    excluding phased updates (Ubuntu/Debian phasing system).
    Uses apt-get upgrade --dry-run to get accurate count.
    """
    try:
        # Run apt-get upgrade with --dry-run to see what would actually be installed
        result = subprocess.run(
            ["sudo", "apt-get", "upgrade", "--dry-run"],
            capture_output=True,
            text=True,
            check=True
        )
        
        # Parse the output to count packages that will be installed
        output = result.stdout
        
        # Look for lines like:
        # "The following packages will be upgraded:"
        # Or in newer apt versions: "The following packages will be upgraded:\n  package1 package2"
        
        will_be_upgraded = False
        package_count = 0
        
        for line in output.split('\n'):
            line = line.strip()
            
            if "The following packages will be upgraded:" in line:
                will_be_upgraded = True
                continue
            
            if will_be_upgraded:
                # If line is empty, we're done with the package list
                if not line:
                    will_be_upgraded = False
                    continue
                
                # Count packages in this line (they're space-separated)
                # Remove any leading/trailing spaces and count words
                packages = line.split()
                package_count += len(packages)
        
        # If we didn't find the pattern, try alternative parsing
        if package_count == 0:
            # Try to find "packages upgraded" pattern
            for line in output.split('\n'):
                if "upgraded," in line and "newly installed" in line:
                    # Example: "3 upgraded, 2 newly installed, 0 to remove and 0 not upgraded."
                    parts = line.split(',')
                    if parts:
                        upgraded_part = parts[0].strip()
                        # Extract number
                        match = re.search(r'(\d+)\s+upgraded', upgraded_part)
                        if match:
                            package_count = int(match.group(1))
        
        return package_count
    
    except subprocess.CalledProcessError as e:
        print(f"Error running apt-get upgrade --dry-run: {e}")
        # Fall back to the old method
        return len(get_upgradable_packages())
    except Exception as e:
        print(f"Error counting installable packages: {e}")
        return 0

def get_upgradable_packages():
    """Get current list of upgradable packages"""
    try:
        result = subprocess.run(
            ["apt", "list", "--upgradable"],
            capture_output=True,
            text=True,
            check=True
        )
        packages = []
        # Process line by line to avoid large string operations
        for i, line in enumerate(result.stdout.strip().split('\n')):
            if i == 0:  # Skip header
                continue
            if '/' in line:
                package = line.split('/')[0].strip()
                if package and package not in packages:
                    packages.append(package)
        
        return packages
    except Exception as e:
        print(f"Error getting upgradable packages: {e}")
        return []

def calculate_packages_hash(packages):
    """Create a hash of packages list to detect changes"""
    if not packages:
        return ""
    return str(hash(frozenset(packages)))  # frozenset is hashable

def fetch_changelog_if_packages_changed():
    """Check if packages changed and fetch changelog only if they did"""
    with state.lock:
        if state.upgrade_in_progress or state.post_upgrade_viewing:
            return False
    
    # Get the actual installable count (excluding phased updates)
    installable_count = get_installable_packages_count()
    
    # Still get full package list for changelog
    packages = get_upgradable_packages()
    current_hash = calculate_packages_hash(packages)
    
    with state.lock:
        if current_hash != state.last_packages_hash:
            # Packages changed, fetch new changelog
            new_changelog = fetch_changelog_for_packages(packages)
            # Write to CURRENT changelog temp file (not last upgrade file)
            write_changelog_to_temp(new_changelog)
            state.current_upgrade_packages = packages.copy()
            state.last_packages_hash = current_hash
            publish_changelog()
            
            # Update count with ACTUAL installable count (not total upgradable)
            client.publish(TOPIC_COUNT, str(installable_count), retain=True)
            return True
        else:
            # Packages unchanged, just update count with ACTUAL installable count
            client.publish(TOPIC_COUNT, str(installable_count), retain=True)
            return False

# ================= CHANGELOG FUNCTIONS =================
def fetch_changelog_for_packages(packages, limit=25):
    """Fetch changelogs for given packages with memory optimization"""
    if not packages:
        return ""
    
    local_cl = []
    for pkg in packages[:limit]:
        try:
            result = subprocess.run(
                ["apt", "changelog", pkg],
                capture_output=True,
                text=True,
                timeout=5
            )
            if result.returncode == 0 and result.stdout:
                # Use generator for memory efficiency
                lines = (line for line in result.stdout.split('\n')[:15])
                local_cl.append(f"=== {pkg} ===")
                local_cl.extend(lines)
                local_cl.append("")
        except Exception as e:
            local_cl.append(f"=== {pkg} ===")
            local_cl.append(f"Could not fetch changelog: {str(e)[:50]}")
            local_cl.append("")
    
    return "\n".join(local_cl)

# ================= APT INTERACTION =================
def wait_for_response():
    """Wait for user response to Y/N/Diff prompt"""
    with state.lock:
        state.waiting_for_prompt = True
    
    # Force immediate log update to show prompt
    update_log_display(force_update=True)
    client.publish(TOPIC_CONFIG_STATE, "yes-no", retain=True)
    client.publish(TOPIC_LOG_PROGRESS, "awaiting_input", retain=True)
    
    while True:
        with state.lock:
            if state.direct_input:
                ans = state.direct_input
                state.direct_input = None
                state.waiting_for_prompt = False
                client.publish(TOPIC_CONFIG_STATE, "OFF", retain=True)
                client.publish(TOPIC_LOG_PROGRESS, "input_received", retain=True)
                update_log_display(force_update=True)
                return ans.strip()
            
            if state.response_input:
                ans = state.response_input
                state.response_input = None
                state.waiting_for_prompt = False
                
                # Special handling for 'd' (diff) - capture diff output
                if ans == "d":
                    # We'll handle diff differently - the PTY will show it
                    # Set flags to capture the diff output
                    with state.lock:
                        state.showing_diff = True
                        state.capturing_diff = True
                        state.diff_buffer = []  # Clear previous diff buffer
                
                client.publish(TOPIC_CONFIG_STATE, "OFF", retain=True)
                client.publish(TOPIC_LOG_PROGRESS, "input_received", retain=True)
                update_log_display(force_update=True)
                return ans
        
        time.sleep(0.05)  # Faster polling for prompt responses

def master_read(fd):
    """Read from PTY and process output"""
    try:
        data = os.read(fd, 4096)  # Reduced buffer size
    except OSError: 
        return b""
    
    if not data: 
        return b""
    
    # Echo to console
    os.write(1, data)
    
    chunk = data.decode(errors="ignore")
    
    # Check if we're capturing diff output
    with state.lock:
        capturing_diff = state.capturing_diff
    
    if capturing_diff:
        # Process diff output - this will extract and store clean diff
        process_diff_output(chunk)
        return data
    
    # Normal processing for non-diff output
    for raw_line in chunk.splitlines():
        if raw_line.strip():
            line = raw_line.rstrip('\n\r')
            # Try to update percentage line first
            if not update_percentage_line(line):
                # If not a percentage line, add as new line
                add_to_log(line)
    
    # Check for prompts
    chunk_lower = chunk.lower()
    if any(x in chunk_lower for x in ["(y/n)", "[y/n]", "(y/i/n/o/d/z)", "[yes/no]", "continue?"]):
        
        if "(y/i/n/o/d/z)" in chunk_lower: 
            client.publish(TOPIC_CONFLICT, "true", retain=True)
        
        ans = wait_for_response()
        
        client.publish(TOPIC_CONFLICT, "false", retain=True)
        os.write(fd, (ans + "\n").encode())
    
    return data

# ================= HOME ASSISTANT DISCOVERY =================
def setup_discovery():
    """Set up Home Assistant auto-discovery"""
    device = {
        "identifiers": [f"apt_update_{SERVER_NAME}"],
        "name": f"Apt Update ({SERVER_NAME})",
        "manufacturer": "Debian/Kali"
    }

    configs = [
        ("sensor", "available_upgrades", {
            "name": f"Available Upgrades ({SERVER_NAME})", 
            "state_topic": TOPIC_COUNT, 
            "json_attributes_topic": TOPIC_ATTR_CHANGELOG, 
            "unique_id": f"apt_{SERVER_NAME}_upgrades"
        }),
        ("sensor", "apt_status", {
            "name": f"Apt Status ({SERVER_NAME})", 
            "state_topic": TOPIC_STATUS, 
            "json_attributes_topic": TOPIC_ATTR_LOG, 
            "unique_id": f"apt_{SERVER_NAME}_status"
        }),
        ("binary_sensor", "config_ask", {
            "name": f"Config Ask ({SERVER_NAME})", 
            "state_topic": TOPIC_CONFIG_STATE, 
            "payload_on": "yes-no", 
            "payload_off": "OFF", 
            "unique_id": f"apt_{SERVER_NAME}_ask"
        }),
        ("binary_sensor", "conflict", {
            "name": f"Config Conflict ({SERVER_NAME})", 
            "state_topic": TOPIC_CONFLICT, 
            "payload_on": "true", 
            "payload_off": "false", 
            "unique_id": f"apt_{SERVER_NAME}_conflict"
        }),
        ("sensor", "changelog_info", {
            "name": f"Changelog Info ({SERVER_NAME})", 
            "state_topic": TOPIC_ATTR_CHANGELOG, 
            "json_attributes_topic": TOPIC_ATTR_CHANGELOG, 
            "unique_id": f"apt_{SERVER_NAME}_changelog_info"
        }),
        # NEW: Changelog State sensor (shows summary as state)
        ("sensor", "changelog_state", {
            "name": f"Changelog State ({SERVER_NAME})", 
            "state_topic": TOPIC_CHANGELOG_STATE, 
            "json_attributes_topic": TOPIC_ATTR_CHANGELOG, 
            "unique_id": f"apt_{SERVER_NAME}_changelog_state"
        }),
        ("sensor", "log_progress", {
            "name": f"Log Progress ({SERVER_NAME})", 
            "state_topic": TOPIC_LOG_PROGRESS, 
            "unique_id": f"apt_{SERVER_NAME}_log_progress"
        }),
        # New: Diff attribute sensor
        ("sensor", "diff_output", {
            "name": f"Diff Output ({SERVER_NAME})", 
            "state_topic": TOPIC_DIFF_ATTR, 
            "json_attributes_topic": TOPIC_DIFF_ATTR, 
            "unique_id": f"apt_{SERVER_NAME}_diff_output"
        }),
        ("button", "apt_upgrade", {
            "name": f"Apt Upgrade ({SERVER_NAME})", 
            "command_topic": TOPIC_CMD, 
            "payload_press": "start", 
            "unique_id": f"apt_{SERVER_NAME}_upgrade"
        }),
        ("button", "apt_upgrade_y", {
            "name": f"Apt Upgrade -y ({SERVER_NAME})", 
            "command_topic": TOPIC_CMD, 
            "payload_press": "start_yes", 
            "unique_id": f"apt_{SERVER_NAME}_upgrade_y"
        }),
        ("button", "confirm_yes", {
            "name": f"Confirm YES ({SERVER_NAME})", 
            "command_topic": TOPIC_CMD, 
            "payload_press": "y", 
            "unique_id": f"apt_{SERVER_NAME}_yes"
        }),
        ("button", "confirm_no", {
            "name": f"Confirm NO ({SERVER_NAME})", 
            "command_topic": TOPIC_CMD, 
            "payload_press": "n", 
            "unique_id": f"apt_{SERVER_NAME}_no"
        }),
        ("button", "show_diff", {
            "name": f"Show Diff ({SERVER_NAME})", 
            "command_topic": TOPIC_CMD, 
            "payload_press": "d", 
            "unique_id": f"apt_{SERVER_NAME}_diff"
        }),
        ("button", "clear", {
            "name": f"Clear ({SERVER_NAME})", 
            "command_topic": TOPIC_CMD, 
            "payload_press": "clear", 
            "unique_id": f"apt_{SERVER_NAME}_clear"
        })
    ]

    for comp, sub, cfg in configs:
        cfg["device"] = device
        cfg["object_id"] = f"apt_update_{SERVER_NAME}_{sub}"
        topic = f"homeassistant/{comp}/{SERVER_NAME}_{sub}/config"
        print(f"Registering {sub} on topic: {topic}")
        client.publish(topic, json.dumps(cfg), retain=True)
    
    keyboard_config = {
        "device": device,
        "object_id": f"apt_update_{SERVER_NAME}_keyboard",
        "name": f"Keyboard Input ({SERVER_NAME})",
        "command_topic": TOPIC_KEYBOARD,
        "state_topic": TOPIC_KEYBOARD,
        "unique_id": f"apt_{SERVER_NAME}_keyboard",
        "mode": "text"
    }
    client.publish(f"homeassistant/text/{SERVER_NAME}_keyboard/config", json.dumps(keyboard_config), retain=True)

    # Initialize states
    client.publish(TOPIC_STATUS, "Off", retain=True)
    client.publish(TOPIC_COUNT, "0", retain=True)
    client.publish(TOPIC_CONFIG_STATE, "OFF", retain=True)
    client.publish(TOPIC_CONFLICT, "false", retain=True)
    client.publish(TOPIC_KEYBOARD, "", retain=True)
    client.publish(TOPIC_LOG_PROGRESS, "idle", retain=True)
    client.publish(TOPIC_ATTR_LOG, json.dumps({"full_log": ""}), retain=True)
    client.publish(TOPIC_ATTR_CHANGELOG, json.dumps({"changelog": ""}), retain=True)
    client.publish(TOPIC_DIFF_ATTR, json.dumps({"diff_output": ""}), retain=True)
    client.publish(TOPIC_CHANGELOG_STATE, "No changelog available", retain=True)

setup_discovery()

# ================= INITIAL BOOT CHANGELOG FETCH =================
def initial_boot_changelog_fetch():
    """Fetch changelog on script startup"""
    print(f"Initial boot: LOG_UPDATE_INTERVAL={LOG_UPDATE_INTERVAL}s, TAIL_LINES={TAIL_LINES}, LOG_MEMORY_LIMIT={LOG_MEMORY_LIMIT}")
    
    # Get actual installable count (excluding phased updates)
    installable_count = get_installable_packages_count()
    client.publish(TOPIC_COUNT, str(installable_count), retain=True)
    
    # Still fetch changelog for all upgradable packages
    packages = get_upgradable_packages()
    
    if packages:
        new_changelog = fetch_changelog_for_packages(packages)
        # Write to CURRENT changelog temp file
        write_changelog_to_temp(new_changelog)
        with state.lock:
            state.current_upgrade_packages = packages.copy()
            state.last_packages_hash = calculate_packages_hash(packages)
        publish_changelog()
        print(f"Initial changelog fetched and published. Installable packages: {installable_count}")
    else:
        print("No packages available for upgrade on boot")

initial_boot_changelog_fetch()

# ================= MAIN LOOP =================
try:
    while True:
        current_time = time.time()
        
        # 1. Background Check (Only when completely idle)
        with state.lock:
            idle = not state.upgrade_requested and not state.upgrade_in_progress and not state.post_upgrade_viewing
        
        if idle and (current_time - state.last_check_time > 60):
            fetch_changelog_if_packages_changed()
            with state.lock:
                state.last_check_time = current_time

        # 2. Upgrade Execution
        with state.lock:
            should_upgrade = state.upgrade_requested and not state.upgrade_in_progress
        
        if should_upgrade:
            with state.lock:
                state.upgrade_in_progress = True
                state.clear_logs()
                state.add_log_line("--- STARTING UPGRADE ---")
                state.waiting_for_prompt = False
                state.last_log_update_time = 0
                state.log_update_count = 0
                state.showing_diff = False
                state.capturing_diff = False
                state.last_upgrade_successful = False  # Reset success flag
                state.last_changelog_summary = ""
                state.diff_buffer = []
            
            # Immediate status update - faster response to start button
            update_log_display(force_update=True)
            client.publish(TOPIC_STATUS, "Upgrading...", retain=True)
            client.publish(TOPIC_LOG_PROGRESS, "starting", retain=True)
            
            # Store packages BEFORE upgrade for changelog
            pre_upgrade_packages = get_upgradable_packages()
            
            # Environment setup
            env_config = {
                "DEBIAN_FRONTEND": "readline",
                "LC_ALL": "C",
                "APT_LISTCHANGES_FRONTEND": "none",
                "PATH": os.environ.get("PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin")
            }
            original_env = os.environ.copy()
            os.environ.update(env_config)
            
            try:
                # Update first
                subprocess.run(["sudo", "-E", "apt-get", "update"], check=True)
                
                # Upgrade in PTY
                cmd = ["/usr/bin/sudo", "-E", "apt-get", "dist-upgrade"]
                if state.upgrade_with_yes: 
                    cmd.append("-y")
                    add_to_log("Running with -y flag (automatic yes)")
                
                def run_pty_upgrade():
                    upgrade_success = False
                    try:
                        pty.spawn(cmd, master_read)
                        upgrade_success = True
                    except Exception as e:
                        add_to_log(f"\nError in PTY: {e}")
                    finally:
                        with state.lock:
                            state.upgrade_in_progress = False
                            state.upgrade_requested = False
                            state.post_upgrade_viewing = True
                            state.waiting_for_prompt = False
                            state.showing_diff = False
                            state.capturing_diff = False
                            state.last_upgrade_successful = upgrade_success
                    
                    # AFTER UPGRADE: Fetch and store LAST upgrade changelog to SEPARATE file
                    if upgrade_success and pre_upgrade_packages:
                        new_changelog = fetch_changelog_for_packages(pre_upgrade_packages)
                        # Write to LAST UPGRADE changelog file (separate from current changelog)
                        upgrade_changelog = f"=== LAST UPGRADED PACKAGES ({time.strftime('%Y-%m-%d %H:%M:%S')}) ===\n\n{new_changelog}"
                        write_last_upgrade_changelog(upgrade_changelog)
                    elif upgrade_success:
                        write_last_upgrade_changelog("No packages were upgraded in the last update.")
                    
                    # CRITICAL: Force publish the changelog IMMEDIATELY after upgrade
                    publish_changelog()
                    
                    add_to_log("--- UPGRADE COMPLETE ---")
                    update_log_display(force_update=True)
                    
                    # Different status based on success
                    if upgrade_success:
                        client.publish(TOPIC_STATUS, "Upgrade Complete", retain=True)
                        client.publish(TOPIC_LOG_PROGRESS, "complete", retain=True)
                    else:
                        client.publish(TOPIC_STATUS, "Upgrade Failed", retain=True)
                        client.publish(TOPIC_LOG_PROGRESS, "failed", retain=True)
                    
                    # Update package count with actual installable count
                    installable_count = get_installable_packages_count()
                    client.publish(TOPIC_COUNT, str(installable_count), retain=True)
                    with state.lock:
                        state.last_packages_hash = ""
                
                upgrade_thread = threading.Thread(target=run_pty_upgrade, daemon=True)
                upgrade_thread.start()
                
            except Exception as e:
                add_to_log(f"\nError during upgrade setup: {e}")
                with state.lock:
                    state.upgrade_in_progress = False
                    state.upgrade_requested = False
                    state.showing_diff = False
                    state.capturing_diff = False
                    state.last_upgrade_successful = False
                os.environ.clear()
                os.environ.update(original_env)
        
        # 3. Regular log updates
        update_log_display()
        
        time.sleep(0.05)  # Faster polling for better responsiveness
        
except KeyboardInterrupt:
    # Clean up temp files on exit
    clear_changelog_temp()
    clear_last_upgrade_changelog()
    clear_diff_temp()
    client.disconnect()

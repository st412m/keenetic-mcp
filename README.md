# Keenetic MCP Server

MCP (Model Context Protocol) server for Keenetic routers. Runs directly on the router via Entware. Allows Claude AI to monitor and manage your router.

Tested on: **Keenetic Giga KN-1010**, KeeneticOS 5.0.8, arch `mips`.

## Available Tools

### System Monitoring
- `get_system_info` — firmware version, uptime, CPU load, memory usage
- `get_internet_status` — internet connection status and external IP address
- `get_interfaces` — all network interfaces status and configuration
- `get_traffic` — top clients by traffic with total rx/tx summary
- `get_vpn_status` — status of all VPN interfaces (WireGuard, IPsec, L2TP, PPTP) with peer details

### WiFi
- `get_wifi` — WiFi radio status: channel, bandwidth, bitrate, temperature, connected stations count
- `get_wifi_stations` — currently connected WiFi devices with signal strength (RSSI), speed and traffic
- `get_site_survey` — scan nearby WiFi networks
- `get_channel_analysis` — analyze WiFi channel congestion and recommend the least busy channel for 2.4GHz and 5GHz

### Clients
- `get_clients` — all devices in the network with IP, MAC, signal, traffic
- `get_unregistered_clients` — active devices not yet registered in the router (unknown devices)
- `get_dhcp_leases` — devices with active DHCP leases including expiry time
- `register_client` — register a device by MAC, assign a name and optionally a static IP
- `update_client` — update name or static IP of a registered device
- `block_client` — block a device by MAC address (works for both registered and unregistered devices)
- `unblock_client` — unblock a previously blocked device by MAC address

### Diagnostics
- `get_log` — system log with optional line count limit and text filter
- `get_log_by_device` — system log filtered by device MAC address, IP address or name
- `run_ping` — ping a host directly from the router, returns latency and packet loss

### Mesh
- `get_mesh_nodes` — get Mesh Wi-Fi system nodes: controller and extenders with firmware, uptime and connection speed

### Mesh
- `get_mesh_nodes` — get Mesh Wi-Fi system nodes: controller and extenders with firmware, uptime and connection speed

### Security
- `get_web_access` — list of web applications exposed to the internet via Keenetic DDNS

### Management
- `reboot` — reboot the router

## Requirements

- Keenetic router with Entware support
- USB drive formatted as ext4
- Entware installed on the USB drive
- Python 3.x

Tested arch **mipsel** (KN-1010/1011, KN-1810, KN-1910, KN-2310, KN-3810). Should also work on **mips** arch (KN-2410, KN-2510, KN-2010, KN-2110, KN-3610).

## Installation

### Step 1 — Install Entware

Format a USB drive as ext4 and plug it into the router. In the router web interface go to Applications -> OPKG and make sure the drive is selected as the storage.

Download the installer for your router model and copy it to the `install` folder on the USB drive via SMB (\\192.168.1.1):

For KN-1010/1011, KN-1810, KN-1910, KN-2310, KN-3810:
https://bin.entware.net/mipselsf-k3.4/installer/mipsel-installer.tar.gz

For KN-2410, KN-2510, KN-2010, KN-2110, KN-3610:
https://bin.entware.net/mipssf-k3.4/installer/mips-installer.tar.gz

Entware installs automatically. Check the router system log for:
[5/5] Installation of the "Entware" package system is complete!

### Step 2 — SSH into the router

After Entware is installed, connect via SSH on port 222:

    ssh root@192.168.1.1 -p 222

Default password: keenetic. Change it immediately:

    passwd

### Step 3 — Install dependencies

    opkg update
    opkg install python3 git git-http nano curl

### Step 4 — Clone and configure

    cd /opt
    git clone https://github.com/st412m/keenetic-mcp.git
    cd keenetic-mcp
    cp .env.example .env
    nano .env

Fill in your credentials in .env:

    KEENETIC_HOST=http://192.168.1.1
    KEENETIC_USER=admin
    KEENETIC_PASS=your_router_password
    MCP_SECRET=some_random_secret_string
    MCP_PORT=9584

### Step 5 — Set up autostart

    cp init.d/S99keenetic-mcp /opt/etc/init.d/
    chmod +x /opt/etc/init.d/S99keenetic-mcp
    /opt/etc/init.d/S99keenetic-mcp start

Verify it is running:

    curl http://localhost:9584/YOUR_MCP_SECRET

### Step 6 — Configure external HTTPS access

In the Keenetic web interface go to Network Rules -> Domain name -> Web application access and click Add:

- Name: keenetic-mcp
- Internet access: Open access
- Device: This Keenetic device
- Protocol: HTTP
- TCP Port: 9584

Your MCP server will be available at:
https://keenetic-mcp.YOUR_DDNS.keenetic.link/YOUR_MCP_SECRET

### Step 7 — Connect to Claude

In Claude.ai go to Settings -> Integrations -> Add custom connector and paste the URL from Step 6.

## How Client Management Works

- `get_unregistered_clients` shows devices that connected to your network but were never named or registered
- `get_dhcp_leases` shows devices that received an IP from DHCP with time until lease expires
- `register_client` assigns a name and optional static IP to a device
- `block_client` denies network access to a device. If the device is not yet registered, it will be registered automatically as "Blocked Device" before blocking
- `unblock_client` restores access with permit rule
- Blocking does not disconnect the device from WiFi — it cuts off internet and LAN access at the firewall level

## Notes

- All 22 tools tested on NDMS 5.0.8
- get_wifi uses show interface (show wireless endpoint removed in NDMS 5.x)
- get_traffic aggregates rx/tx from active clients and shows top 10 by usage
- get_channel_analysis uses site survey data to recommend least congested channel
- get_log_by_device resolves device name/IP to MAC for more accurate log matching
- Mesh extender clients are visible in get_clients as part of the main network
- Port forwarding and firewall rules are not available via RCI in NDMS 5.x

## Security Notes

- The endpoint is protected by a secret token in the URL path
- HTTPS is handled by Keenetic built-in SSL certificate
- Never commit .env — it is in .gitignore
- Change the default SSH password after installation

## License

MIT

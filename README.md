# Keenetic MCP Server

MCP (Model Context Protocol) server for Keenetic routers. Runs directly on the router via Entware. Allows Claude AI to monitor and manage your router.

Tested on: **Keenetic Giga KN-1010**, KeeneticOS 5.0.8, arch `mips`.

## Features

- `get_system_info` — router version, uptime, CPU load, memory
- `get_clients` — all connected devices with IP, MAC, RSSI, traffic
- `get_interfaces` — network interfaces status and config
- `get_log` — system log with line count and text filter
- `get_wifi` — WiFi networks and stations
- `reboot` — reboot the router

## Requirements

- Keenetic router (mipsel arch: KN-1010/1011, KN-1810, KN-1910, KN-2310, KN-3810)
- USB drive formatted as ext4
- Entware installed on the USB drive
- Python 3.x

## Step 1 — Install Entware

Format a USB drive as ext4 and plug it into the router. In the router web interface go to Applications → OPKG and make sure the drive is selected as the storage.

Download the installer for your router model and copy it to the `install` folder on the USB drive via SMB (\\192.168.1.1):

For KN-1010/1011, KN-1810, KN-1910, KN-2310, KN-3810 use mipsel:
https://bin.entware.net/mipselsf-k3.4/installer/mipsel-installer.tar.gz

For KN-2410, KN-2510, KN-2010, KN-2110, KN-3610 use mips:
https://bin.entware.net/mipssf-k3.4/installer/mips-installer.tar.gz

Entware installs automatically. Check the router system log for:
[5/5] Installation of the "Entware" package system is complete!

## Step 2 — SSH into the router

After Entware is installed, connect via SSH on port 222:

    ssh root@192.168.1.1 -p 222

Default password: keenetic. Change it immediately:

    passwd

## Step 3 — Install dependencies

    opkg update
    opkg install python3 git git-http nano curl

## Step 4 — Clone and configure

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

## Step 5 — Set up autostart

    cp init.d/S99keenetic-mcp /opt/etc/init.d/
    chmod +x /opt/etc/init.d/S99keenetic-mcp
    /opt/etc/init.d/S99keenetic-mcp start

Verify it is running:

    curl http://localhost:9584/YOUR_MCP_SECRET

## Step 6 — Configure external HTTPS access

In the Keenetic web interface go to Network Rules → Domain name → Web application access and click Add:

- Name: keenetic-mcp
- Internet access: Open access
- Device: This Keenetic device
- Protocol: HTTP
- TCP Port: 9584

Your MCP server will be available at:
https://keenetic-mcp.YOUR_DDNS.keenetic.link/YOUR_MCP_SECRET

## Step 7 — Connect to Claude

In Claude.ai go to Settings → Integrations → Add custom connector and paste the URL from Step 6.

## Security Notes

- The endpoint is protected by the secret token in the URL path
- HTTPS is handled by Keenetic built-in SSL certificate
- Never commit .env — it is in .gitignore
- Change the default SSH password after installation

## License

MIT

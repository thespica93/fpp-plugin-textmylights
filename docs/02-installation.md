# Installation

> Install the FPP SMS Twilio Plugin on your Falcon Player device.

---

## Table of Contents

- [Method 1 — FPP Plugin Manager (Recommended)](#method-1--fpp-plugin-manager-recommended)
- [Method 2 — Manual Installation](#method-2--manual-installation)
- [Verifying the Installation](#verifying-the-installation)
- [Updating the Plugin](#updating-the-plugin)
- [Uninstalling](#uninstalling)

---

## Method 1 — FPP Plugin Manager (Recommended)

1. Open your FPP web interface (e.g., `http://192.168.1.100`)
2. Navigate to **Content Setup** → **Plugin Manager**

   ![FPP Plugin Manager menu](images/fpp-plugin-manager-menu.png)

3. In the plugin list or search, find **FPP SMS Twilio Plugin**
4. Click **Install**

   ![FPP Plugin Manager install button](images/fpp-plugin-manager-install.png)

5. FPP will clone the repository and run the install script automatically
6. The install script installs the required Python packages: `flask`, `twilio`, `requests`
7. When complete, the plugin will appear in your installed plugins list

> ✅ The plugin auto-starts whenever FPP's scheduler (FPPD) starts, so no manual start is needed after reboots.

---

## Method 2 — Manual Installation

If the Plugin Manager is unavailable or you want to install a specific version:

```bash
# SSH into your FPP device
ssh fpp@YOUR_FPP_IP

# Navigate to the plugins directory
cd /opt/fpp/plugins

# Clone the repository
git clone https://github.com/thespica93/fpp-plugin-textmylights.git

# Run the install script
cd fpp-plugin-textmylights
bash scripts/fpp_install.sh
```

The install script will:
- Install `flask`, `twilio`, and `requests` via pip
- Ensure `whitelist.txt` and `blacklist.txt` are present (restores from git if missing)

---

## Verifying the Installation

### Check Plugin is Running

After FPP starts (or immediately after install), the plugin service should be running on **port 5000**.

Open a browser and go to:
```
http://YOUR_FPP_IP:5000
```

You should see the plugin configuration page.

![Plugin configuration page](images/plugin-config-page.png)

### Check via SSH

```bash
# Check if the plugin process is running
ps aux | grep sms_plugin

# View the startup log
tail -50 /home/fpp/media/logs/sms_plugin.log
```

A healthy startup log looks like:
```
[INFO] Loading configuration...
[INFO] Whitelist loaded: 22170 names
[INFO] Blacklist loaded: 450 words
[INFO] Starting Flask server on port 5000
[INFO] Plugin polling started (interval: 10s)
```

---

## Updating the Plugin

To update to the latest version:

### Via FPP Plugin Manager
1. Go to **Content Setup** → **Plugin Manager**
2. Find the plugin and click **Update** (if available)

### Via SSH
```bash
cd /opt/fpp/plugins/fpp-plugin-textmylights
git pull
```

> ⚠️ **Your `whitelist.txt` and `blacklist.txt` are protected** by `.gitattributes` with `merge=union` — your local additions survive git pulls. Words/names you removed via the web UI are tracked in local files and automatically re-filtered after update.

---

## Uninstalling

### Via FPP Plugin Manager
1. Go to **Content Setup** → **Plugin Manager**
2. Click **Uninstall** next to the plugin

### Via SSH
```bash
cd /opt/fpp/plugins/fpp-plugin-textmylights
bash scripts/fpp_uninstall.sh
cd ..
rm -rf fpp-plugin-textmylights
```

---

## Port 5000 Access

The plugin web UI runs on **port 5000**. If you can't reach it:

```bash
# Check if port 5000 is open
sudo ufw allow 5000

# Or check if the process is listening
ss -tlnp | grep 5000
```

---

## Next Step

[→ Plugin Configuration](03-plugin-configuration.md)

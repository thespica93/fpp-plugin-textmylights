<?php
// Plugin Name: Text My Lights
// Plugin Description: Viewers text a name that appears on your display (Twilio or Google Voice)
// Plugin Version: 2.5
// Plugin Author: Nick
// Plugin URL: https://github.com/thespica93/fpp-plugin-textmylights

// This file is required by FPP to recognize and install the plugin

$pluginName = "fpp-plugin-textmylights";
$pluginVersion = "2.5";
$pluginDescription = "Text My Lights - Viewers text a name that appears on your display (Twilio or Google Voice)";
$pluginAuthor = "Nick";

// Plugin configuration
$pluginConfigFile = $settings['configDirectory'] . "/plugin." . $pluginName . ".json";

// Python service file location
$pluginPythonScript = $pluginDirectory . "/sms_plugin.py";

// Log files
$pluginLogFile = $settings['logDirectory'] . "/sms_plugin.log";
$messageLogFile = $settings['configDirectory'] . "/received_messages.json";

// Check if plugin is enabled
function isPluginEnabled() {
    global $pluginConfigFile;
    if (file_exists($pluginConfigFile)) {
        $config = json_decode(file_get_contents($pluginConfigFile), true);
        return isset($config['enabled']) && $config['enabled'] === true;
    }
    return false;
}

// Start the Python service
function startPlugin() {
    global $pluginPythonScript, $pluginLogFile;
    
    // Kill any existing instances
    exec("pkill -f sms_plugin.py");
    
    // Start the Python service in the background
    exec("nohup python3 " . escapeshellarg($pluginPythonScript) . " > " . escapeshellarg($pluginLogFile) . " 2>&1 &");
    
    return true;
}

// Stop the Python service
function stopPlugin() {
    exec("pkill -f sms_plugin.py");
    return true;
}

// Restart the plugin
function restartPlugin() {
    stopPlugin();
    sleep(1);
    return startPlugin();
}

// Check if the plugin service is running
function isPluginRunning() {
    $output = shell_exec("pgrep -f sms_plugin.py");
    return !empty(trim($output));
}

// Install dependencies
function installDependencies() {
    global $pluginDirectory;
    
    // Run the fpp_install.sh script in scripts directory
    $installScript = $pluginDirectory . "/scripts/fpp_install.sh";
    
    if (file_exists($installScript)) {
        // Run the installer using bash (doesn't need to be executable)
        exec("bash " . escapeshellarg($installScript) . " 2>&1", $output, $return);
        
        // Log the output
        error_log("Text My Lights Install: " . implode("\n", $output));
        
        return $return === 0;
    }
    
    return false;
}

// Create default configuration files if they don't exist
function createDefaultFiles() {
    global $settings, $pluginConfigFile;
    
    // Create blacklist.txt if it doesn't exist
    $blacklistFile = $settings['configDirectory'] . "/blacklist.txt";
    if (!file_exists($blacklistFile)) {
        $defaultBlacklist = "fuck\nshit\ndamn\nhell\nass\nbitch\ncrap\nbastard\npiss";
        file_put_contents($blacklistFile, $defaultBlacklist);
    }
    
    // Create whitelist.txt if it doesn't exist
    $whitelistFile = $settings['configDirectory'] . "/whitelist.txt";
    if (!file_exists($whitelistFile)) {
        touch($whitelistFile);
    }
    
    // Create blocked_phones.json if it doesn't exist
    $blocklistFile = $settings['configDirectory'] . "/blocked_phones.json";
    if (!file_exists($blocklistFile)) {
        file_put_contents($blocklistFile, "[]");
    }
    
    // Create default config if it doesn't exist
    if (!file_exists($pluginConfigFile)) {
        $defaultConfig = array(
            "enabled" => false,
            "twilio_account_sid" => "",
            "twilio_auth_token" => "",
            "twilio_phone_number" => "",
            "poll_interval" => 2,
            "display_duration" => 10,
            "max_messages_per_phone" => 5,
            "profanity_filter" => true,
            "use_whitelist" => false,
            "send_sms_responses" => false
        );
        file_put_contents($pluginConfigFile, json_encode($defaultConfig, JSON_PRETTY_PRINT));
    }
    
    return true;
}

// Plugin callbacks that FPP will call

// Called when plugin is installed
if (isset($_GET['install'])) {
    installDependencies();
    createDefaultFiles();
    echo "Plugin installed successfully!\n";
}

// Called when plugin is uninstalled
if (isset($_GET['uninstall'])) {
    stopPlugin();
    echo "Plugin uninstalled successfully!\n";
}

// Called when plugin is updated
if (isset($_GET['update'])) {
    stopPlugin();
    installDependencies();
    createDefaultFiles();
    startPlugin();
    echo "Plugin updated successfully!\n";
}

// Called when plugin is enabled
if (isset($_GET['enable'])) {
    startPlugin();
    echo "Plugin enabled!\n";
}

// Called when plugin is disabled
if (isset($_GET['disable'])) {
    stopPlugin();
    echo "Plugin disabled!\n";
}

// Called to check plugin status
if (isset($_GET['status'])) {
    if (isPluginRunning()) {
        echo "running";
    } else {
        echo "stopped";
    }
}
?>

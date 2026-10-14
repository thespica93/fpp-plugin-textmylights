#!/bin/bash
###############################################################################
# FPP Text My Lights Plugin - Pre Stop Script
# Runs automatically before FPPD stops. Counterpart to postStart.sh: it stops
# the daemon that postStart.sh started, so the plugin isn't left polling
# Twilio/Gmail after FPPD is gone ("stop what you start").
###############################################################################

PLUGIN_DIR="/home/fpp/media/plugins/fpp-plugin-textmylights"

"$PLUGIN_DIR/scripts/tml-stop.sh" || true

exit 0

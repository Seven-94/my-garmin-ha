"""Constants shared across the integration."""

DOMAIN = "my_garmin"
TITLE = "My Garmin"

# Garmin does not expose the key expiry date: the user enters it, as shown on
# the Garmin Connect page where the key was created.
CONF_KEY_EXPIRES = "key_expires"

# Options: when the watch counts as not synced, and how often the daily summary
# (which carries the last sync time) is polled.
CONF_SYNC_OVERDUE_HOURS = "sync_overdue_hours"
DEFAULT_SYNC_OVERDUE_HOURS = 24
CONF_DAILY_INTERVAL_MINUTES = "daily_interval_minutes"
DEFAULT_DAILY_INTERVAL_MINUTES = 10

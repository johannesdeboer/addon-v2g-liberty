# Import from official V2G Liberty

The fork can import settings and V2G Liberty's own SQLite history from a
decrypted Home Assistant partial backup of official V2G Liberty **0.9.0**.
Use a fresh installation of the fork. Its **Backup import mode** opens an
administrator-only page in Home Assistant and leaves AppDaemon stopped.

1. On the official app's Info tab, turn off **Start on boot**, then click
   **Stop**. Keep the official installation for rollback.
2. In **Settings → System → Backups**, create a manual backup and select only
   the official **V2G Liberty** app. Download the decrypted copy from the
   Home Assistant backup page. Keep the download private because it contains
   your V2G Liberty credentials.
3. Install **V2G Liberty with LAHEMS2 Schedule Export**. Before its first
   normal start, open the fork's **Configuration** tab, enable
   **Backup import mode**, and save. Leave **Start on boot** and **Watchdog**
   off during import.
4. Start the fork and select **Open Web UI**. Upload the downloaded .tar file.
   Wait for the confirmation that settings and history were imported.
5. Turn off **Backup import mode**, save and restart the fork. Confirm your
   settings, charger connection, and historical data in V2G Liberty before
   resuming automatic operation. Keep the official app stopped while the fork
   controls the charger.

The import page accepts app-only backups up to 512 MiB. It imports the settings
JSON file and the V2G Liberty history database, including committed SQLite
write-ahead-log entries when present. Home Assistant's Recorder already
belongs to your Home Assistant instance. Custom files stored in Home Assistant's
shared configuration stay there.

## Rollback

Stop the fork and disable its **Start on boot** before starting the official
app again. The official app retains the data it had when it was stopped.
Test-period entries in the fork's private database stay with the fork.

## Schedule export

Once the fork accepts a schedule in Automatic mode, the entity
**sensor.v2g_liberty_accepted_ev_schedule** exposes signed power in watts with
UTC start and end times. Select it as **Scheduled EV power from the external
controller** in LAHEMS2. V2G Liberty continues to control the charger.

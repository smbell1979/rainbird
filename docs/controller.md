# The controller

An ESP-TM2 with an LNK2 WiFi module at 192.168.0.105, which serves HTTPS
only.

## probe.py: read-only first contact

    .venv313\Scripts\python probe.py

Reads the model, firmware, zones, current state, seasonal adjust and the
stored programs. It sends no command that changes anything.

Before running:

1. Put the controller password (the one the Rain Bird app asks for) alone in
   `controller-password.txt` in the repo root. It is git-ignored and never
   printed.
2. Close the Rain Bird app on every phone. The module serves one client at a
   time.

Each run writes `probe-logs/<time>.json` (results) and `probe-logs/<time>.log`
(pyrainbird's debug log with every raw reply; any record mentioning a password
is blanked).

### Lockout guard

The LNK2 locks out local access for 15 minutes after 3 bad passwords. The probe
records each failed first contact that may have reached the password check in
`.probe-password-failures.json`, and refuses to run once there are 2 in the
last 15 minutes. A connection that was refused, or a "busy" reply, is not
counted. A successful run clears the record.

## How the module behaves

- LNK2 firmware 4.x is reported to answer the first request and then time out
  or reset connections (home-assistant/core#169765, unresolved). Measured here
  (LNK2 on ESP-TM2 fw 2.29.0, 20 reads each): a fresh HTTPS connection per
  request took ~4 s per reply and 20% went unanswered; one kept-alive connection
  took ~0.25 s and all answered. The probe therefore keeps the connection alive
  (`--fresh-connections` to re-test), spaces requests 1 s apart, retries an
  unanswered read once, and stops after 3 unanswered reads in a row. Some reads
  still need that retry.
- After an unanswered request the module often refuses the next connection
  outright for a few seconds. It seems to stay stuck on the abandoned one.
- `getServerMode` shows the module checks in with an AWS IoT endpoint every 60 s.
- The combined-state seasonal adjust reads 65535 (0xFFFF, presumably "per
  program"). The real values are per program.
- pyrainbird can read ESP-TM2 programs but not write them (`set_zone_schedule`
  is for ESP-RZXe/ST8 only).

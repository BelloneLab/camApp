Example CSV exports for the current CamApp Arduino/TTL export format.

These files mirror the CSVs written by `_save_arduino_ttl_data(...)` in
`main_window_enhanced.py`:

- `session_example_ttl_states.csv`
- `session_example_ttl_states_updated.csv` (same layout, with label columns updated to drop `_state`)
- `session_example_ttl_counts.csv`
- `session_example_behavior_summary.csv`

Example user-defined labels and pins used here:

- `gate` -> `Gate Beam` on pin `3`
- `sync` -> `Sync Pulse` on pin `9`
- `barcode` -> `Code Out` on pin `18`
- `lever` -> `Lever Press` on pin `14`
- `cue` -> `Cue LED` on pin `45`
- `reward` -> `Reward Valve` on pin `21`
- `iti` -> `ITI Light` on pin `46`

Example camera line label mapping used in `session_example_ttl_states_updated.csv`:

- `line1_status` -> `line1_status_gate`
- `line2_status` -> `line2_status_ttl_1hz`
- `line3_status` -> `line3_status_barcode`
- `line4_status` -> `line4_status_lever`

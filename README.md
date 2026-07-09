# logbook-audit

Compare ForeFlight and LogTen logbook exports and flag discrepancies.

## Getting the data

### ForeFlight

1. Login to ForeFlight on a web browser.
2. From the **Logbook** tab, go to **Export** and obtain a local copy of the file.
3. Move the file to the repo directory.
[!IMPORTANT]
Filename must be `ff.csv`.

### LogTen

1. From the **Logbook** tab, go to **Reports → Exporters → Export Flights (Tab)**.
2. **Configure Report**, confirm "All" selected, and **Generate**.
3. I use the dialogue to email this file to myself, then save it to the repo dir
   as well.
[!IMPORTANT]
Filename must be `lt.txt`.

[!NOTE]
The `.gitignore` already excludes `ff.csv` and `lt.txt`.

## Usage

Run from the repo root:

```
python audit.py
```

## Options

| Flag | Effect |
|------|--------|
| `--tol N` | Tolerance in hours before a difference is flagged (default: 0.05) |
| `--custom-night-xc` | Use a dedicated custom column for Night XC in LogTen instead of deriving it |
| `--include-sim` | Include simulator/FTS entries in the comparison |
| `--verbose` | Show extra debug info (custom column mapping, sim filter counts) |

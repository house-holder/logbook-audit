# logbook-audit

Compare ForeFlight and LogTen logbook exports and flag discrepancies.

> [!CAUTION]
> This was invented by an LLM when I asked for a simple logbook file comparison. I have tried to massage the script to be more generically useful, but it is tailored to my use case and I can't guarantee your outcome.

## Getting the data

### ForeFlight

1. Login to ForeFlight on a web browser.
2. From the **Logbook** tab, go to **Export** and obtain a local copy of the file.
3. Move the file to the repo directory as `ff.csv`.

### LogTen

1. From the **Logbook** tab, go to **Reports → Exporters → Export Flights (Tab)**.
2. **Configure Report**, confirm "All" selected, and **Generate**. Use the dialogue to obtain this file in whatever way you choose.
3. Move the file to the repo directory as `lt.txt`.

> [!NOTE]
> The filenames are hardcoded paths instead of requiring args. `ff.csv` and `lt.txt` are also in the gitignore for convenience.

## Usage

Run from the repo root:

```
python audit.py
```

## Options

You shouldn't need to use these, and I mostly took advantage of them while testing.

| Flag | Effect |
|------|--------|
| `--tol N` | Tolerance in hours before a difference is flagged (default: 0.05) |
| `--custom-night-xc` | Use a dedicated custom column for Night XC in LogTen instead of deriving it |
| `--include-sim` | Include simulator/FTS entries in the comparison |
| `--verbose` | Show extra debug info (custom column mapping, sim filter counts) |

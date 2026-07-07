# watchmaker

Batch mark whole series as watched or unwatched on the aniworld.to / bs.to family / s.to family streaming sites.

One sequential worker logs into each exact host once, discovers every season of each series URL, and invokes the site's native "mark all episodes in this season" control.

## Supported hosts

- `aniworld.to`
- `bs.to`, `bs.cine.to`, `burningseries.ac`, `burningseries.cx`
- `s.to`, `serienstream.to`, `186.2.175.5`

## Setup

1. Install dependencies:

   ```bash
   pip install -r requirements.txt
   ```

2. Copy `.env.example` to `.env` and fill in your credentials:

   ```bash
   cp .env.example .env
   ```

3. Add series URLs to the default batch file (`series_urls.txt`), one per line. Lines starting with `#` are ignored.

## Usage

Run the interactive menu:

```bash
python main.py
```

At startup you can choose the source:

- **Paste a URL** → marks that single series
- **Enter a file path** → uses that file as the batch
- **Press Enter** → uses the default batch file
- **Type 0** → exit

### Menu options

1. Mark as **WATCHED**
2. Mark as **UNWATCHED**
3. Exit

At startup, each host found in the batch is pinged once. Hosts that do not respond are skipped, so the script will not try to log into dead mirrors. If a site family has several mirrors in the batch, only one reachable mirror is used.

## Configuration

### Default batch file path

Open `config.py` and change `DEFAULT_BATCH_FILE_PATH`:

```python
DEFAULT_BATCH_FILE_PATH = "series_urls.txt"          # relative to the project folder
DEFAULT_BATCH_FILE_PATH = r"C:\Users\me\urls.txt"   # absolute path
```

### Custom batch file per run

You can also override the default for a single run:

```bash
set WATCHMAKER_URLS_FILE=path\to\urls.txt
python main.py
```

## Outputs

- `data/last_run_report.json` — full report of the last run.
- `data/.failed_urls.json` — list of URLs that failed so they can be retried.
- `logs/watchmaker.log` — detailed debug log.

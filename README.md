# watchmaker

Batch mark whole series as watched or unwatched on the aniworld.to, bs.to family, and s.to family streaming sites.

One sequential worker logs into each reachable host, discovers every season of each series URL, and invokes the site's native "mark all episodes in this season" control.

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

The program starts with the default batch file (`series_urls.txt`) already loaded. If the file is empty, the menu is still shown so you can add a URL or switch batch files with option **5**.

Each host is pinged once; unreachable hosts are skipped, and reachable family mirrors are used automatically. Raw IP addresses such as `186.2.175.5` are contacted over HTTP, all other hosts over HTTPS. Reachable hosts are resolved on startup and URLs in the batch file are rewritten to the first reachable mirror of each site family. The same refresh happens after retrying failed URLs, changing the batch, or importing URLs.

### Menu options

0. Exit
1. Mark as **WATCHED**
2. Mark as **UNWATCHED**
3. Export URLs to scraper lists
4. Retry failed URLs
5. Add / change batch
6. Import URLs from scraper lists

Before marking, a preview of every series, season, and current episode count is shown. Confirm with **y** to proceed or **n** to cancel.

### Changing the batch on the fly (option 5)

While the program is running, select **5** to:

- Paste a single URL → overwrites the default batch with that URL.
- Enter a file path → switches the current batch to that file.

### Importing URLs from scraper lists (option 6)

Select **6** to pull URLs from the scraper `series_urls.txt` files defined in `config.py` (`SERIES_URLS_EXPORTS`) and append any new URLs to the current batch file. The import preview shows which URLs will be added per family and skips anything already present in the batch.

### Manual batch override

You can override the default batch for a single run by editing `DEFAULT_BATCH_FILE` in `config.py`.

Or change the default path permanently in `config.py`:

```python
DEFAULT_BATCH_FILE = "series_urls.txt"          # relative to the project folder
DEFAULT_BATCH_FILE = r"C:\Users\me\urls.txt"   # absolute path
```

## Configuration

See `config.py` for credentials, supported domains, export/import targets, and the default batch file path.

## Outputs

- `data/.failed_urls.json` — list of URLs that failed so they can be retried.
- `logs/watchmaker.log` — detailed debug log.

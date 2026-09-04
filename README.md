# watchmaker

Batch mark whole series as watched or unwatched on the aniworld.to, bs.to family, and s.to family streaming sites.

Each reachable host gets its own worker — all hosts log in and run at once — and each worker discovers every season of its host's series URLs, strictly one series at a time, invoking the site's native "mark all episodes in this season" control.

When marking a series as **WATCHED** on the aniworld or s.to family, watchmaker also subscribes to the series first (if the subscribe control is present and not already active). The bs.to family has no subscribe control, so this step is skipped there.

## Supported hosts

- `aniworld.to`, `aniworld.cc`, `186.2.175.111`
- `bs.cine.to`, `burningseries.ac`, `burningseries.cx` (`bs.to` is a dead primary and intentionally not matched)
- `serienstream.to`, `serienstream.cx`, `186.2.175.5` (`s.to` is a dead primary and intentionally not matched)

## Requirements

- **Python 3.11+** — developed and tested on 3.14. `requires-python` in
  `pyproject.toml` enforces 3.11, so pip will refuse anything older. The code
  itself uses nothing newer than 3.10 features (`zip(strict=True)`, PEP 604
  `X | None` annotations evaluated at runtime), so 3.10 would very likely work —
  it is simply not tested.
- Dependencies: `httpx`, `lxml`, `h2`, `python-dotenv`

`lxml` and `h2` are the speed-relevant ones: pages parse ~4-6x quicker than with
BeautifulSoup, and HTTP/2 lets one connection carry many requests.

## Setup

1. Clone the repository and install dependencies:

   ```bash
   git clone https://github.com/Nawid3333/watchmaker.git
   cd watchmaker

   python -m venv .venv
   source .venv/bin/activate        # Linux / macOS
   .venv\Scripts\Activate.ps1       # Windows (PowerShell)

   pip install -r requirements.txt
   ```

2. Copy `.env.example` to `.env` and fill in your credentials:

   ```bash
   cp .env.example .env
   ```

3. Add series URLs to the default batch file (`series_urls.txt`), one per line. Lines starting with `#` are ignored. Two URLs pointing at the same series (for example `/serie/x` and `/serie/x/staffel-3`) mark the same thing, so only the first is used. To keep some entries around permanently instead of clearing them with option 7, see [The batch file has two parts](#the-batch-file-has-two-parts) below.

### Install it as a command

Building a wheel puts a `watchmaker` command on your PATH:

```bash
pip install build
python -m build
pip install dist/watchmaker-*-py3-none-any.whl
```

Two things are worth knowing before you do.

**Give each program its own virtual environment.** This project and its siblings
ship their code as the top-level modules `main` and `config`. Install two of them
into the same environment and the second overwrites the first — the command still
exists, but it silently runs the other program. `pipx` creates an isolated
environment per application and avoids this entirely:

```bash
pipx install .
```

**Tell it where to keep your files.** Once installed, the package lives inside
`site-packages`, which is no place to keep a `.env` you have to edit by hand.
Point `WATCHMAKER_HOME` at a folder you own, and `.env` , the batch file, and the `data/` and `logs/` folders all move there:

```bash
export WATCHMAKER_HOME=~/watchmaker                # Linux / macOS
$env:WATCHMAKER_HOME = "$HOME\watchmaker"          # Windows (PowerShell)

mkdir -p ~/watchmaker
cp .env.example ~/watchmaker/.env
```

If you skip that copy, the first run writes the template there for you and
says where it put it -- so an installed copy never leaves you hunting for a
file inside `site-packages`.

`WATCHMAKER_HOME` has to be a real environment variable. It cannot be set inside `.env`,
because it is what tells the program where to find that file in the first place.
Left unset it resolves to the checkout, which is why running from a clone needs
no configuration at all.

## Usage

Run the interactive menu:

```bash
python main.py
```

The program starts with the default batch file (`series_urls.txt`) already loaded. If the file is empty, the menu is still shown so you can add a URL or switch batch files with option **5**.

Each host is pinged once; unreachable hosts are skipped, and reachable family mirrors are used automatically. Raw IP addresses such as `186.2.175.5` are contacted over HTTP, all other hosts over HTTPS. Reachable hosts are resolved on startup and URLs in the batch file are rewritten to the first reachable mirror of each site family. The same refresh happens after retrying failed URLs, changing the batch, importing URLs, or clearing temporary entries.

### Menu options

1. Mark as **WATCHED**
2. Mark as **UNWATCHED**
3. Export URLs to scraper lists
4. Import URLs from scraper lists
5. Add link / change batch
6. Retry failed URLs
7. Clear temporary entries
8. Exit

Before marking, a preview of every series, season, and current episode count is shown. For the aniworld and s.to families the preview also shows the current subscription (`Sub`) and watchlist (`WL`) status per series, plus a `⚡` badge when a subscription change is pending. Confirm with **y** to proceed or **n** to cancel.

The preview is grouped rather than interleaved: **ALREADY AT TARGET**, **WILL CHANGE**, and **COULD NOT READ** are printed as separate blocks, so you can see at a glance what a run is actually going to do. Series already at the target state are reported as-is and are not touched again, so a re-run only does work where something has to change.

Marking runs every host at once. Each site keeps its own worker, session and strict one-series-at-a-time order, so no site ever sees two requests from a run — but finishing one host no longer leaves the others idle, which is what used to make switching from one domain to the next feel like a pause.

### The batch file has two parts

`series_urls.txt` has two ways to mark a URL permanent, usable together:

```
https://serienstream.to/serie/some-show
https://burningseries.ac/serie/Some-Show
-https://serienstream.to/serie/a-quick-one-off-pin

# ===== KEEP BELOW (never cleared by option 7) =====
https://serienstream.to/serie/a-show-you-always-track
```

- **A block.** Everything **from the marker line down** is permanent. Good for a group of shows you always keep, like ongoing/trash-TV watchlists.
- **A single line.** A `-` directly before one URL, with no blank line splitting them, pins just that entry — wherever it sits in the file, no need to move it into the block.

Everything else — no marker below it, no `-` in front of it — is temporary.

- **Option 7** clears temporary entries — URLs only. Your own comments and blank lines are left alone, and it shows you exactly what will go before asking.
- Adding URLs (option 5, or importing with option 4) inserts them **above** the marker, untagged, so new series always land in the working list.
- Retrying failed URLs (option 6) and pasting a single URL (option 5) replace the temporary entries only; the keep block and any `-`-tagged line are left alone.
- Permanent does **not** mean skipped: both kinds of permanent entries are still marked by options 1 and 2 like any other. Neither one controls anything but what option 7 removes.

The marker is a comment, so a batch file using neither mechanism still works exactly as before — everything in it simply counts as temporary. It's matched loosely (`# KEEP …`, any spacing, casing, or number of `=`), because it is meant to be edited by hand.

## How a result is judged

A season counts as successful only when the episode page, re-read after the request, actually shows the target state:

- Every mark is verified by re-fetching the season page, whether a request was sent or the season was already at the target state. These sites answer `HTTP 200` even when nothing changed, so the response status alone proves nothing.
- A season page where no episode rows can be parsed is reported as **failed** (`no episodes found`), never as a silent success — an unreadable page means the result cannot be verified.
- If verification itself fails (network error, error page), the season is reported as **unverified** and lands in the retry list. Re-running is safe: a season already at the target state issues no request.
- `✓` is action-aware: a fully _unwatched_ series is a success at 0 watched episodes.
- If a session expires mid-batch, watchmaker re-authenticates once and retries that season before giving up.
- A retired or mistyped slug is answered by these sites with the catalogue page at `HTTP 200`. Such a page is rejected by name (`Alle Serien`, `Andere Serien`, ...) instead of being marked as if it were a real series.

## Tests

```bash
python -m unittest discover -s tests
```

Covers URL classification, batch-file rewriting, season discovery, episode counting, title extraction, and the mark/verify logic. No extra dependencies.

### Changing the batch on the fly (option 5)

While the program is running, select **5** to:

- Paste a single URL → replaces the temporary entries with that URL, keeping the keep block and any `-`-tagged line.
- Enter a file path → switches the current batch to that file.

### Importing URLs from scraper lists (option 4)

Select **4** to pull URLs from the scraper `series_urls.txt` files defined in `config.py` (`SERIES_URLS_EXPORTS`) and append any new URLs to the current batch file. The import preview shows which URLs will be added per family and skips anything already present in the batch.

### Manual batch override

Change the default batch file permanently by editing `DEFAULT_BATCH_FILE_PATH` in `config.py`:

```python
DEFAULT_BATCH_FILE_PATH = "series_urls.txt"  # relative to the project folder
DEFAULT_BATCH_FILE_PATH = r"C:\Users\me\urls.txt"  # absolute path
```

For a one-off switch without editing any file, use option **5** while the program is running instead.

## Configuration

See `config.py` for credentials, supported domains, export/import targets, and the default batch file path.

`WATCHMAKER_HOME` decides where `.env`, the batch file, `data/` and `logs/` live.
Unset, that is this checkout. Set it when you install the package, so those do
not land in site-packages. It must be a real environment variable — it cannot go
in `.env`, because it is what locates that file.

Credentials are loaded from a `.env` file next to `config.py` (see `.env.example`):

```
ANIWORLD_EMAIL=...
ANIWORLD_PASSWORD=...
BS_USERNAME=...
BS_PASSWORD=...
STO_EMAIL=...
STO_PASSWORD=...
```

### Export targets

Menu option 3 exports URLs to each scraper's `series_urls.txt`, and option 4 reads
them back. The defaults assume the three scrapers sit next to this project, which
is the normal layout, and are derived from that — nothing is hardcoded to one
machine. Point them anywhere with:

```
WATCHMAKER_ANIWORLD_URLS=/path/to/Aniworld.to HTTPX scraper/series_urls.txt
WATCHMAKER_BS_URLS=/path/to/BS.to HTTPX scraper/series_urls.txt
WATCHMAKER_STO_URLS=/path/to/S.to HTTPX scraper/series_urls.txt
```

## Project Structure

```
├── .env.example             # Template for your credentials
├── .gitignore
├── LICENSE                  # GNU GPL v3.0
├── README.md                # This file
├── config.py                # Domains, credentials, export targets, paths
├── main.py                  # Entry point & interactive menu
├── requirements.txt         # Python dependencies
└── tests/
    └── test_watchmaker.py   # Unit tests
```

Directories created at runtime (`data/`, `logs/`), your `.env`, and your
`series_urls.txt` batch file are not part of the repository.

## Outputs

- `data/.failed_urls.json` — URLs that failed, so they can be retried. Each entry records which action (WATCHED/UNWATCHED) it failed under, so a success in one action never silently erases a failure recorded under the other for the same URL. Only URLs actually attempted in a run are reconciled, so failures recorded by an earlier run against a different batch are never silently dropped. Option 6 shows which action each failure came from and which menu option to retry it with.
- `logs/watchmaker.log` — detailed debug log.

## Author

Nawid Salehie

## License

GNU General Public License v3.0 — see [LICENSE](LICENSE) for details.

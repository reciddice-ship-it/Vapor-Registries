# Multi-State Vapor / ENDS Registry Change Tracker

Static GitHub Pages website + GitHub Actions workflow that downloads public state vapor / ENDS registry files, stores daily snapshots, compares the current file against the prior snapshot, and publishes a public change dashboard.

The starter configuration includes:

- **NC — North Carolina**: finds the current NCDOR `Export Table Data` CSV link from the source page.
- **NE — Nebraska**: downloads the Nebraska Department of Revenue XLSX directory directly.

## What it writes

For each configured state, the workflow writes:

```text
docs/data/{state}/latest.csv
docs/data/{state}/latest.json
docs/data/{state}/snapshots/YYYY-MM-DD.csv
docs/data/{state}/changes/YYYY-MM-DD.json
docs/data/{state}/index.json
```

It also writes a global index used by the dashboard:

```text
docs/data/registry_index.json
```

## Setup

1. Create a new GitHub repository.
2. Upload this project to the repository.
3. Go to **Settings → Pages**.
4. Under **Build and deployment**, choose **GitHub Actions**.
5. Go to **Actions → Update State Vapor Registries → Run workflow**.
6. Leave the `state` input blank to run all enabled states, or enter `NC` / `NE` to run only one.
7. After the first successful run, the site will update daily.

## Add a new state

Add a new object to `config/states.json`.

### Pattern 1: state page with an export link

Use this when the state has a public page and the export file URL changes over time.

```json
{
  "state": "XX",
  "state_name": "Example State",
  "enabled": true,
  "source_type": "page_export",
  "page_url": "https://example.gov/vapor-directory",
  "export_link_text": "Export Table Data",
  "file_type": "csv",
  "identity_columns": ["UPC", "Manufacturer", "Product Name"],
  "display_columns": ["UPC", "Manufacturer", "Brand Name", "Product Name", "Flavor", "Date Added"]
}
```

### Pattern 2: direct CSV/XLSX file

Use this when the state publishes a stable file URL.

```json
{
  "state": "XX",
  "state_name": "Example State",
  "enabled": true,
  "source_type": "direct_file",
  "file_url": "https://example.gov/vapor-directory.xlsx",
  "source_page_url": "https://example.gov/tobacco-products",
  "file_type": "xlsx",
  "sheet_name": "Directory",
  "header_row": 0,
  "identity_columns": ["Package UPC", "SKU", "Legal Name", "Product Name"],
  "display_columns": ["Legal Name", "Product Name", "SKU", "Package UPC", "Flavor"]
}
```

## Choosing identity columns

The `identity_columns` setting controls how the script decides whether a row is the same product across snapshots.

Good identity columns are stable fields such as:

- UPC
- SKU
- manufacturer / legal name
- brand name
- product name

Avoid using fields that commonly change, such as status, date added, notes, or review comments. If a changeable field is included in `identity_columns`, an edited row may appear as one removed row and one added row rather than a changed row.

## Local test

From the project folder:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python scripts/monitor_registries.py
```

Then open:

```text
docs/index.html
```

## Notes

- GitHub Actions cron uses UTC.
- The first run treats all rows as “added” because no prior snapshot exists yet.
- This is designed for public, non-confidential registry data.
- Before adding a state, check the state website’s terms and prefer official export endpoints over scraping visible table HTML.
- GitHub Pages is generally public. Do not publish confidential analysis or client notes in the `docs` folder.

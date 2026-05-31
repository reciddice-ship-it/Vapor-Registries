from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import pandas as pd
import requests
from bs4 import BeautifulSoup

BASE_DIR = Path(__file__).resolve().parents[1]
CONFIG_PATH = BASE_DIR / "config" / "states.json"
DOCS_DIR = BASE_DIR / "docs"
DATA_DIR = DOCS_DIR / "data"
REGISTRY_INDEX = DATA_DIR / "registry_index.json"

REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0 compatible; vapor registry change tracker; contact: repository owner"
}


def slug_state(state: str) -> str:
    return state.strip().lower()


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def get_url(url: str) -> requests.Response:
    response = requests.get(url, headers=REQUEST_HEADERS, timeout=90)
    response.raise_for_status()
    return response


def find_export_url(page_url: str, export_link_text: str | None = None) -> tuple[str, str, str | None]:
    """Return (html, export_url, page_last_modified)."""
    response = get_url(page_url)
    html = response.text
    soup = BeautifulSoup(html, "html.parser")

    export_url = None
    if export_link_text:
        wanted = " ".join(export_link_text.split()).lower()
        for a in soup.find_all("a"):
            text = " ".join(a.get_text(" ", strip=True).split()).lower()
            href = a.get("href")
            if href and wanted in text:
                export_url = urljoin(page_url, href)
                break

    # Fallbacks for government directory pages that expose direct files but change link labels.
    if not export_url:
        for extension in (".csv", ".xlsx", ".xls"):
            for a in soup.find_all("a"):
                href = a.get("href")
                if href and extension in href.lower():
                    export_url = urljoin(page_url, href)
                    break
            if export_url:
                break

    if not export_url:
        raise RuntimeError(f"Could not find an export file link on {page_url}")

    page_last_modified = extract_page_modified_date(html)
    return html, export_url, page_last_modified


def extract_page_modified_date(html: str) -> str | None:
    patterns = [
        r"This page was last modified on\s+([0-9]{2}/[0-9]{2}/[0-9]{4})",
        r"Last updated\s*:?\s*([A-Za-z]+\s+\d{1,2},\s+\d{4})",
        r"updated\s+([0-9]{2}/[0-9]{2}/[0-9]{4})",
    ]
    for pattern in patterns:
        match = re.search(pattern, html, flags=re.IGNORECASE)
        if match:
            return match.group(1)
    return None


def fetch_source_file(state_config: dict[str, Any]) -> dict[str, Any]:
    source_type = state_config.get("source_type")

    if source_type == "page_export":
        page_url = state_config["page_url"]
        _html, file_url, page_last_modified = find_export_url(
            page_url=page_url,
            export_link_text=state_config.get("export_link_text"),
        )
        file_response = get_url(file_url)
        return {
            "bytes": file_response.content,
            "source_url": page_url,
            "file_url": file_url,
            "page_last_modified": page_last_modified,
            "content_type": file_response.headers.get("content-type"),
        }

    if source_type == "direct_file":
        file_url = state_config["file_url"]
        file_response = get_url(file_url)
        return {
            "bytes": file_response.content,
            "source_url": state_config.get("source_page_url") or file_url,
            "file_url": file_url,
            "page_last_modified": None,
            "content_type": file_response.headers.get("content-type"),
        }

    raise ValueError(f"Unsupported source_type for {state_config.get('state')}: {source_type}")


def load_dataframe(source: dict[str, Any], state_config: dict[str, Any]) -> pd.DataFrame:
    file_type = (state_config.get("file_type") or "").lower()
    payload = io.BytesIO(source["bytes"])

    if file_type == "csv":
        df = pd.read_csv(payload, dtype=str, keep_default_na=False)
    elif file_type in {"xlsx", "xls"}:
        header_row = int(state_config.get("header_row", 0))
        df = pd.read_excel(
            payload,
            sheet_name=state_config.get("sheet_name", 0),
            header=header_row,
            dtype=object,
            engine="openpyxl" if file_type == "xlsx" else None,
        )
    else:
        raise ValueError(f"Unsupported file_type for {state_config.get('state')}: {file_type}")

    return normalize_dataframe(df)


def normalize_header(column: Any) -> str:
    text = str(column).strip().lstrip("\ufeff")
    text = re.sub(r"\s+", " ", text)
    if text.lower().startswith("unnamed:"):
        return ""
    return text


def normalize_cell(value: Any) -> str:
    if value is None:
        return ""
    if pd.isna(value):
        return ""
    if isinstance(value, pd.Timestamp):
        if value.hour == 0 and value.minute == 0 and value.second == 0:
            return value.date().isoformat()
        return value.isoformat()
    text = str(value).strip()
    # Excel often serializes dates as 'YYYY-MM-DD 00:00:00'. Make daily diffs less noisy.
    text = re.sub(r"^(\d{4}-\d{2}-\d{2})\s+00:00:00$", r"\1", text)
    text = re.sub(r"\s+", " ", text)
    return text


def normalize_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df.columns = [normalize_header(c) for c in df.columns]
    df = df.loc[:, [c for c in df.columns if c]]
    df = df.dropna(how="all")
    for col in df.columns:
        df[col] = df[col].map(normalize_cell)
    df = df.loc[~(df.astype(str).apply(lambda row: "".join(row.values), axis=1).str.strip() == "")]
    df = df.reset_index(drop=True)
    return df


def canonical(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def choose_identity_columns(df: pd.DataFrame, configured: list[str] | None) -> list[str]:
    configured = configured or []
    found = [c for c in configured if c in df.columns]
    if found:
        return found

    candidates = [
        "Unique Product Identifier (UPC or SKU)",
        "Package UPC",
        "UPC",
        "SKU",
        "Manufacturer",
        "Legal Name",
        "DBA",
        "Brand Name",
        "Product Name",
    ]
    found = [c for c in candidates if c in df.columns]
    if found:
        return found

    # Last-resort fallback: exact row hash. This detects added/removed rows but cannot reliably detect edits.
    return list(df.columns)


def with_identity_keys(df: pd.DataFrame, identity_columns: list[str]) -> pd.DataFrame:
    keyed = df.copy()
    keyed["_identity_base"] = keyed.apply(
        lambda row: hash_text("||".join(canonical(row.get(col, "")) for col in identity_columns)),
        axis=1,
    )
    # Keep duplicate keys stable enough to compare duplicate products without crashing.
    keyed["_identity_occurrence"] = keyed.groupby("_identity_base", sort=False).cumcount().astype(str)
    keyed["_key"] = keyed["_identity_base"] + "#" + keyed["_identity_occurrence"]
    return keyed


def latest_prior_snapshot(snapshot_dir: Path, today_name: str) -> Path | None:
    snapshots = sorted(snapshot_dir.glob("*.csv"))
    prior = [p for p in snapshots if p.name != today_name]
    return prior[-1] if prior else None


def compare_dataframes(current: pd.DataFrame, previous: pd.DataFrame | None, identity_columns: list[str]) -> dict[str, Any]:
    current_k = with_identity_keys(current, identity_columns).set_index("_key", drop=False)

    if previous is None or previous.empty:
        return {
            "added": current.to_dict(orient="records"),
            "removed": [],
            "changed": [],
        }

    previous = normalize_dataframe(previous)
    previous_identity_columns = [c for c in identity_columns if c in previous.columns]
    if not previous_identity_columns:
        previous_identity_columns = choose_identity_columns(previous, identity_columns)

    previous_k = with_identity_keys(previous, previous_identity_columns).set_index("_key", drop=False)

    current_keys = set(current_k.index)
    previous_keys = set(previous_k.index)

    added_keys = sorted(current_keys - previous_keys)
    removed_keys = sorted(previous_keys - current_keys)
    shared_keys = sorted(current_keys & previous_keys)

    hidden_cols = {"_identity_base", "_identity_occurrence", "_key"}
    added = current_k.loc[added_keys].drop(columns=[c for c in hidden_cols if c in current_k.columns]).to_dict(orient="records") if added_keys else []
    removed = previous_k.loc[removed_keys].drop(columns=[c for c in hidden_cols if c in previous_k.columns]).to_dict(orient="records") if removed_keys else []

    changed = []
    compare_columns = [c for c in current.columns if c in previous.columns]
    for key in shared_keys:
        before = previous_k.loc[key]
        after = current_k.loc[key]
        field_changes = {}
        for col in compare_columns:
            before_value = normalize_cell(before.get(col, ""))
            after_value = normalize_cell(after.get(col, ""))
            if before_value != after_value:
                field_changes[col] = {"before": before_value, "after": after_value}
        if field_changes:
            changed.append(summarize_changed_row(after, field_changes))

    return {"added": added, "removed": removed, "changed": changed}


def summarize_changed_row(row: pd.Series, field_changes: dict[str, dict[str, str]]) -> dict[str, Any]:
    identifier = first_present(row, ["Unique Product Identifier (UPC or SKU)", "Package UPC", "UPC", "SKU"])
    manufacturer = first_present(row, ["Manufacturer", "Legal Name", "DBA"])
    brand = first_present(row, ["Brand Name", "DBA"])
    product = first_present(row, ["Product Name", "SKU"])
    return {
        "identifier": identifier,
        "manufacturer": manufacturer,
        "brand": brand,
        "product": product,
        "changes": field_changes,
    }


def first_present(row: pd.Series, columns: list[str]) -> str:
    for col in columns:
        value = normalize_cell(row.get(col, ""))
        if value:
            return value
    return ""


def process_state(state_config: dict[str, Any], generated_at: datetime) -> dict[str, Any]:
    state = state_config["state"].upper()
    state_slug = slug_state(state)
    out_dir = DATA_DIR / state_slug
    snapshot_dir = out_dir / "snapshots"
    change_dir = out_dir / "changes"
    out_dir.mkdir(parents=True, exist_ok=True)
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    change_dir.mkdir(parents=True, exist_ok=True)

    today = generated_at.date().isoformat()
    snapshot_name = f"{today}.csv"

    source = fetch_source_file(state_config)
    df = load_dataframe(source, state_config)
    identity_columns = choose_identity_columns(df, state_config.get("identity_columns"))

    prior_path = latest_prior_snapshot(snapshot_dir, snapshot_name)
    previous_df = pd.read_csv(prior_path, dtype=str, keep_default_na=False) if prior_path else None

    changes = compare_dataframes(df, previous_df, identity_columns)
    summary = {
        "state": state,
        "state_name": state_config.get("state_name", state),
        "generated_at_utc": generated_at.isoformat(),
        "source_url": source["source_url"],
        "source_file_url": source["file_url"],
        "source_content_type": source.get("content_type"),
        "source_page_last_modified": source.get("page_last_modified"),
        "row_count": int(len(df)),
        "columns": list(df.columns),
        "identity_columns_used": identity_columns,
        "display_columns": [c for c in state_config.get("display_columns", []) if c in df.columns],
        "compared_to_snapshot": prior_path.name if prior_path else None,
        "counts": {
            "added": len(changes["added"]),
            "removed": len(changes["removed"]),
            "changed": len(changes["changed"]),
        },
        "changes": changes,
    }

    df.to_csv(out_dir / "latest.csv", index=False)
    df.to_json(out_dir / "latest.json", orient="records", indent=2, force_ascii=False)
    df.to_csv(snapshot_dir / snapshot_name, index=False)
    write_json(change_dir / f"{today}.json", summary)

    history = build_state_history(state_slug, change_dir)
    state_index = {
        "state": state,
        "state_name": state_config.get("state_name", state),
        "enabled": state_config.get("enabled", True),
        "source_url": source["source_url"],
        "source_file_url": source["file_url"],
        "latest_csv_file": f"data/{state_slug}/latest.csv",
        "latest_json_file": f"data/{state_slug}/latest.json",
        "latest_change_file": history[0]["file"] if history else None,
        "history": history,
    }
    write_json(out_dir / "index.json", state_index)
    return state_index


def build_state_history(state_slug: str, change_dir: Path, limit: int = 90) -> list[dict[str, Any]]:
    history = []
    for p in sorted(change_dir.glob("*.json"), reverse=True)[:limit]:
        try:
            data = read_json(p, {})
            history.append(
                {
                    "date": p.stem,
                    "file": f"data/{state_slug}/changes/{p.name}",
                    "row_count": data.get("row_count"),
                    "counts": data.get("counts", {}),
                    "source_file_url": data.get("source_file_url"),
                    "source_page_last_modified": data.get("source_page_last_modified"),
                    "generated_at_utc": data.get("generated_at_utc"),
                }
            )
        except Exception:
            continue
    return history


def write_global_index(state_indexes: list[dict[str, Any]], generated_at: datetime) -> None:
    write_json(
        REGISTRY_INDEX,
        {
            "generated_at_utc": generated_at.isoformat(),
            "states": state_indexes,
        },
    )


def load_states(config_path: Path = CONFIG_PATH) -> list[dict[str, Any]]:
    states = read_json(config_path, [])
    if not isinstance(states, list):
        raise ValueError("config/states.json must contain a JSON array of state configurations.")
    return states


def main() -> None:
    parser = argparse.ArgumentParser(description="Monitor state vapor/ENDS registries and write static dashboard data.")
    parser.add_argument("--state", action="append", help="Optional state abbreviation to run. Can be repeated.")
    parser.add_argument("--config", default=str(CONFIG_PATH), help="Path to states.json config.")
    args = parser.parse_args()

    requested_states = {s.upper() for s in args.state or []}
    states = load_states(Path(args.config))
    generated_at = datetime.now(timezone.utc)

    existing_global = read_json(REGISTRY_INDEX, {"states": []})
    existing_by_state = {s.get("state"): s for s in existing_global.get("states", []) if s.get("state")}

    updated_indexes: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []

    for state_config in states:
        state = state_config.get("state", "").upper()
        if not state_config.get("enabled", True):
            if state in existing_by_state:
                updated_indexes.append(existing_by_state[state])
            continue
        if requested_states and state not in requested_states:
            if state in existing_by_state:
                updated_indexes.append(existing_by_state[state])
            continue

        try:
            updated_indexes.append(process_state(state_config, generated_at))
            print(f"Processed {state}")
        except Exception as exc:
            print(f"ERROR processing {state}: {exc}")
            errors.append({"state": state, "error": str(exc)})
            if state in existing_by_state:
                updated_indexes.append(existing_by_state[state])

    # Keep configured-state ordering and include states that were not run yet as empty placeholders.
    by_state = {s.get("state"): s for s in updated_indexes if s.get("state")}
    ordered = []
    for state_config in states:
        state = state_config.get("state", "").upper()
        if state in by_state:
            ordered.append(by_state[state])
        else:
            state_slug = slug_state(state)
            ordered.append(
                {
                    "state": state,
                    "state_name": state_config.get("state_name", state),
                    "enabled": state_config.get("enabled", True),
                    "source_url": state_config.get("source_page_url") or state_config.get("page_url") or state_config.get("file_url"),
                    "source_file_url": state_config.get("file_url"),
                    "latest_csv_file": f"data/{state_slug}/latest.csv",
                    "latest_json_file": f"data/{state_slug}/latest.json",
                    "latest_change_file": None,
                    "history": [],
                }
            )

    global_index = {
        "generated_at_utc": generated_at.isoformat(),
        "states": ordered,
    }
    if errors:
        global_index["errors"] = errors
    write_json(REGISTRY_INDEX, global_index)

    if errors:
        raise SystemExit(f"Completed with {len(errors)} state error(s). See docs/data/registry_index.json.")


if __name__ == "__main__":
    main()

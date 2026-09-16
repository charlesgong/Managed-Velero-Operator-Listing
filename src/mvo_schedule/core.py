from __future__ import annotations

import csv
import json
import os
import re
import shlex
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence


PRODUCTION_OCM_URL = "https://api.openshift.com"
SHEET_HEADERS = [
    "Cluster ID",
    "Cluster Name",
    "Version",
    "Owner/Org",
    "Region",
    "MVO ",
    "OADP ",
    "Backups",
    "Schedules",
]
EXTERNAL_ID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-"
    r"[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$"
)
INTERNAL_ID_RE = re.compile(r"^[0-9a-z]{32}$")
EXTERNAL_ID_IN_TEXT_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
INTERNAL_ID_IN_TEXT_RE = re.compile(r"(?<![0-9a-z])[0-9a-z]{32}(?![0-9a-z])")


class MVOError(RuntimeError):
    """An expected workflow failure that should stop all downstream writes."""


class ValidationError(MVOError):
    pass


def redact_cluster_identifiers(text: str) -> str:
    text = EXTERNAL_ID_IN_TEXT_RE.sub(lambda match: match.group(0)[:8] + "…", text)
    return INTERNAL_ID_IN_TEXT_RE.sub(lambda match: match.group(0)[:8] + "…", text)


@dataclass(frozen=True)
class CommandResult:
    stdout: str
    stderr: str
    returncode: int


CommandRunner = Callable[[Sequence[str], Optional[Mapping[str, str]], int], CommandResult]


def run_command(
    argv: Sequence[str], env: Mapping[str, str] | None = None, timeout: int = 60
) -> CommandResult:
    try:
        result = subprocess.run(
            list(argv),
            capture_output=True,
            text=True,
            timeout=timeout,
            env=dict(env) if env else None,
            check=False,
        )
    except FileNotFoundError as exc:
        raise MVOError(f"Required command is not installed: {argv[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise MVOError(f"Command timed out after {timeout}s: {shlex.join(argv)}") from exc
    return CommandResult(result.stdout.strip(), result.stderr.strip(), result.returncode)


def require_success(result: CommandResult, description: str) -> str:
    if result.returncode != 0:
        detail = result.stderr or result.stdout or f"exit {result.returncode}"
        raise MVOError(f"{description} failed: {detail[:500]}")
    return result.stdout.strip()


def verify_ocm_production(
    runner: CommandRunner = run_command, timeout: int = 30
) -> str:
    url = require_success(
        runner(["ocm", "config", "get", "url"], None, timeout),
        "OCM environment check",
    )
    if url != PRODUCTION_OCM_URL:
        raise MVOError(
            f"OCM is not configured for production: got {url!r}, "
            f"expected {PRODUCTION_OCM_URL!r}. No data was changed."
        )
    identity = require_success(runner(["ocm", "whoami"], None, timeout), "OCM login check")
    if not identity:
        raise MVOError("OCM login check returned no identity. No data was changed.")
    return identity


def validate_external_ids(values: Iterable[str]) -> list[str]:
    ids = [value.strip() for value in values if value.strip()]
    if not ids:
        raise ValidationError("The generated MVO full list is empty")
    invalid = [value for value in ids if not EXTERNAL_ID_RE.fullmatch(value)]
    if invalid:
        raise ValidationError(f"Invalid external cluster IDs: {', '.join(invalid[:5])}")
    duplicates = sorted({value for value in ids if ids.count(value) > 1})
    if duplicates:
        raise ValidationError(f"Duplicate external cluster IDs: {', '.join(duplicates[:5])}")
    return ids


def _ids_from_json(payload: Any) -> list[str]:
    if isinstance(payload, list):
        return [str(value) for value in payload]
    if isinstance(payload, dict) and isinstance(payload.get("clusters"), list):
        return [str(value) for value in payload["clusters"]]
    results = payload.get("data", {}).get("result", []) if isinstance(payload, dict) else []
    if isinstance(results, list):
        ids = [item.get("metric", {}).get("_id") for item in results if isinstance(item, dict)]
        if ids and all(ids):
            return [str(value) for value in ids]
    raise ValidationError("Source JSON does not contain a cluster list or Prometheus _id results")


def parse_cluster_source(text: str) -> list[str]:
    stripped = text.strip()
    if not stripped:
        raise ValidationError("MVO cluster source returned no data")
    if stripped[0] in "[{":
        try:
            return validate_external_ids(_ids_from_json(json.loads(stripped)))
        except json.JSONDecodeError as exc:
            raise ValidationError(f"MVO cluster source returned invalid JSON: {exc}") from exc
    return validate_external_ids(stripped.splitlines())


def generate_full_list(
    output_path: Path,
    *,
    source_file: Path | None = None,
    source_command: Sequence[str] | None = None,
    runner: CommandRunner = run_command,
    timeout: int = 120,
) -> list[str]:
    if bool(source_file) == bool(source_command):
        raise ValidationError("Configure exactly one of source_file or full_list_command")
    if source_file:
        try:
            source_text = source_file.read_text(encoding="utf-8")
        except OSError as exc:
            raise MVOError(f"Cannot read MVO source file {source_file}: {exc}") from exc
    else:
        assert source_command is not None
        source_text = require_success(
            runner(list(source_command), None, timeout), "MVO telemetry export"
        )
    ids = parse_cluster_source(source_text)
    atomic_write_text(output_path, "".join(f"{cluster_id}\n" for cluster_id in ids))
    reread = validate_external_ids(output_path.read_text(encoding="utf-8").splitlines())
    if reread != ids:
        raise ValidationError("MVO full-list read-back did not match generated data")
    return ids


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(text, encoding="utf-8")
    temp.replace(path)


def atomic_write_json(path: Path, payload: Any) -> None:
    atomic_write_text(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _json_result(
    argv: Sequence[str], description: str, runner: CommandRunner, timeout: int
) -> Any:
    raw = require_success(runner(argv, None, timeout), description)
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise MVOError(f"{description} returned invalid JSON: {exc}") from exc


def get_cluster_metadata(
    external_id: str, runner: CommandRunner = run_command, timeout: int = 60
) -> dict[str, Any]:
    payload = _json_result(
        ["ocm", "get", f"/api/clusters_mgmt/v1/clusters?search=external_id='{external_id}'"],
        f"OCM lookup for {external_id}",
        runner,
        timeout,
    )
    items = payload.get("items", []) if isinstance(payload, dict) else []
    if len(items) != 1:
        raise ValidationError(f"OCM returned {len(items)} clusters for external ID {external_id}")
    cluster = items[0]
    sts = cluster.get("aws", {}).get("sts", {}).get("enabled")
    internal_id = str(cluster.get("id", ""))
    if not INTERNAL_ID_RE.fullmatch(internal_id):
        raise ValidationError(f"OCM returned invalid internal ID for {external_id}")
    return {
        "external_id": external_id,
        "internal_id": internal_id,
        "name": str(cluster.get("name", "")),
        "version": str(cluster.get("openshift_version", "") or cluster.get("version", {}).get("raw_id", "")),
        "region": str(cluster.get("region", {}).get("id", "")),
        "product": str(cluster.get("product", {}).get("id", "")),
        "subscription_id": str(cluster.get("subscription", {}).get("id", "")),
        "sts_enabled": bool(sts),
    }


def resolve_org_name(
    subscription_id: str,
    runner: CommandRunner = run_command,
    timeout: int = 60,
) -> str:
    if not subscription_id:
        raise ValidationError("Cluster has no subscription ID")
    subscription = _json_result(
        ["ocm", "get", f"/api/accounts_mgmt/v1/subscriptions/{subscription_id}"],
        f"OCM subscription lookup {subscription_id}",
        runner,
        timeout,
    )
    org_id = str(subscription.get("organization_id", ""))
    if not org_id:
        raise ValidationError(f"Subscription {subscription_id} has no organization ID")
    organization = _json_result(
        ["ocm", "get", f"/api/accounts_mgmt/v1/organizations/{org_id}"],
        f"OCM organization lookup {org_id}",
        runner,
        timeout,
    )
    name = str(organization.get("name", "")).strip()
    if not name:
        raise ValidationError(f"Organization {org_id} has no name")
    return name


def _oc_get_state(
    argv: Sequence[str], env: Mapping[str, str], runner: CommandRunner, timeout: int
) -> tuple[bool, str]:
    result = runner(argv, env, timeout)
    if result.returncode != 0:
        return False, result.stderr or result.stdout or f"exit {result.returncode}"
    return bool(result.stdout.strip()), ""


def _oc_count(
    resource: str,
    namespace: str,
    env: Mapping[str, str],
    runner: CommandRunner,
    timeout: int,
) -> int:
    result = runner(
        ["oc", "get", resource, "-n", namespace, "--no-headers", "--ignore-not-found"],
        env,
        timeout,
    )
    if result.returncode != 0:
        raise MVOError(result.stderr or result.stdout or f"oc exited {result.returncode}")
    return len([line for line in result.stdout.splitlines() if line.strip()])


def collect_cluster(
    external_id: str,
    runner: CommandRunner = run_command,
    timeout: int = 60,
    org_cache: dict[str, str] | None = None,
) -> dict[str, Any]:
    meta = get_cluster_metadata(external_id, runner, timeout)
    cache = org_cache if org_cache is not None else {}
    subscription_id = meta["subscription_id"]
    if subscription_id not in cache:
        cache[subscription_id] = resolve_org_name(subscription_id, runner, timeout)
    meta["org"] = cache[subscription_id]

    with tempfile.NamedTemporaryFile(suffix=".kubeconfig", delete=False) as handle:
        kubeconfig = handle.name
    env = os.environ.copy()
    env["KUBECONFIG"] = kubeconfig
    try:
        require_success(
            runner(["ocm", "backplane", "login", meta["internal_id"]], env, timeout),
            f"Backplane login for {external_id}",
        )
        mvo_exists, error = _oc_get_state(
            [
                "oc",
                "get",
                "deployment",
                "managed-velero-operator",
                "-n",
                "openshift-velero",
                "--no-headers",
                "--ignore-not-found",
            ],
            env,
            runner,
            timeout,
        )
        if error:
            raise MVOError(f"MVO deployment check failed: {error}")
        oadp_exists, error = _oc_get_state(
            ["oc", "get", "namespace", "openshift-adp", "--no-headers", "--ignore-not-found"],
            env,
            runner,
            timeout,
        )
        if error:
            raise MVOError(f"OADP namespace check failed: {error}")
        backups = _oc_count("backups.velero.io", "openshift-velero", env, runner, timeout)
        schedules = _oc_count("schedules.velero.io", "openshift-velero", env, runner, timeout)
        if oadp_exists:
            backups += _oc_count("backups.velero.io", "openshift-adp", env, runner, timeout)
            schedules += _oc_count("schedules.velero.io", "openshift-adp", env, runner, timeout)
        meta.update(
            mvo="Yes" if mvo_exists else "No",
            oadp="Yes" if oadp_exists else "No",
            backups=backups,
            schedules=schedules,
            status="ok",
            error="",
        )
        return meta
    except MVOError as exc:
        meta.update(
            mvo="Unknown",
            oadp="Unknown",
            backups="",
            schedules="",
            status="error",
            error=str(exc),
        )
        return meta
    finally:
        try:
            os.unlink(kubeconfig)
        except OSError:
            pass


def collect_clusters(
    external_ids: Sequence[str],
    *,
    runner: CommandRunner = run_command,
    workers: int = 8,
    timeout: int = 60,
) -> list[dict[str, Any]]:
    results: dict[str, dict[str, Any]] = {}
    # Organization caching is deliberately per worker task. It avoids shared mutable
    # state and keeps a failed lookup from contaminating other cluster results.
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(collect_cluster, external_id, runner, timeout, {}): external_id
            for external_id in external_ids
        }
        for future in as_completed(futures):
            external_id = futures[future]
            try:
                results[external_id] = future.result()
            except Exception as exc:  # retain a complete diagnostic report
                results[external_id] = {
                    "external_id": external_id,
                    "status": "error",
                    "error": str(exc),
                }
    return [results[external_id] for external_id in external_ids]


def merge_collection_with_sheet(
    rows: Sequence[Mapping[str, Any]], current_values: Sequence[Sequence[Any]]
) -> list[dict[str, Any]]:
    """Preserve the last verified worksheet fields for transient probe failures.

    OCM metadata (including internal ID and STS status) must still have succeeded.
    This lets hibernating or temporarily inaccessible clusters retain their last
    known values without turning an access failure into false zero/No results.
    """
    validate_sheet_matrix(current_values)
    previous = {
        str(values[0]): list(values)
        for values in current_values[1:]
        if values and len(values) == len(SHEET_HEADERS)
    }
    merged: list[dict[str, Any]] = []
    for source_row in rows:
        row = dict(source_row)
        if row.get("status") == "ok":
            merged.append(row)
            continue
        external_id = str(row.get("external_id", ""))
        old = previous.get(external_id)
        if (
            old is None
            or not INTERNAL_ID_RE.fullmatch(str(row.get("internal_id", "")))
            or "sts_enabled" not in row
        ):
            merged.append(row)
            continue
        row.update(
            name=old[1],
            version=old[2],
            org=old[3],
            region=old[4],
            mvo=old[5],
            oadp=old[6],
            backups=old[7],
            schedules=old[8],
            status="stale",
            fallback="sheet_snapshot",
        )
        merged.append(row)
    return merged


def validate_collection(rows: Sequence[Mapping[str, Any]], expected_ids: Sequence[str]) -> None:
    if [row.get("external_id") for row in rows] != list(expected_ids):
        raise ValidationError("Collected cluster order/identity does not match the full list")
    errors = [row for row in rows if row.get("status") not in {"ok", "stale"}]
    if errors:
        examples = "; ".join(
            f"{str(row.get('external_id'))[:8]}…: "
            f"{redact_cluster_identifiers(str(row.get('error', 'unknown error')))}"
            for row in errors[:5]
        )
        raise ValidationError(
            f"Cluster collection was incomplete ({len(errors)} of {len(rows)} failed): {examples}"
        )
    required = ("internal_id", "name", "version", "org", "region", "mvo", "oadp")
    missing = [
        str(row.get("external_id"))
        for row in rows
        if any(row.get(field) in (None, "") for field in required)
    ]
    if missing:
        raise ValidationError(f"Collected rows have missing required fields: {', '.join(missing[:5])}")
    internal_ids = [str(row["internal_id"]) for row in rows]
    if len(set(internal_ids)) != len(internal_ids):
        raise ValidationError("OCM returned duplicate internal cluster IDs")


def sheet_values(rows: Sequence[Mapping[str, Any]]) -> list[list[Any]]:
    values: list[list[Any]] = [SHEET_HEADERS.copy()]
    for row in rows:
        values.append(
            [
                row["external_id"],
                row["name"],
                row["version"],
                row["org"],
                row["region"],
                row["mvo"],
                row["oadp"],
                row["backups"],
                row["schedules"],
            ]
        )
    return values


def write_collection_csv(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    with temp.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerows(sheet_values(rows))
    temp.replace(path)


def write_classification_csv(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    with temp.open("w", encoding="utf-8", newline="") as handle:
        fields = ["external_id", "internal_id", "name", "product", "sts_enabled", "status", "error"]
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    temp.replace(path)


def load_collection_artifacts(run_dir: Path) -> tuple[list[dict[str, Any]], Path]:
    cluster_files = sorted(run_dir.glob("mvo_cluster_list_*.csv"))
    if len(cluster_files) != 1:
        raise ValidationError(
            f"Expected exactly one dated MVO cluster list in {run_dir}, found {len(cluster_files)}"
        )
    cluster_path = cluster_files[0]
    classification_path = run_dir / "mvo_sts_classification.csv"
    try:
        with cluster_path.open(encoding="utf-8", newline="") as handle:
            matrix = list(csv.reader(handle))
        with classification_path.open(encoding="utf-8", newline="") as handle:
            classifications = {
                row["external_id"]: row for row in csv.DictReader(handle)
            }
    except OSError as exc:
        raise MVOError(f"Cannot load run artifacts from {run_dir}: {exc}") from exc
    validate_sheet_matrix(matrix)
    rows: list[dict[str, Any]] = []
    for values in matrix[1:]:
        external_id = values[0]
        classification = classifications.get(external_id)
        if classification is None:
            raise ValidationError(f"Missing classification for {external_id[:8]}…")
        status = classification.get("status", "")
        backups: Any = values[7]
        schedules: Any = values[8]
        if status == "ok":
            try:
                backups = int(backups)
                schedules = int(schedules)
            except ValueError as exc:
                raise ValidationError(
                    f"Fresh row {external_id[:8]}… has non-numeric backup/schedule counts"
                ) from exc
        rows.append(
            {
                "external_id": external_id,
                "internal_id": classification.get("internal_id", ""),
                "name": values[1],
                "version": values[2],
                "org": values[3],
                "region": values[4],
                "product": classification.get("product", ""),
                "sts_enabled": classification.get("sts_enabled", "").lower() == "true",
                "mvo": values[5],
                "oadp": values[6],
                "backups": backups,
                "schedules": schedules,
                "status": status,
                "error": classification.get("error", ""),
            }
        )
    if len(classifications) != len(rows):
        raise ValidationError("Classification and dated cluster-list row counts do not match")
    validate_collection(rows, [row["external_id"] for row in rows])
    return rows, cluster_path


def _column_count(values: Sequence[Sequence[Any]]) -> int:
    return max((len(row) for row in values), default=0)


def validate_sheet_matrix(values: Sequence[Sequence[Any]]) -> None:
    if not values or list(values[0]) != SHEET_HEADERS:
        raise ValidationError("Google Sheet header does not exactly match the approved MVO schema")
    if _column_count(values) != len(SHEET_HEADERS):
        raise ValidationError("Google Sheet data extends outside the approved nine-column schema")
    ids = [str(row[0]) for row in values[1:] if row]
    validate_external_ids(ids)


class GoogleSheetsClient:
    def __init__(
        self,
        sheet_id: str,
        sheet_range: str,
        runner: CommandRunner = run_command,
        timeout: int = 60,
        quota_project: str | None = None,
    ) -> None:
        self.sheet_id = sheet_id
        self.sheet_range = sheet_range
        self.runner = runner
        self.timeout = timeout
        self.quota_project = quota_project or os.environ.get("GOOGLE_QUOTA_PROJECT", "").strip()

    def _token(self) -> str:
        token = os.environ.get("GOOGLE_OAUTH_ACCESS_TOKEN", "").strip()
        if token:
            return token
        result = self.runner(
            ["gcloud", "auth", "application-default", "print-access-token"], None, self.timeout
        )
        token = require_success(result, "Google application-default authentication")
        if not token:
            raise MVOError("Google authentication returned an empty access token")
        return token

    def _request(self, method: str, url: str, payload: Any | None = None) -> Any:
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {self._token()}",
            "Content-Type": "application/json",
        }
        if self.quota_project:
            headers["X-Goog-User-Project"] = self.quota_project
        request = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers=headers,
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise MVOError(f"Google Sheets API returned HTTP {exc.code}: {detail[:500]}") from exc
        except urllib.error.URLError as exc:
            raise MVOError(f"Google Sheets API request failed: {exc.reason}") from exc
        return json.loads(body) if body else {}

    def _values_url(self, range_name: str | None = None) -> str:
        encoded = urllib.parse.quote(range_name or self.sheet_range, safe="")
        return f"https://sheets.googleapis.com/v4/spreadsheets/{self.sheet_id}/values/{encoded}"

    def read(self) -> list[list[Any]]:
        payload = self._request("GET", self._values_url() + "?valueRenderOption=UNFORMATTED_VALUE")
        return payload.get("values", [])

    def write(self, values: Sequence[Sequence[Any]]) -> None:
        self._request(
            "PUT",
            self._values_url() + "?valueInputOption=RAW",
            {"range": self.sheet_range, "majorDimension": "ROWS", "values": values},
        )

    def clear(self, start_row: int, end_row: int) -> None:
        if end_row < start_row:
            return
        tab = self.sheet_range.split("!", 1)[0]
        clear_range = f"{tab}!A{start_row}:I{end_row}"
        self._request("POST", self._values_url(clear_range) + ":clear", {})


def update_sheet_verified(
    client: Any,
    expected: list[list[Any]],
    snapshot_path: Path,
    *,
    write: bool,
    current: list[list[Any]] | None = None,
) -> str:
    current = client.read() if current is None else current
    validate_sheet_matrix(current)
    atomic_write_json(snapshot_path, {"values": current})
    if not write:
        return "dry-run"
    old_rows = len(current)
    try:
        client.write(expected)
        if old_rows > len(expected):
            client.clear(len(expected) + 1, old_rows)
        readback = client.read()
        if readback != expected:
            atomic_write_json(
                snapshot_path.with_name("sheet_readback_mismatch.json"),
                {"expected": expected, "actual": readback},
            )
            raise ValidationError("Google Sheet read-back does not match the intended values")
    except Exception:
        try:
            client.write(current)
            if len(expected) > old_rows:
                client.clear(old_rows + 1, len(expected))
        except Exception as rollback_error:
            raise MVOError(f"Sheet update failed and rollback also failed: {rollback_error}")
        raise
    return "updated"


def prepare_service_log_targets(
    rows: Sequence[Mapping[str, Any]],
    state_path: Path,
    output_dir: Path,
    run_date: str,
    *,
    commit_state: bool,
) -> dict[str, Any]:
    eligible = {str(row["external_id"]) for row in rows if not bool(row["sts_enabled"])}
    mapping = {str(row["external_id"]): str(row["internal_id"]) for row in rows}
    if state_path.exists():
        state = json.loads(state_path.read_text(encoding="utf-8"))
        seen = set(state.get("seen_external_ids", []))
        pending = set(state.get("pending_external_ids", []))
        new_ids = eligible - seen
        pending = (pending | new_ids) & eligible
        bootstrapped = False
    else:
        seen = set()
        pending = set()
        new_ids = set()
        bootstrapped = True
    unresolved = sorted(external_id for external_id in pending if external_id not in mapping)
    if unresolved:
        raise ValidationError(f"Pending service-log IDs have no OCM mapping: {', '.join(unresolved[:5])}")
    pending_external_ids = sorted(pending)
    pending_internal_ids = [mapping[external_id] for external_id in pending_external_ids]
    all_external_ids = sorted(mapping)
    all_internal_ids = [mapping[external_id] for external_id in all_external_ids]
    if any(not INTERNAL_ID_RE.fullmatch(value) for value in all_internal_ids):
        raise ValidationError("Generated service-log list contains invalid internal cluster IDs")
    output_dir.mkdir(parents=True, exist_ok=True)
    external_path = output_dir / f"cluster_list_{run_date}.json"
    internal_path = output_dir / f"mvo_clusters_{run_date}.json"
    pending_external_path = output_dir / f"pending_cluster_list_{run_date}.json"
    pending_internal_path = output_dir / f"pending_mvo_clusters_{run_date}.json"
    mapping_path = output_dir / f"mvo_cluster_mapping_{run_date}.csv"
    atomic_write_json(external_path, {"clusters": all_external_ids})
    atomic_write_json(internal_path, {"clusters": all_internal_ids})
    atomic_write_json(pending_external_path, {"clusters": pending_external_ids})
    atomic_write_json(pending_internal_path, {"clusters": pending_internal_ids})
    with mapping_path.with_suffix(".csv.tmp").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["external_id", "internal_id"])
        writer.writerows(zip(all_external_ids, all_internal_ids))
    mapping_path.with_suffix(".csv.tmp").replace(mapping_path)
    next_state = {
        "seen_external_ids": sorted(eligible),
        "pending_external_ids": pending_external_ids,
        "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    if commit_state:
        atomic_write_json(state_path, next_state)
    return {
        "bootstrapped": bootstrapped,
        "new_count": len(new_ids),
        "full_count": len(all_external_ids),
        "pending_count": len(pending_external_ids),
        "external_path": str(external_path),
        "internal_path": str(internal_path),
        "pending_external_path": str(pending_external_path),
        "pending_internal_path": str(pending_internal_path),
        "mapping_path": str(mapping_path),
    }


def acknowledge_pending(state_path: Path, acknowledged_ids: Sequence[str]) -> int:
    if not state_path.exists():
        raise ValidationError(f"State file does not exist: {state_path}")
    state = json.loads(state_path.read_text(encoding="utf-8"))
    pending = set(state.get("pending_external_ids", []))
    unknown = set(acknowledged_ids) - pending
    if unknown:
        raise ValidationError(f"IDs are not pending: {', '.join(sorted(unknown)[:5])}")
    pending -= set(acknowledged_ids)
    state["pending_external_ids"] = sorted(pending)
    state["updated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    atomic_write_json(state_path, state)
    return len(pending)

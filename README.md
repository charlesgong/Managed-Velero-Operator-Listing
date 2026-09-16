# MVO cluster scan

This project runs the temporary local workflow for discovering clusters affected by Managed Velero Operator deprecation, validating the results, updating the approved spreadsheet, and preparing cluster lists for a service-log dry run.

The Codex desktop automation runs this repository on the local machine. The machine must be awake with Codex available at the scheduled time.

## Safety boundaries

- Credentials are never stored in this repository. OCM, Google, and GitHub authentication must come from local authenticated tools or the operating-system credential store.
- Every run must verify that OCM is authenticated against the production API before reading cluster data.
- Generated cluster IDs, mappings, reports, logs, and scheduler state remain local and are ignored by Git.
- Spreadsheet changes occur only after all collection checks pass and are verified by reading the updated range back.
- The scheduled workflow may prepare and validate service-log input, but it must not run `osdctl servicelog post --yes`.

## Workflow

1. Verify `ocm config get url` is exactly `https://api.openshift.com` and `ocm whoami` succeeds.
2. Run `scripts/generate_mvo_cluster_list.py` to produce a fresh, validated `mvo-full-list.txt` from the configured telemetry export.
3. Query OCM with JSON responses, classify STS/non-STS clusters, and collect the nine approved MVO worksheet fields. Any lookup, backplane, or `oc` error blocks later writes.
4. Snapshot the current `MVO!A:I` range, update only that range, read it back, and roll it back if verification fails.
5. Bootstrap the first successful run without sending notices. On later runs, retain newly discovered non-STS clusters in a pending set until a human acknowledges that the service logs were posted.
6. Generate a dated external-ID audit list plus the internal-ID JSON accepted by `osdctl servicelog post --clusters-file`. The scheduled workflow never posts it.

## Configuration

Copy `config.example.json` to `config.local.json` and replace `full_list_command` with the approved command that emits either one external UUID per line, a JSON `{"clusters": [...]}` object, or a Prometheus JSON response with `_id` labels. The command is executed directly without a shell.

No telemetry export command was present in the original scripts, so it is an explicit required configuration instead of an invented production dependency.

Google authentication uses, in order:

1. an ephemeral `GOOGLE_OAUTH_ACCESS_TOKEN` environment variable, or
2. `gcloud auth application-default print-access-token`.

Neither token is printed or written to the repository.

## Commands

Run tests:

```sh
PYTHONPYCACHEPREFIX=/tmp/mvo-pycache PYTHONPATH=src python3 -m unittest discover -s tests -v
```

Production preflight and read-only Sheet verification:

```sh
PYTHONPATH=src python3 scripts/run_mvo_schedule.py --config config.local.json
```

Update and verify the Sheet only after the read-only run succeeds:

```sh
PYTHONPATH=src python3 scripts/run_mvo_schedule.py --config config.local.json --write-sheet
```

To acknowledge service logs after a manual, reviewed post, put the external IDs that were successfully notified in a local file and run:

```sh
PYTHONPATH=src python3 scripts/run_mvo_schedule.py --ack-file acknowledged_external_ids.txt
```

Run artifacts are written under `runs/`; state is written under `state/`. Both are ignored by Git.

## Codex schedule

The Codex automation is scheduled for Mondays at 11:00 local time. It is safe to remain active while configuration is incomplete because the runner exits before external writes. A successful scheduled write requires all of these checks to pass:

- OCM is logged in to production.
- `config.local.json` contains the approved telemetry export command.
- Google application-default authentication can read and update the target Sheet.
- a complete read-only production run succeeds.

The runner performs its preflight and validations before honoring `--write-sheet`; a separate collection run is not required. The automation must never use the manual service-log execute option.

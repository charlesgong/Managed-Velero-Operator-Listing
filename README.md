# MVO cluster scan

This project will run the temporary local workflow for discovering clusters affected by Managed Velero Operator deprecation, validating the results, updating the approved spreadsheet, and preparing a cluster list for a service-log dry run.

## Safety boundaries

- Credentials are never stored in this repository. OCM, Google, and GitHub authentication must come from local authenticated tools or the operating-system credential store.
- Every run must verify that OCM is authenticated against the production API before reading cluster data.
- Generated cluster IDs, mappings, reports, logs, and scheduler state remain local and are ignored by Git.
- Spreadsheet changes occur only after all collection checks pass and are verified by reading the updated range back.
- The scheduled workflow may prepare and validate service-log input, but it must not run `osdctl servicelog post --yes`.

## Status

The Codex daily automation exists in a paused state. It should be activated only after the workflow is implemented, tested with fixtures, and successfully exercised as a dry run.

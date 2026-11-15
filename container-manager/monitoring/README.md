# RAG dashboards

Four Git-managed dashboards are provisioned into the **RAG** folder using the
existing `rag-loki` datasource. Compose mounts `dashboards.yaml` and `dashboards/`.
Start/recreate Grafana using the existing monitoring profile; keep its data volume.
Stable UIDs prevent duplicates. File changes reload every 30 seconds; UI edits
are disabled.

From the repository root (Python 3.12+, stdlib only):

```text
python container-manager/monitoring/generate_dashboards.py
python container-manager/monitoring/verify.py
```

Commit the generator and generated JSON together. `verify.py` uses Docker to run
isolated Loki 3.5.8/Grafana 12.1.1 containers without persistent volumes or `.env`.
It checks real provisioning/restart, LogQL execution, empty results, control
calculations and links. Its control traffic never enters the project's Loki.

`--app-events` additionally executes an existing Core test fixture and replays
its actual handler logs (requires application dependencies; storage/models are
mocked). `--hold` keeps the private stack available for a UI inspection and prints
its URL and the temporary marker whose removal releases cleanup.

See [the monitoring guide](../../docs/documents/faq/monitoring.md) for event
sources, counting rules, request/delivery correlation and unavailable metrics.
Alloy and Loki configuration remain unchanged; high-cardinality IDs stay in JSON.

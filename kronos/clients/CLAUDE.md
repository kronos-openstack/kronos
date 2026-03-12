# Clients Module

## Purpose
Wrappers for external services: Prometheus HTTP API and Nova (OpenStack).

## Prometheus Client (`prometheus.py`)
- Uses `requests` directly (NOT prometheus-api-client library — poorly maintained)
- Endpoints: `/api/v1/query` (instant), `/api/v1/query_range` (range), `/api/v1/status/runtimeinfo` (health)
- Health model — `PrometheusHealth` enum:
  - `HEALTHY`: reachable, data fresh and complete
  - `STALE`: reachable but sample timestamps exceed staleness_threshold
  - `PARTIAL`: reachable but missing expected series (some hosts not reporting)
  - `UNREACHABLE`: connection failed
- `instant_query(query, label_key, expected_labels)` → `QueryResult`
  - `label_key`: which Prometheus label to use as dict key (default: "host")
  - `expected_labels`: set of expected values (e.g., Nova hostnames) for partial-data detection
- Retries: `tenacity` with exponential backoff
- Auth: bearer token (string or file), optional CA cert
- Config source: oslo.config `[prometheus]` group

## Nova Client (`nova.py`)
- Uses `openstacksdk` with a `keystoneauth1` session loaded from oslo.config
- Auth configured via `[nova]` config group: auth_type, auth_url, username, password, etc.
- Uses `ks_loading.load_auth_from_conf_options()` + `load_session_from_conf_options()`
- Returns dataclasses, not raw openstacksdk objects
- Key types: `ComputeHost`, `Instance`, `HostAggregate`
- Read-only in M1 — live-migrate calls added in M3

## Testing
- Prometheus: `responses` library to mock HTTP at transport level
- Nova: `unittest.mock.patch` on `openstack.connect()`
- Fixtures in `tests/fixtures/prometheus_responses/`

## Logging
Use oslo.log, never stdlib logging:
```python
from oslo_log import log as logging
LOG = logging.getLogger(__name__)
```

## Design Rules
- Clients NEVER make decisions — they return data, the engine decides
- All exceptions inherit from KronosException (see common/exceptions.py)
- Exceptions use the msg_fmt pattern with %(placeholder)s formatting
- Retry logic lives in the client, not the caller
- No caching in M1 (revisit if Nova API becomes a bottleneck)

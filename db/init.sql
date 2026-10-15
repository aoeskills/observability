-- Run by the db-init service on every start, in both modes (local container or external Postgres).
-- Idempotent: creates what is missing and never overwrites rows. One Postgres server, one database per user:
--   openobserve  OpenObserve metadata (stream schemas, file list = which parquet files are in the bucket, users, dashboards)
--   grafana      Grafana's own database (users, alert state, provisioned dashboards and rules)
--   monitoring   usage thresholds, edited from the Fleet Overview cards (table usage_threshold), and the machine and
--                service inventories (tables machine, service) that tell one that stopped sending from one that never existed
-- Creating a database needs the CREATEDB privilege; on a server where it is missing, create the three databases by hand.
SELECT 'CREATE DATABASE openobserve' WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'openobserve')\gexec
SELECT 'CREATE DATABASE grafana' WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'grafana')\gexec
SELECT 'CREATE DATABASE monitoring' WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'monitoring')\gexec

\connect monitoring

-- Row instance=* holds the default used by any machine that has no row of its own.
CREATE TABLE IF NOT EXISTS usage_threshold (
    instance   text        NOT NULL,
    resource   text        NOT NULL CHECK (resource IN ('cpu', 'memory', 'disk')),
    warn       integer     NOT NULL CHECK (warn BETWEEN 0 AND 100),
    crit       integer     NOT NULL CHECK (crit BETWEEN 0 AND 100),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (instance, resource),
    CHECK (warn < crit)
);

INSERT INTO usage_threshold (instance, resource, warn, crit) VALUES
    ('*', 'cpu',    75, 85),
    ('*', 'memory', 75, 85),
    ('*', 'disk',   75, 85)
ON CONFLICT (instance, resource) DO NOTHING;

-- Every machine that ever sent its status (agents push, so a stopped machine simply goes quiet). The provisioner adds a
-- machine the first time it sees it and refreshes last_seen; a registered machine without data is DOWN (alert + red card)
-- until someone retires it with the button on its Fleet Overview card (which deletes the row and its own thresholds).
CREATE TABLE IF NOT EXISTS machine (
    instance          text        PRIMARY KEY,  -- host.name (HOST_NAME in the machine's agent/.env)
    service_namespace text        NOT NULL DEFAULT '',
    first_seen        timestamptz NOT NULL DEFAULT now(),
    last_seen         timestamptz NOT NULL DEFAULT now()
);

-- Every service ever probed (agent/probes/*.yml on its machine; the agent pushes the results like everything else), the same
-- way: the provisioner adds a service the first time it sees a probe result and refreshes last_seen; a registered service
-- whose probes fail or stop arriving is DOWN (alert + red card) until someone retires it with the button on its Fleet
-- Overview card. Separate from the machine: retiring a machine leaves its services, each is retired on its own card.
CREATE TABLE IF NOT EXISTS service (
    instance          text        NOT NULL,     -- the machine whose agent probes it (host.name)
    service           text        NOT NULL,     -- label `service` of the probe target
    service_namespace text        NOT NULL DEFAULT '',
    first_seen        timestamptz NOT NULL DEFAULT now(),
    last_seen         timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (instance, service)
);

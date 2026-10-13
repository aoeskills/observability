# 服務關係圖

兩種角色的主機：
- **被監控的機器**（API VM，這台 Mac 也算一台）：每台跑一個 agent。app 的 traces / logs 和 node_exporter 的機器狀態都只送到本機的 agent，由 agent 統一推到 observer。
- **observer host**：唯一入口是 gateway（:4317，OTLP gRPC + mTLS，只接受我們 CA 簽發的 client 憑證）。新增機器不用改 observer。

local 和 prod 是同一套設計、同類型的服務，只差在 Bucket 和 Postgres 由誰提供（local：RustFS / Postgres 容器；prod：GCS / Cloud SQL 等），由 env 決定（`.local.env` / `.prod.env`），分別用 `docker-compose.local.yml` / `docker-compose.prod.yml` 啟動。

```mermaid
flowchart LR
    subgraph machine["被監控的機器（每台一份，例如 API VM）"]
        app["API 服務（如 sclaser-food）<br/>OpenTelemetry SDK · service.name + service.namespace"]
        node["node_exporter<br/>只聽本機"]
        agent["otel-agent<br/>偵測機器名稱 host.name、加上 service.namespace · 磁碟佇列（gateway 斷線時暫存、重送）"]
        cert[/"client 憑證 + CA<br/>scripts/pki.sh client 機器名"/]
        app -->|"OTLP traces / logs<br/>localhost:4318"| agent
        agent -->|"scrape 機器狀態"| node
        cert -.- agent
    end

    subgraph observer["observer host"]
        gateway["otel-collector（gateway）<br/>:4317 · 擋大小 / 欄位數"]
        gwcert[/"gateway 憑證 + CA<br/>scripts/pki.sh gateway IP"/]
        gwcert -.- gateway

        blackbox["blackbox-exporter"]
        pgexp["postgres-exporter"]
        gateway -->|"scrape"| blackbox
        gateway -->|"scrape"| pgexp

        o2["OpenObserve<br/>儲存 + logs / traces 查詢"]
        gateway -->|"traces / logs → stream ingest<br/>metrics：Prometheus remote write"| o2
        o2 -.->|"pipeline：依 service.namespace<br/>自動建立 / 分流到同名 stream"| o2

        subgraph pg["Postgres（一台，三個 DB）"]
            db_o2[("openobserve<br/>metadata、file_list")]
            db_g[("grafana<br/>使用者、告警狀態")]
            db_m[("monitoring<br/>門檻 usage_threshold · 機器清單 machine")]
        end
        dbinit["db-init<br/>啟動時建 DB / 表"] -.-> pg

        o2 --> db_o2
        pgexp -->|"門檻、機器清單 → metrics"| db_m

        grafana["Grafana<br/>機器 / 服務狀態 · 門檻 · 告警"]
        grafana -->|"PromQL（Prometheus 相容 API）"| o2
        grafana -->|"✎ 讀寫門檻 · Retire 退役機器"| db_m
        grafana --> db_g

        provisioner["provisioner<br/>每 60 秒依現有 stream 產生"]
        provisioner -->|"維持分流 pipeline · 保存天數（retention.json）<br/>同步 API Service 儀表板"| o2
        provisioner -->|"儀表板、告警規則檔"| grafana
        provisioner -->|"登記看到的機器"| db_m
    end

    agent ==>|"OTLP gRPC :4317 · mTLS<br/>metrics + traces + logs"| gateway
    blackbox -->|"每分鐘 HTTP 探測"| app

    bucket[("Bucket（S3 API）<br/>parquet 資料")]
    o2 -->|"WAL → parquet 上傳"| bucket

    grafana -->|"立即告警 + 啟動 / 05:00 / 17:00 彙整"| line["LINE"]

    user(("使用者"))
    caddy["Caddy :80<br/>/grafana · /openobserve"]
    user -->|"SSH tunnel / 本機"| caddy
    caddy --> grafana
    caddy --> o2
    grafana -.->|"連結：同時間範圍的 traces / logs"| o2
```

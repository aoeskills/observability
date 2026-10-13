# Observability（OpenObserve + Grafana）

監控機器狀態（node_exporter）與 API 服務的 logs / traces（OpenTelemetry），用來 debug。
分工：**Grafana** 看機器與服務狀態、改門檻、發 LINE 告警（沿用 GCP 的 Fleet Overview 設計）；**OpenObserve** 存所有資料（含 metrics），並負責 logs / traces 的查詢。
目前只跑在本機，但所有狀態都在 bucket + 一台 Postgres（用 env 指定），可以直接部署到雲端。先前的 Prometheus + Grafana 只當參考，整理在 [docs/takeaways-from-grafana.md](docs/takeaways-from-grafana.md)。

**儀表板和告警規則不在 repo 裡、也不在 UI 上編輯**：`provisioner` 服務每 60 秒依「目前實際存在的東西」產生它們（每個 project 的 stream 一個 OpenObserve 儀表板、Grafana 上對應的連結、stream 的分流規則和保存天數）。新 project 送進資料後會自動出現，stream 被刪除後自動消失；UI 上的修改會被還原。要改內容就改 `provisioner/` 裡的 generator。

服務之間的關係見 [docs/architecture.md](docs/architecture.md)（mermaid 圖）。

| 服務 | 位址（只綁 127.0.0.1） | 作用 |
|---|---|---|
| OpenObserve | http://localhost/openobserve/ | 儲存；logs / traces 查詢（SQL）；每個 project（OpenTelemetry 的 `service.namespace`）一個 stream，自動建立 |
| Grafana | http://localhost/grafana/ | Fleet Overview（每台機器的卡片、✎ 改門檻）→ Node Exporter Details；LINE 告警。metrics 從 OpenObserve 的 Prometheus API 讀 |
| otel-collector（gateway） | 4317（gRPC + mTLS） | 唯一入口：只接受帶我們 CA 簽發憑證的 agent；另外 scrape 門檻，再送進 OpenObserve。純 push：observer 不主動連任何機器或服務。OpenObserve 帳密只放在這裡 |
| otel-agent（每台機器一個，`agent/`） | 127.0.0.1:4318 | 收本機 app 的 OTLP、在本機 scrape node_exporter、用本機的 blackbox-exporter 探測本機服務（`agent/probes/*.yml`），全部加上機器名稱，用 client 憑證推給 gateway；gateway 斷線時資料先存在磁碟佇列 |
| provisioner | – | 維持 OpenObserve 的分流 pipeline、套用各 project 的保存天數（`retention.json`）、產生 Grafana 儀表板與告警規則、每個 stream 的 OpenObserve 儀表板 |
| postgres-exporter | – | 把門檻和機器清單變成 metrics（給告警、Fleet Overview 用） |
| db-init | – | 每次啟動建立缺少的 DB 和門檻表，跑完就結束 |
| bucket（只有 local） | http://localhost:9001 | RustFS（S3 API），可在 console 看 bucket 裡的檔案，帳密見 `.local.env` |
| postgres（只有 local） | – | 唯一的 Postgres，裡面三個 DB |
| node_exporter | 127.0.0.1:9100 | Mac 本機指標（Homebrew 服務，不在 Docker 裡，否則量到的是 Colima 的 VM） |

## 啟動

```bash
cp .env.example .env   # 填 ZO_ROOT_USER_PASSWORD（至少 8 字元，含大小寫、數字、特殊符號）、GRAFANA_ADMIN_PASSWORD
```

```bash
cp agent/.env.example agent/.env   # 這台 Mac 的 agent：填 HOST_NAME（這台 Mac 要設）、SERVICE_NAMESPACE、GATEWAY_ENDPOINT、AGENT_CERTS_DIR（值見下方）
```

```bash
./scripts/setup-node-exporter-macos.sh
```

憑證（自建 CA，只需第一次；放在 `pki/`，不進 git）：

```bash
scripts/pki.sh init && scripts/pki.sh gateway host.docker.internal localhost 127.0.0.1 && scripts/pki.sh client local-mac
```

這台 Mac 自己的 agent 設定在 `agent/.env`（`HOST_NAME=local-mac`、`SERVICE_NAMESPACE=local`、`GATEWAY_ENDPOINT=host.docker.internal:4317`、`AGENT_CERTS_DIR=../pki/clients/local-mac`）。

**local**：observer 全套 + 它的 bucket（RustFS）和 Postgres 容器 + 這台 Mac 的 agent，一行啟動、一行關閉，不需要網路：

```bash
docker compose -f docker-compose.local.yml up -d
```

```bash
docker compose -f docker-compose.local.yml down
```

`down` 會保留所有資料（volume）；加 `-v` 才會連資料一起刪。其他指令也一樣帶 `-f`，例如 `docker compose -f docker-compose.local.yml logs -f openobserve`。

**prod**（observer host）：連外部的 bucket + Postgres，先把 `.prod.env.example` 複製成 `.prod.env` 填好：

```bash
docker compose -f docker-compose.prod.yml up -d
```

被監控的遠端機器只跑 `agent/`（見下面「加一台機器」）。`prod` 也可以用來讓本機連共用的測試 bucket（用 `O2_S3_PREFIX` 分開各自的資料）。
資料怎麼從 WAL 進到 bucket、查詢怎麼合併兩邊，見 AGENTS.md 的 Storage。

網頁都經過 Caddy（只聽 127.0.0.1:80），用路徑區分：Grafana 在 `/grafana/`、OpenObserve 在 `/openobserve/`。部署在 observer host 時用 `ssh -N -L 8080:127.0.0.1:80 <host>`（→ http://localhost:8080/grafana/）或 Tailscale 連線；連結都是相對路徑，不用設定網址。

登入 http://localhost/openobserve/ ，帳密是 `.env` 的 `ZO_ROOT_USER_EMAIL` / `ZO_ROOT_USER_PASSWORD`。

- 追蹤 (Traces) / 日誌 (Logs)：先選 project 的 stream（例如 `sclaser_food`）；`enduser_id` = 使用者 DB id、`user_name` = 手機號碼；錯誤日誌有 `trace_id` 可以跳到 trace
- 儀表板 → **API Service 服務 · <stream>**（路由、延遲、使用者、錯誤日誌），每個 stream 一個，自動產生

Grafana：http://localhost/grafana/ ，帳密是 `.env` 的 `GRAFANA_ADMIN_USER` / `GRAFANA_ADMIN_PASSWORD`。
- **Fleet Overview**：每台機器 Status / CPU / Memory / Disk 卡片（依門檻變色、條上有警告線），點卡片進 **Node Exporter Details**；卡片上 ✎ 改該機器門檻，右上 ⚙ 改預設門檻
- 右上角連結 **OpenObserve Traces / Logs · <stream>**：開啟「目前時間範圍」的 traces / logs。看到用量尖峰時先在圖上拖曳放大，再點連結

### 送資料的 app（sclaser-food）

sclaser-food 預設把 OTLP 送到 `http://localhost:4318`。`npm run dev`、`npm start` 和 Docker image 都會自動先載入 OpenTelemetry（`.otel/preload.cjs`），開發和部署都不用另外設定。用 docker compose 跑的 `web` 服務會送到 `host.docker.internal:4318`。

sclaser-food 的 session cookie 是加密的 `{sessionToken, userId, username}`（sclaser-food `docs/adr/0008`）；同一個 request 的每個 span 和 log 都有 `enduser_id`（DB id）和 `user_name`（手機號碼），可以直接用來查資料庫。
細節在 sclaser-food 的 `src/otel-preload.ts`、`src/instrumentation.ts`、`src/lib/observability/`。

**不會被塞爆**：每個服務自己決定 request 的限制；這個 stack 只在 collector 保護自己（單次上傳 4 MiB、每筆最多 128 個屬性、單一值 32 KiB、記憶體上限），細節見 AGENTS.md 的 Ingest limits。

## 告警（LINE，Grafana）

| 告警 | 條件 |
|---|---|
| 服務沒有回應 | 那台機器的 agent 每分鐘探測一次（`agent/probes/*.yml`），連續 5 次不是 HTTP 200。整台機器停了則只發「機器沒有回應」 |
| 機器沒有回應 | 曾經出現過的機器超過 1 分鐘沒送狀態，持續 4 分鐘（agent、node_exporter 或整台機器停了）。卡片變紅色 DOWN；不再使用的機器在卡片上按 **Retire** 移除 |
| 用量超過警告 / 嚴重門檻 | CPU、記憶體、磁碟 `/` 超過該機器的門檻（持續 2 分鐘） |
| 門檻資料讀不到 | 門檻 DB 或 postgres-exporter 掛了 |
| 彙整 | 服務啟動時一則，之後每天 05:00、17:00（UTC+8）一則；一定會送：「全部正常」或列出問題（沒收到 = 監控本身壞了）。時間可用 `DIGEST_TIMES` 改 |

出事立即送一則、恢復時送一則；問題持續中由定時彙整提醒。設計沿用 GCP（彙整時間改為固定時刻）（`provisioner/grafana/gen_alerts.py`）。

**設定 LINE**：在 `.env` 填 `LINE_CHANNEL_ACCESS_TOKEN`、`LINE_USER_ID`，然後重建 Grafana（只 restart 不會讀到新的值）：

```bash
docker compose -f docker-compose.local.yml up -d grafana
```

**改門檻**：Fleet Overview 卡片上的 ✎（該機器）或右上 ⚙ Default thresholds（預設值）。存在 Postgres 的 `monitoring` DB，告警約 15 秒後就用新的值。

## 修改儀表板 / 告警

改 `provisioner/` 裡的 generator（Grafana：`provisioner/grafana/*.py`；OpenObserve：`provisioner/openobserve_dashboards.py`），然後讓 provisioner 立刻跑一次：

```bash
docker compose -f docker-compose.local.yml restart provisioner
```

儀表板 30 秒內更新；告警規則要再重啟 Grafana（它只在啟動時讀規則）：

```bash
docker compose -f docker-compose.local.yml restart grafana
```

UI 上手動新建的 OpenObserve 儀表板（說明裡沒有 `[managed: ...]` 標記）不會被動到。

## 加一台機器

全部在新機器上設定，observer 不用改。步驟、確認方式和疑難排解見 **[docs/agent-runbook.md](docs/agent-runbook.md)**，摘要：

1. 在你的電腦簽一張這台機器的憑證：`scripts/pki.sh client <機器名稱>`，連同 `agent/` 的三個檔案複製到新機器。
2. 新機器的 `agent/.env` 填 `SERVICE_NAMESPACE`（屬於哪個 project）、`GATEWAY_ENDPOINT=<observer IP>:4317`；app 跑在 Docker 的話設 `AGENT_OTLP_BIND=<docker0 IP>:4318`。
3. `docker compose up -d`：起 agent 和 node_exporter（只聽 `127.0.0.1:9100`）。
4. Fleet Overview 會自動多出這台的卡片、套用預設門檻；告警也自動涵蓋。卡片名稱是 agent 自動偵測的機器名稱（GCP 上就是 VM 名稱，在 GCP 改名後一分鐘內跟著改）。

新機器只需要能連出去到 observer 的 4317；observer 的 4317 對任何 IP 開放（`.env` 設 `OTEL_GATEWAY_BIND=0.0.0.0`），沒有憑證的連線會被拒絕。

## 加一個 API 服務

1. 讓服務把 OTLP traces / logs 送到本機的 agent（`http://localhost:4318`，`OTEL_EXPORTER_OTLP_ENDPOINT` 的預設值），設 `service.name`（服務）和 `service.namespace`（project，例如 `sclaser_food`）。沒設 namespace 的話用該機器 agent 的 `SERVICE_NAMESPACE`。
2. 不用改 observer：OpenObserve 收到新的 namespace 就自動建立同名 stream，儀表板和 Grafana 連結隨後出現。保存天數不是預設 30 天的話，在 `retention.json` 加一行（例如 `"sclaser_food": 90`）。
3. 服務探測：在服務所在機器的 `agent/probes/` 放一個 `.yml`（照 `example.yml.sample`），一分鐘內 Fleet Overview 的 Services 出現它的卡片，告警自動涵蓋。observer 不用改。

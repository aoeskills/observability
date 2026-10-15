# Observability（OpenObserve + Grafana）

監控機器狀態（node_exporter）、服務是否活著（HTTP 探測）與 API 服務的 logs / traces（OpenTelemetry），用來 debug。
分工：**Grafana** 看機器與服務狀態、改門檻、發 LINE 告警；**OpenObserve** 存所有資料（含 metrics），並負責 logs / traces 的查詢。
所有狀態都在一個 S3 相容 bucket + 一個 SQL 資料庫（目前用 Postgres），用 env 指定；容器裡不放狀態。

## 角色與用詞

系統裡的主機只有兩種角色，文件一律用這兩個詞：

| 角色 | 是什麼 | 跑什麼 | 幾台 |
|---|---|---|---|
| **collector**（收集端） | 收資料、存資料、看資料、發告警的那一台 | 整套服務：gateway（`otel-collector`，唯一入口 :4317）、OpenObserve、Grafana、provisioner、網頁入口…（`docker-compose.prod.yml`） | 一台 |
| **agent**（被收集端） | 被監控的機器 | 只跑 `agent/`：`otel-agent`（收本機 app 的 traces / logs、scrape 本機 node_exporter、探測本機服務）、`blackbox-exporter`、Linux 上的 `node-exporter` | 任意台 |

- 資料只有一個方向：**agent 主動推給 collector**（OTLP gRPC + mTLS，用自建 CA 簽的憑證）。collector 從不主動連 agent（純 push），所以加一台 agent 不用改 collector。
- **簽憑證是 collector 的職責**：CA 放在 collector 的 `pki/`（不進 git），gateway 和每台 agent 的憑證都由它簽。agent 的私鑰在 agent 上產生、不離開那台機器，交給 collector 的只有 CSR。
- agent 上**不 clone 這個 repo**：只放 agent runbook 列出的幾個檔案。操作者的電腦上有 repo（文件 + 要複製的檔案），從那裡 ssh 到 collector 和 agent。
- 一台機器可以同時是兩種角色：collector 通常也是自己的 agent；**local 模式**（開發用）則是一台機器同時跑 collector 全套、自帶的 bucket 和 Postgres，以及自己的 agent。
- 名稱對照：「collector」「agent」指**角色**（某台主機）；`otel-collector`、`otel-agent` 指**容器 / 服務**；**gateway** 指 collector 上的 `otel-collector` 服務（agent 唯一連的入口）。兩個容器其實都是 OpenTelemetry Collector 程式，只是設定不同。
- **project** = OpenTelemetry 的 `service.namespace`：資料依它分到 OpenObserve 的 stream、Grafana 依它篩選。

服務之間的關係見 [docs/architecture.md](docs/architecture.md)（mermaid 圖）。各角色的操作手冊：
- collector：架設、更新、重開、備份、疑難排解 → **[docs/collector-runbook.md](docs/collector-runbook.md)**
- agent：加一台機器、簽憑證、服務探測、退役 → **[docs/agent-runbook.md](docs/agent-runbook.md)**
- 除役：把一台機器上的這套 stack（服務、資料、套件、設定、測試痕跡）清乾淨 → **[docs/decommission-runbook.md](docs/decommission-runbook.md)**

這些文件只寫通用做法。自己部署的具體資訊（主機、IP、雲端資源、現有 agent）建議記在 repo 根目錄的 `SITE.md`，它已被 gitignore。

**儀表板和告警規則不在 repo 裡、也不在 UI 上編輯**：`provisioner` 服務每 60 秒依「目前實際存在的東西」產生它們（每個 project 的 stream 一個 OpenObserve 儀表板、Grafana 上對應的連結、stream 的分流規則和保存天數）。新 project 送進資料後會自動出現，stream 被刪除後自動消失；UI 上的修改會被還原。要改內容就改 `provisioner/` 裡的 generator。

### collector 上的服務

全部只綁 127.0.0.1，唯一對外的是 gateway 的 4317。

| 服務 | 作用 |
|---|---|
| OpenObserve | 儲存；logs / traces 查詢（SQL）；每個 project 一個 stream，自動建立 |
| Grafana | Fleet Overview（每台 agent 的卡片、✎ 改門檻、服務卡片）→ Node Exporter Details；LINE 告警。metrics 從 OpenObserve 的 Prometheus API 讀 |
| otel-collector（gateway） | 唯一入口（:4317，gRPC + mTLS）：只接受帶自家 CA 簽發憑證的 agent；另外 scrape 門檻，再送進 OpenObserve。OpenObserve 帳密只放在這裡 |
| caddy | 網頁入口，依路徑分到 Grafana / OpenObserve |
| provisioner | 維持 OpenObserve 的分流 pipeline、套用各 project 的保存天數（`retention.json`）、產生 Grafana 儀表板與告警規則、每個 stream 的 OpenObserve 儀表板、登記看到的機器和服務 |
| postgres-exporter | 把門檻和機器 / 服務清單變成 metrics（給告警、Fleet Overview 用） |
| db-init | 每次啟動建立缺少的 DB 和表，跑完就結束 |
| bucket、postgres（只有 local 模式） | RustFS（S3 API）和 Postgres 容器，取代外部的 bucket 和資料庫 |

### agent 上的服務（`agent/`）

| 服務 | 作用 |
|---|---|
| otel-agent | 收本機 app 的 OTLP（:4318）、scrape 本機 node_exporter、透過 blackbox-exporter 探測本機服務（`agent/probes/*.yml`），全部加上機器名稱，用 client 憑證推給 gateway；gateway 斷線時資料先存在磁碟佇列（最多重試 1 小時） |
| blackbox-exporter | 服務探測，只有 HTTP 200 算 UP |
| node-exporter | 機器指標。Linux 由 `agent/` 一起跑；macOS 用 Homebrew 服務（`scripts/setup-node-exporter-macos.sh`，在 Docker 裡量到的會是 Docker 的 VM） |

## 網頁

collector 的網頁入口只聽 127.0.0.1，用路徑區分：Grafana 在 `<網址>/grafana/`、OpenObserve 在 `<網址>/openobserve/`。連結都是相對路徑，不用設定網址，所以從哪個入口連進來都可以（SSH tunnel、VPN、自己的反向代理）。例如：

```bash
ssh -N -L 8080:127.0.0.1:80 <collector>
```

→ http://localhost:8080/grafana/ 、http://localhost:8080/openobserve/ 。local 模式是 http://localhost/grafana/ 。

OpenObserve 帳密是 collector `.env` 的 `ZO_ROOT_USER_EMAIL` / `ZO_ROOT_USER_PASSWORD`：
- 追蹤 (Traces) / 日誌 (Logs)：先選 project 的 stream；錯誤日誌有 `trace_id` 可以跳到 trace
- 儀表板 → **API Service 服務 · <stream>**，每個 stream 一個，自動產生

Grafana 帳密是 collector `.env` 的 `GRAFANA_ADMIN_USER` / `GRAFANA_ADMIN_PASSWORD`：
- **Fleet Overview**：每台 agent 的 Status / CPU / Memory / Disk 卡片（依門檻變色、條上有警告線），點卡片進 **Node Exporter Details**；卡片上 ✎ 改該機器門檻，右上 ⚙ 改預設門檻；下方 **Services** 是各 agent 探測的服務
- 右上角連結 **OpenObserve Traces / Logs · <stream>**：開啟「目前時間範圍」的 traces / logs。看到用量尖峰時先在圖上拖曳放大，再點連結

## 啟動

**collector（prod）**：連外部的 bucket + 資料庫。第一次架設、填哪些變數、gateway 憑證、對外開放，見 [docs/collector-runbook.md](docs/collector-runbook.md)。架好之後：

```bash
docker compose -f docker-compose.prod.yml up -d
```

**agent**：只跑 `agent/`，見 [docs/agent-runbook.md](docs/agent-runbook.md)。

**local 模式**（開發用：一台機器同時當 collector 和 agent，自帶 bucket（RustFS）和 Postgres 容器，不需要外部資源）：

```bash
docker compose -f docker-compose.local.yml up -d
```

```bash
docker compose -f docker-compose.local.yml down
```

- 需要 `.env`（`cp .env.example .env`，填 `ZO_ROOT_USER_PASSWORD`（8 字元以上，含大小寫、數字、特殊符號）、`GRAFANA_ADMIN_PASSWORD`）和 `agent/.env`。
- local 模式的 collector 有自己的 CA（和 prod 的 CA 無關），私鑰和憑證都在本機產生：`scripts/pki.sh init`、`scripts/pki.sh gateway host.docker.internal`、`scripts/pki.sh client <機器名稱>`；`agent/.env` 設 `GATEWAY_ENDPOINT=host.docker.internal:4317`、`AGENT_CERTS_DIR=../pki/clients/<機器名稱>`。
- 它會一起啟動本機的 agent，所以先停掉單獨跑的 agent（`cd agent && docker compose down`）。
- `down` 會保留所有資料（volume）；加 `-v` 才會連資料一起刪。其他指令也一樣帶 `-f`，例如 `docker compose -f docker-compose.local.yml logs -f openobserve`。

資料怎麼從 WAL 進到 bucket、查詢怎麼合併兩邊，見 AGENTS.md 的 Storage。

**不會被塞爆**：每個服務自己決定 request 的限制；這個 stack 只在 gateway 保護自己（單次上傳 4 MiB、每筆最多 128 個屬性、單一值 32 KiB、記憶體上限），細節見 AGENTS.md 的 Ingest limits。

## 告警（LINE，Grafana）

| 告警 | 條件 |
|---|---|
| 服務沒有回應 | 曾經出現過的服務（那台 agent 每分鐘探測一次，`agent/probes/*.yml`）連續 5 分鐘不是 HTTP 200，或沒收到探測結果（agent 或整台機器停了：這時「機器沒有回應」和它上面每個服務的「服務沒有回應」都會發）。卡片變紅色 DOWN；不再探測的服務在它的「no data」卡片上按 **Retire** 移除 |
| 機器沒有回應 | 曾經出現過的 agent 超過 1 分鐘沒送狀態，持續 4 分鐘（otel-agent、node_exporter 或整台機器停了）。卡片變紅色 DOWN；不再使用的機器在卡片上按 **Retire** 移除 |
| 用量超過嚴重門檻 | CPU、記憶體、磁碟 `/` 超過該機器的嚴重門檻（持續 2 分鐘）。超過警告門檻不即時通知，只列在彙整裡 |
| Grafana 讀不到用量警告門檻資料 | 門檻 DB 或 postgres-exporter 掛了，用量告警暫時不會觸發 |
| 彙整 | provisioner 啟動約 3 分鐘後一則，之後每天固定時刻（預設 05:00、17:00，Asia/Taipei，用 `DIGEST_TIMES` 改）；一定會送（沒收到 = 監控本身壞了；算不出來時送「12 小時彙整產生失敗」）。內容：目前有問題的項目（或「全部正常」），以及過去 12 小時的【機器】【服務】停機分鐘與可用率、【用量超標】（超過警告 / 嚴重門檻幾分鐘、最高值）；沒有的段落不顯示 |

出事立即送一則、恢復時送一則（告警只寫確認得到的事實，例如多久沒回應，不猜原因）；問題持續中、或不到 5 分鐘的短暫中斷，由定時彙整提醒。LINE 免費方案每月 200 則，彙整本身每月約 60 則；新增會常常觸發的規則前先想配額。規則在 `provisioner/grafana/gen_alerts.py`（Grafana 發），彙整在 `provisioner/digest.py`（provisioner 發）。

**設定 LINE**：在 collector 的 `.env` 填 `LINE_CHANNEL_ACCESS_TOKEN`、`LINE_USER_ID`，然後重建 Grafana 和 provisioner（只 restart 不會讀到新的值）。收不到時怎麼查，見 collector runbook。

```bash
docker compose -f docker-compose.prod.yml up -d grafana provisioner
```

**改門檻**：Fleet Overview 卡片上的 ✎（該機器）或右上 ⚙ Default thresholds（預設值）。存在資料庫的 `monitoring` DB，告警約 15 秒後就用新的值。

## 修改儀表板 / 告警

改 `provisioner/` 裡的 generator（Grafana：`provisioner/grafana/*.py`；OpenObserve：`provisioner/openobserve_dashboards.py`），push 之後在 collector 上 `git pull`，讓 provisioner 立刻跑一次：

```bash
docker compose -f docker-compose.prod.yml restart provisioner
```

儀表板 30 秒內更新；告警規則要再重啟 Grafana（它只在啟動時讀規則）：

```bash
docker compose -f docker-compose.prod.yml restart grafana
```

UI 上手動新建的 OpenObserve 儀表板（說明裡沒有 `[managed: ...]` 標記）不會被動到。local 模式把指令裡的 `prod` 換成 `local`。

## 加一台 agent

全部在新的 agent 上設定，collector 不用改。步驟、確認方式和疑難排解見 **[docs/agent-runbook.md](docs/agent-runbook.md)**，摘要：

1. 憑證：在新 agent 上產生私鑰和 CSR，CSR 交給 collector 簽（`scripts/pki.sh sign`），只把簽好的憑證送回去。私鑰不離開那台機器。
2. 從操作者電腦上的 repo 把 `agent/` 的幾個檔案複製過去，`agent/.env` 填 `SERVICE_NAMESPACE`（屬於哪個 project）、`GATEWAY_ENDPOINT=<collector>:4317`；app 跑在 Docker 的話設 `AGENT_OTLP_BIND=<docker0 IP>:4318`。要探測的服務寫在 `agent/probes/*.yml`。
3. `docker compose up -d`：起 otel-agent、blackbox-exporter 和 node_exporter（都只聽 127.0.0.1）。
4. Fleet Overview 會自動多出這台的卡片、套用預設門檻；告警也自動涵蓋。卡片名稱是 otel-agent 自動偵測的機器名稱，在雲端改名後一分鐘內跟著改。

新的 agent 只需要能連出去到 collector 的 4317。

## 加一個 API 服務

1. 讓服務把 OTLP traces / logs 送到同一台 agent 的 otel-agent（`http://localhost:4318`，`OTEL_EXPORTER_OTLP_ENDPOINT` 的預設值），設 `service.name`（服務）和 `service.namespace`（project）。沒設 namespace 的話用該 agent 的 `SERVICE_NAMESPACE`。
2. 不用改 collector：OpenObserve 收到新的 namespace 就自動建立同名 stream，儀表板和 Grafana 連結隨後出現。保存天數不是預設 30 天的話，在 `retention.json` 加一行（`"<project>": 90`），push 後在 collector 上 `git pull`。
3. 服務探測：在服務所在 agent 的 `agent/probes/` 放一個 `.yml`（照 `example.yml.sample`），一分鐘內 Fleet Overview 的 Services 出現它的卡片，告警自動涵蓋。collector 不用改。

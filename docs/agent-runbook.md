# Agent runbook：加一台 agent（被收集端）

**agent** 是被監控的機器（角色定義見 [README 的「角色與用詞」](../README.md#角色與用詞)）。每台 agent 只跑 `agent/`：`otel-agent` 收集這台機器的狀態（node_exporter）、這台機器上服務的 HTTP 探測結果（blackbox-exporter）和 app 的 traces / logs，用 mTLS 推到 collector 的 gateway（`:4317`）。**collector 不用做任何修改**，新 agent 的卡片、告警、OpenObserve stream 都會自動出現。架構見 [architecture.md](architecture.md)；collector 那一端見 [collector-runbook.md](collector-runbook.md)。

下面的指令都在**操作者的電腦**上、這個 repo 的根目錄執行（repo 在那裡只用來讀文件和複製檔案），透過 ssh 操作兩台主機：`<agent>` 是新 agent 的 ssh 名稱，`<collector>` 是 collector 的 ssh 名稱。**agent 上不 clone 這個 repo**，只放第 2 步列的幾個檔案。collector 自己也當 agent 時，`<agent>` 和 `<collector>` 是同一台，步驟完全一樣（一樣用 `~/agent`，不用 collector 上 repo 裡的 `agent/`）。

名詞：
- **機器名稱**：這台 agent 在所有資料裡的名字，也就是 OpenTelemetry 的 `host.name`，**由 otel-agent 自動偵測**，每分鐘重新偵測一次（支援的雲端上是執行個體名稱，其他環境是作業系統的 hostname）。它會出現在 Fleet Overview 的卡片、metrics 的 `instance` / `host_name`、traces / logs 的 `host.name`，也是門檻和機器清單的 key。只有偵測不到好名字的機器（例如 macOS 上，偵測到的是容器的 hostname）才在 `agent/.env` 設 `HOST_NAME` 覆蓋。憑證的名稱和它無關。
- **project**（`SERVICE_NAMESPACE`）：這台 agent 屬於哪個專案，也就是 OpenTelemetry 的 `service.namespace`。沒有自己設 namespace 的 app 會用這個值，它也決定資料進 OpenObserve 的哪個 stream。

## 0. 前提

| 在哪裡 | 需要 |
|---|---|
| 新 agent | Linux（或 macOS，見第 4 步）、Docker + Docker Compose；能**連出去**到 collector 的 4317（不需要開任何對內的 port） |
| collector | 已照 [collector-runbook.md](collector-runbook.md) 架好：gateway 對外開放、防火牆允許 4317、gateway 憑證的 SAN 包含 agent 要連的 IP 或名稱（collector 自己用 `127.0.0.1`），以及 CA（`~/observability/pki/`，collector runbook 第 3 步）。**簽憑證是 collector 的職責** |
| 操作者的電腦 | 這個 repo 的 clone；能 ssh 到 collector 和新 agent |

## 1. 憑證：在新 agent 上產生私鑰，由 collector 簽

私鑰在新 agent 上產生、不離開那台機器。collector 只簽 CSR，CSR 和憑證透過 ssh 管線經過操作者的電腦傳遞，不會在那裡留下檔案。

在新 agent 上（`<機器名稱>` 只是憑證上的名字，和偵測到的機器名稱無關）：

```bash
ssh <agent> 'mkdir -p ~/agent/certs ~/agent/probes && cd ~/agent/certs && openssl req -new -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes -keyout client.key -out client.csr -subj "/CN=<機器名稱>" && chmod 600 client.key'
```

CSR 交給 collector 簽（有效 2 年），簽好的憑證直接寫回 agent（collector 的 stderr 會印出憑證的名稱和到期日）：

```bash
ssh <agent> 'cat ~/agent/certs/client.csr' | ssh <collector> 'cd ~/observability && scripts/pki.sh sign' | ssh <agent> 'cat > ~/agent/certs/client.crt'
```

把 CA 憑證送過去，刪掉 CSR，然後驗證（印出 `client.crt: OK`）：

```bash
ssh <collector> 'cat ~/observability/pki/ca.crt' | ssh <agent> 'cd ~/agent/certs && cat > ca.crt && rm client.csr && openssl verify -CAfile ca.crt client.crt'
```

`~/agent/certs/` 最後有 `ca.crt`、`client.crt`、`client.key` 三個檔案。otel-agent 在容器裡以 root 執行，所以私鑰維持 600 即可。

## 2. 把 agent 的檔案放到新 agent 上

只需要 `agent/` 裡的四個檔案和探測範例：

```bash
scp agent/docker-compose.yml agent/config.yaml agent/blackbox.yml agent/.env.example <agent>:~/agent/
```

```bash
scp agent/probes/example.yml.sample <agent>:~/agent/probes/
```

## 3. 設定 `agent/.env`（在新 agent 上）

```bash
cd ~/agent && cp .env.example .env
```

| 變數 | 填什麼 |
|---|---|
| `HOST_NAME` | **通常不用設**。只有自動偵測的名字不能用時才設（例如 macOS） |
| `SERVICE_NAMESPACE` | 這台機器屬於的 project |
| `GATEWAY_ENDPOINT` | `<collector IP 或名稱>:4317`（collector 自己的 agent 用 `127.0.0.1:4317`），必須是 gateway 憑證 SAN 裡的其中一個 |
| `COMPOSE_PROFILES=node-exporter` | 保留：由這份 compose 一起跑 node_exporter。這台 agent 上**已經有** node_exporter 的話刪掉這行，並設 `NODE_EXPORTER_TARGET=<它的位址>`（預設 `127.0.0.1:9100`） |
| `AGENT_OTLP_BIND` | app 送 traces / logs 的位址，**見第 5 步**：app 直接跑在這台 agent 上（不在 Docker 裡）不用設；app 跑在 Docker 設 docker0 的位址（通常 `172.17.0.1:4318`） |

### 服務探測（`agent/probes/*.yml`）

這台機器上要顯示 UP / DOWN、斷線要告警的服務，在 `~/agent/probes/` 放一個 `.yml`（照 `example.yml.sample`，每台機器自己一份，不進 git）。agent 每分鐘透過本機的 blackbox-exporter 打一次，只有 HTTP 200 算 UP；連續 5 次失敗發「服務沒有回應」。探測在機器自己身上做，所以 URL 寫本機連得到的位址（app 跑在 Docker 裡就用它對外的 port，例如 `http://127.0.0.1:3000/health`）。改檔案一分鐘內生效，不用重啟；沒有 `.yml` 就不探測。

```yaml
- targets: ["http://127.0.0.1:3000/health"]
  labels:
    service: my-app                # 卡片標題、告警文字
    # service_namespace: my_project   # 不寫就用 SERVICE_NAMESPACE
```

## 4. 啟動

```bash
cd ~/agent && docker compose up -d
```

會起 `otel-agent`、`blackbox-exporter`（服務探測，只聽 127.0.0.1:9115），加上 `node-exporter`（如果 `COMPOSE_PROFILES` 有設）。都是 `restart: unless-stopped`，機器重開機後會自己起來。

**macOS**：Docker 跑在 VM 裡，host network 是 VM 的、量到的也是 VM，所以：node_exporter 用 Homebrew 服務跑在主機上（從 repo 執行 `scripts/setup-node-exporter-macos.sh`，`.env` 拿掉 `COMPOSE_PROFILES`），agent 多複製 `agent/docker-compose.mac.yml`，用它啟動（app 一樣送 `127.0.0.1:4318`，探測的 URL 寫 `http://host.docker.internal:<port>/...`）：

```bash
cd ~/agent && docker compose -f docker-compose.yml -f docker-compose.mac.yml up -d
```

## 5. 讓 API server 把 traces / logs 送進 agent

agent 不會去 app 那邊抓資料，是 app 的 OpenTelemetry SDK 主動送到 agent 的 4318。每個 app 要設：
- `service.name`：服務名稱；
- `service.namespace`：project。沒設的話用 agent 的 `SERVICE_NAMESPACE`；
- 送往的位址 `OTEL_EXPORTER_OTLP_ENDPOINT`，依 app 跑在哪裡而不同（見下面兩種情況）。

`host.name` 不用在 app 設，agent 會統一換成偵測到的機器名稱。

### 情況 A：app 直接跑在 agent 主機上（不在 Docker 裡）

不用額外設定：agent 預設聽 `127.0.0.1:4318`，也就是 SDK 預設送往的 `http://localhost:4318`。

### 情況 B：app 跑在 Docker 容器裡

容器裡的 `localhost` 是容器自己，連不到主機上的 otel-agent，所以要做兩件事：

1. **otel-agent 改聽 docker0 的位址**：在這台 agent 上查 docker0 的 IP，通常是 `172.17.0.1`：

   ```bash
   ip -4 addr show docker0
   ```

   把 `agent/.env` 設成 `AGENT_OTLP_BIND=172.17.0.1:4318`，然後執行 `docker compose up -d`。這個位址只有本機和容器連得到，外部連不進來。

2. **app 送往 `host.docker.internal:4318`**：app 的 compose 加上：

   ```yaml
   environment:
     OTEL_EXPORTER_OTLP_ENDPOINT: http://host.docker.internal:4318
   extra_hosts:
     - host.docker.internal:host-gateway
   ```

   在 Linux 上，`host-gateway` 會解析成 docker0 的 IP，正好是 agent 聽的位址。

注意：設成 docker0 位址後，這台 agent 上**不在** Docker 裡的程式要改送 `http://172.17.0.1:4318`，`localhost:4318` 就不通了。

**從 app 容器裡直接測試**（Node.js app；回應 `200` 就是通了）：

```bash
docker exec <app 容器名稱> node -e "fetch('http://host.docker.internal:4318/v1/traces',{method:'POST',headers:{'content-type':'application/json'},body:'{}'}).then(r=>console.log(r.status))"
```

如果連不到，先在容器裡確認 `host.docker.internal` 解析出來的是 docker0 的 IP（Docker daemon 設過 `host-gateway-ip` 的話會不同）：

```bash
docker exec <app 容器名稱> getent hosts host.docker.internal
```

agent 聽 docker0 位址時，預設 bridge 和 compose 自建網路裡的容器都能送進來。macOS 不適用（`host-gateway` 指向 Mac 本身）：用第 4 步的 `docker-compose.mac.yml`。

## 6. 確認

在新 agent 上看 otel-agent 最近的 log，不能有 `failed to connect`、`certificate` 之類的錯誤：

```bash
docker compose logs --tail 30 otel-agent
```

node_exporter 有在回應：

```bash
curl -s 127.0.0.1:9100/metrics | head -3
```

服務探測有在運作（`probe_success 1` = UP；URL 換成 `probes/*.yml` 裡的）：

```bash
curl -s '127.0.0.1:9115/probe?module=http_200&target=http://127.0.0.1:3000/health' | grep -E '^probe_(success|http_status_code) '
```

在 collector（Grafana / OpenObserve 網頁）：
- Grafana **Fleet Overview** 約 1 分鐘內出現這台機器的卡片，名稱就是偵測到的機器名稱，套用預設門檻；
- 有設服務探測的話，**Services** 出現該服務的卡片（`instance` = 這台機器，`target` = 探測的 URL），服務從此登記在服務清單裡；
- app 有流量後，OpenObserve **Traces** 選 stream `<SERVICE_NAMESPACE>`，可以看到 `service_host_name = <機器名稱>` 的 trace；第一次看到新的 project 時，它的 stream 和儀表板會自動建立；
- 這個 project 的保存天數如果不是預設 30 天，在 repo 的 `retention.json` 加一行。

## 疑難排解

| 症狀 | 原因與處理 |
|---|---|
| agent log：`x509: certificate is valid for ..., not ...` | `GATEWAY_ENDPOINT` 不在 gateway 憑證的 SAN 裡 → 重簽 gateway 憑證（[collector-runbook.md](collector-runbook.md) 第 3 步），或改用 SAN 裡的名稱 |
| agent log：`connection refused` / timeout | collector 沒開放 4317（`OTEL_GATEWAY_BIND=0.0.0.0`、雲端防火牆），或 IP 打錯 |
| agent log：`context deadline exceeded` / `Unavailable`，但 4317 連得上 | collector 那邊 OpenObserve 沒起來，gateway 寫不進去 → 見 collector runbook 的疑難排解 |
| agent log：`certificate required` / `bad certificate` | `agent/certs` 少檔案，或憑證不是這個 CA 簽的（CA 重建過 → 重簽所有機器） |
| 卡片沒出現，但 agent 沒有錯誤 | node_exporter 沒在跑或位址不對 → 檢查 `curl 127.0.0.1:9100/metrics`、`COMPOSE_PROFILES` / `NODE_EXPORTER_TARGET` |
| Services 沒有卡片 / 一直 DOWN | `probes/*.yml` 的 URL 在這台機器上連不到或不是 200 → 用第 6 步的 curl 測；檔名要是 `.yml`；YAML 格式錯誤時 agent log 會有 `file_sd` 的錯誤。卡片上有 `probe: no data` = 沒收到探測結果（agent、blackbox-exporter 或整台機器停了，或這個服務已從 `probes/*.yml` 移除） |
| 卡片有，但沒有 traces | app 送不到 agent → 照第 5 步的測試指令檢查；也確認 OpenObserve 選的是對的 stream（= namespace） |
| gateway 斷線一段時間 | 正常：agent 把資料暫存在磁碟佇列，最多重試 1 小時，連上後補送 |

## 日常操作

| 要做的事 | 怎麼做 |
|---|---|
| 改機器名稱 | 在雲端改執行個體名稱（或改 hostname），agent 一分鐘內偵測到；有設 `HOST_NAME` 的機器改它後執行 `docker compose up -d`。新名稱的卡片下一次取樣就出現；舊名稱會變成紅色 DOWN 卡片（約 6 分鐘後發「機器沒有回應」），在它上面按 **Retire**。這台機器自己的門檻留在舊名稱下，Retire 會一起刪掉，需要的話在新卡片上用 ✎ 重設 |
| 換 project | 改 `SERVICE_NAMESPACE`，執行 `docker compose up -d` |
| 加 / 移除服務探測 | 改 `~/agent/probes/*.yml`，一分鐘內生效。移除的服務約 2 分鐘後變成紅色 DOWN 卡片（`probe: no data`，約 6 分鐘後發「服務沒有回應」），在它上面按 **Retire**（從服務清單刪除，卡片和告警約 1 分鐘內消失）。改服務名稱（`service` label）= 移除舊的 + 加新的；只改 URL 不用處理 |
| 移除機器 | `docker compose down`（磁碟佇列保留；加 `-v` 連佇列一起刪），然後在 Fleet Overview 它的紅色 DOWN 卡片上按 **Retire**（從機器清單刪除、刪掉它自己的門檻，卡片和告警約 1 分鐘內消失）。它的服務各自在 Services 的 DOWN 卡片上按 **Retire**（機器的 Retire 不會連帶移除服務）。沒按 Retire 就會一直顯示 DOWN 並告警：這就是「機器 / 服務掛了」的偵測方式 |
| 暫時停機維護 | 停機期間機器和它的服務都會顯示 DOWN 並告警，恢復後自動回到 UP。不要按 Retire（按了也沒關係，再送資料時會自動重新登記，只是機器的門檻回到預設） |
| 更新 agent 設定 | 從 repo 重新複製 `config.yaml` / `docker-compose.yml` / `blackbox.yml`，執行 `docker compose up -d` |
| 憑證到期（client 2 年） | 先刪掉 `~/agent/certs/client.key`，第 1 步重做（新私鑰 + CSR，collector 簽），執行 `docker compose restart otel-agent` |
| 撤銷一台 agent | gateway 沒有撤銷清單：在 collector 上重建 CA（先移走舊的 `pki/`，`scripts/pki.sh init`），重簽 gateway 憑證（[collector-runbook.md](collector-runbook.md) 第 3 步）和每台保留的 agent（第 1 步），所有 agent 換上新的 `ca.crt` |

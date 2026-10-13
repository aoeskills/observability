# Collector runbook：架設與維護收集端

**collector** 是收資料、存資料、看資料、發告警的那一台主機（角色定義見 [README 的「角色與用詞」](../README.md#角色與用詞)）。它跑整套服務（`docker-compose.prod.yml`）：gateway（`otel-collector`，agent 唯一連的入口 :4317）、OpenObserve、Grafana、provisioner、Caddy、postgres-exporter。狀態都在外部的 bucket + Postgres，容器裡只有 OpenObserve 尚未上傳的 WAL、Grafana 外掛快取和產生出來的設定檔。

目前的 collector：

| 項目 | 值 |
|---|---|
| 主機 | GCP VM `aoeskills-first-host`（`ssh GCP_aoeskills`；專案 `united-rope-297316`、zone `asia-northeast1-b`、外部 IP `34.146.61.17`；2 vCPU / 1.9 GB RAM / 9.7 GB 磁碟） |
| repo | `~/observability`（`git pull` 用唯讀 deploy key `~/.ssh/observability_deploy`） |
| bucket | GCS `aoeskills_bucket-1`（asia-northeast1），走 S3 XML API + HMAC key |
| Postgres | Cloud SQL（Postgres 18），私有 IP，使用者 `obs_app`，三個 DB：`openobserve`、`grafana`、`monitoring` |
| 防火牆 | 規則 `allow-otel-gateway`：tcp:4317，來源 `0.0.0.0/0`，套用到 network tag `otel-gateway`（agent 的 IP 會變，憑證才是門禁） |
| 網頁 | `ssh -N -L 8080:127.0.0.1:80 GCP_aoeskills` → http://localhost:8080/grafana/ ；Tailscale：http://aoeskills-first-host.tail8656d8.ts.net/grafana/ （VM 上的 `tailscale serve` 把 tailnet 的 :80 轉到 127.0.0.1:80） |
| 用量 | 約 1.4 GB RAM（OpenObserve ~310 MB、Grafana ~345 MB、otel-collector ~75 MB）、磁碟剩約 2.3 GB |

## 0. 前提

| 在哪裡 | 需要 |
|---|---|
| collector 主機 | Linux、Docker + Docker Compose ≥ 2.20（`include`）、`git`；能連到 bucket 和 Postgres |
| GCP | GCS bucket（OpenObserve 不會自己建）+ 有讀寫權限的 service account 的 HMAC key；Cloud SQL Postgres（使用者有 CREATEDB，否則手動建三個 DB）；防火牆開 tcp:4317 |
| 這台 Mac | repo 和 `pki/`（CA 私鑰只在這裡）：簽 gateway 憑證用 |

**不要**在 bucket 設生命週期規則（保存期限由 OpenObserve 自己刪，見 `retention.json`）。

## 1. 取得 repo（deploy key）

collector 上沒有 GitHub 帳號的金鑰，用 repo 專屬的唯讀 deploy key：

```bash
ssh-keygen -t ed25519 -N "" -C "<主機名> observability deploy" -f ~/.ssh/observability_deploy && cat ~/.ssh/observability_deploy.pub
```

把印出的公鑰加到 GitHub repo 的 **Settings → Deploy keys**（不要勾 write access），然後：

```bash
GIT_SSH_COMMAND="ssh -i ~/.ssh/observability_deploy -o IdentitiesOnly=yes" git clone git@github.com:aoeskills/observability.git ~/observability
```

```bash
git -C ~/observability config core.sshCommand "ssh -i ~/.ssh/observability_deploy -o IdentitiesOnly=yes"
```

之後的更新一律是：在這台 Mac 改、push，collector 上 `git pull`。不要在 collector 上直接改 repo 的檔案。

## 2. 填 `.env` 和 `.prod.env`

```bash
cd ~/observability && cp .env.example .env && cp .prod.env.example .prod.env && chmod 600 .env .prod.env
```

| 檔案 | 變數 | 填什麼 |
|---|---|---|
| `.env` | `ZO_ROOT_USER_EMAIL` / `ZO_ROOT_USER_PASSWORD` | OpenObserve 管理帳號（也是 gateway 寫入用的帳密）。密碼規則見下 |
| `.env` | `GRAFANA_ADMIN_USER` / `GRAFANA_ADMIN_PASSWORD` | Grafana 管理帳號 |
| `.env` | `LINE_CHANNEL_ACCESS_TOKEN` / `LINE_USER_ID` | LINE bot 的 channel token 和收訊者 ID |
| `.env` | `OTEL_GATEWAY_BIND=0.0.0.0` | gateway 對外開放（預設只聽 127.0.0.1，agent 連不進來） |
| `.env` | `O2_ORG`、`DASHBOARD_SYNC_INTERVAL`、`O2_RETENTION_DAYS` | 照範例：`default`、`60`、`30` |
| `.prod.env` | `O2_S3_SERVER_URL` / `O2_S3_REGION` / `O2_S3_BUCKET` | `https://storage.googleapis.com`、bucket 的 location（例如 `asia-northeast1`）、bucket 名稱 |
| `.prod.env` | `O2_S3_ACCESS_KEY` / `O2_S3_SECRET_KEY` | HMAC key |
| `.prod.env` | `O2_EXT_AUTH_SALT` | `openssl rand -hex 24`。**設定後永遠不能改**（和 Postgres 裡的 OpenObserve 資料綁在一起） |
| `.prod.env` | `PG_HOST` / `PG_PORT` / `PG_USER` / `PG_PASSWORD` / `PG_SSLMODE` | Cloud SQL 的私有 IP、5432、使用者、密碼、`require` |

**密碼規則**（今天部署時踩到的）：
- Cloud SQL 和 OpenObserve 都要求密碼同時有小寫、大寫、數字、特殊字元，否則 Cloud SQL 拒絕設定、OpenObserve 啟動失敗（`ZO_ROOT_USER_PASSWORD is too weak`）。
- `PG_PASSWORD` 會被放進網址（OpenObserve 的連線字串），特殊字元只能用 `-` `.` `_` `~`，否則 OpenObserve 啟動失敗（`InvalidPort`）。
- `.env` 裡不要有 `$`（docker compose 和 Grafana 都會把它當變數展開）、引號、空白。
- 一個同時符合全部規則的產生方式：

```bash
printf 'Aa1-%s\n' "$(openssl rand -hex 20)"
```

改 Cloud SQL 使用者的密碼（用目前的密碼連進去）：

```sql
ALTER ROLE obs_app WITH PASSWORD 'Aa1-<40 碼 hex>';
```

## 3. gateway 憑證

agent 用 mTLS 連 gateway，gateway 需要一張我們 CA 簽的伺服器憑證，SAN 要包含 agent 連線用的 IP 或名稱。私鑰在 collector 上產生、不離開 collector；這台 Mac 只簽 CSR。

在 collector 上產生私鑰和 CSR：

```bash
cd ~/observability && mkdir -p pki/gateway && chmod 700 pki && openssl req -new -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes -keyout pki/gateway/gateway.key -out pki/gateway/gateway.csr -subj "/CN=otel-gateway" && chmod 600 pki/gateway/gateway.key
```

在這台 Mac 上（repo 根目錄）把 CSR 拿回來、用 CA 簽、把憑證送回去（`SAN` 換成 agent 會連的所有 IP / 名稱；有效 825 天）：

```bash
scp GCP_aoeskills:observability/pki/gateway/gateway.csr "$TMPDIR/gateway.csr" && printf 'subjectAltName=IP:34.146.61.17\nextendedKeyUsage=serverAuth\nkeyUsage=digitalSignature\n' > "$TMPDIR/gateway.ext" && openssl x509 -req -in "$TMPDIR/gateway.csr" -CA pki/ca.crt -CAkey pki/ca.key -CAcreateserial -days 825 -sha256 -extfile "$TMPDIR/gateway.ext" -out "$TMPDIR/gateway.crt" && scp "$TMPDIR/gateway.crt" pki/ca.crt GCP_aoeskills:observability/pki/gateway/
```

回到 collector：刪掉 CSR，私鑰交給 `otel-collector` 容器的使用者（uid 10001，權限維持 600；不做的話 gateway 會因 `open /certs/gateway.key: permission denied` 一直重啟）：

```bash
cd ~/observability && rm pki/gateway/gateway.csr && sudo chown 10001:10001 pki/gateway/gateway.key
```

目前的 gateway 憑證：SAN `IP:34.146.61.17`，2029-01-15 到期。到期前、或 collector 的 IP / 名稱改變時，重做這一步，然後 `docker compose -f docker-compose.prod.yml restart otel-collector`。

## 4. 防火牆（GCP）

```bash
gcloud compute instances add-tags aoeskills-first-host --zone asia-northeast1-b --tags otel-gateway --project united-rope-297316
```

```bash
gcloud compute firewall-rules create allow-otel-gateway --project united-rope-297316 --network default --direction INGRESS --action ALLOW --rules tcp:4317 --target-tags otel-gateway --source-ranges 0.0.0.0/0
```

## 5. 啟動與確認

```bash
cd ~/observability && docker compose -f docker-compose.prod.yml up -d
```

約 1～2 分鐘後全部就緒（db-init 建好 DB → OpenObserve → provisioner 第一輪 → Grafana）：

```bash
docker compose -f docker-compose.prod.yml ps -a
```

- `db-init` 是 `Exited (0)`，其他都是 `Up`，沒有 `Restarting`。
- `docker logs --tail 10 provisioner` 有 `created pipeline route_traces_by_namespace` / `route_logs_by_namespace`，agent 送資料後有 `machine registered <機器名稱>`。
- 網頁打得開（見 README 的「網頁」）；Fleet Overview 出現 agent 的卡片。
- 約 20 分鐘後 bucket 裡出現 `files/default/metrics/...`；traces / logs 也一樣，但資料很少的 stream 會更晚（見下方「停機與重開的代價」）。
- LINE 收到一則「開機彙整」。

## 6. 更新

在這台 Mac 改、push，然後在 collector：

```bash
cd ~/observability && git pull
```

| 改了什麼 | 在 collector 上執行 |
|---|---|
| `otel-collector/config.yaml` | `docker compose -f docker-compose.prod.yml restart otel-collector` |
| `provisioner/` 的 generator（儀表板） | `docker compose -f docker-compose.prod.yml restart provisioner` |
| 告警規則（`gen_alerts.py`） | 先 `restart provisioner`，等它跑完第一輪，再 `restart grafana` |
| `.env` / `.prod.env` / `docker-compose*.yml` | `docker compose -f docker-compose.prod.yml up -d`（重建有變動的容器；只 restart 不會讀新的 env） |
| `provisioner/Dockerfile` | `docker compose -f docker-compose.prod.yml build provisioner && docker compose -f docker-compose.prod.yml up -d` |
| 升級 image（OpenObserve 等） | 先做第 8 步的備份，再 `docker compose -f docker-compose.prod.yml pull && docker compose -f docker-compose.prod.yml up -d`。OpenObserve 的 migration 只能往前，新版建過的 DB 舊版打不開 |

## 7. 停機與重開的代價

`docker compose -f docker-compose.prod.yml down` 再 `up -d`（**不加 `-v`**）：

| 項目 | 會發生什麼 |
|---|---|
| 停機期間 agent 送的資料 | 各 agent 先存在自己的磁碟佇列，每批最多重試 1 小時，重開後帶原本的時間戳補送，不會掉。停超過 1 小時，太舊的批次會被丟掉 |
| 已存的資料 | 已上傳的在 GCS / Cloud SQL；尚未上傳的在 volume `openobserve-wal`，`down` 不刪 volume，重開後接著用 |
| 儀表板、pipeline、門檻、機器清單 | 在 Postgres 或由 provisioner 重新產生，不受影響 |
| 告警 | 停機期間不會有告警；告警狀態在 Postgres，重開後接著評估。DOWN 告警要連續 4 分鐘，agent 補送很快，一般不會誤報 |
| LINE | 重開後送一則開機彙整；已發過的告警不會重發 |
| 網頁 | 停機期間打不開，重開約 1～2 分鐘 |

- **不要 `down -v`**：會刪掉尚未上傳到 GCS 的資料和 Grafana 外掛快取。尚未上傳的資料通常是最近約 20 分鐘；資料很少的 stream 可能更久，因為 WAL 檔過了 10 分鐘後要等「下一筆資料」進來才會封檔（2026-10-13 觀察到 logs 停留 28 分鐘）。
- 整台主機重開機：服務是 `restart: unless-stopped`，Docker 起來後自己跑回來；但先 `down` 過的不會自動起來，要自己 `up -d`。
- 已驗證的範圍：local 模式下 gateway 停 40 秒、期間的資料全部補到；同一組 bucket + Postgres 開全新容器，舊資料都在。collector 實際 `down` / `up` 還沒做過。

## 8. 備份（升級前）

Cloud SQL 是 Postgres 18，`pg_dump` 必須是 18 以上（stack 自帶的 `postgres:17-alpine` 會拒絕）。在 collector 上：

```bash
cd ~/observability && set -a && . ./.prod.env && set +a && mkdir -p backups && for db in openobserve grafana monitoring; do docker run --rm -e PGPASSWORD="$PG_PASSWORD" postgres:18-alpine pg_dump "host=$PG_HOST port=$PG_PORT user=$PG_USER dbname=$db sslmode=$PG_SSLMODE" | gzip > "backups/$db-$(date +%Y%m%d).sql.gz"; done
```

`backups/` 不進 git。已驗證（2026-10-13）：三個 DB 都能匯出。用完可以 `docker rmi postgres:18-alpine` 省磁碟。bucket 裡的資料不用另外備份，但 Postgres 遺失的話 bucket 裡的檔案就沒有索引（資料還在、查不到）。

## 9. 疑難排解

| 症狀 | 原因與處理 |
|---|---|
| `openobserve` 一直 `Restarting`，log：`postgres connect options create failed: Configuration(InvalidPort)` | `PG_PASSWORD` 有網址保留字元 → 照第 2 步的規則換密碼 |
| `openobserve` 一直重啟，log：`ZO_ROOT_USER_PASSWORD is too weak` | root 密碼缺大寫 / 數字 / 特殊字元其中一類 → 改 `.env`，`up -d` |
| `openobserve` 一直重啟，log：`db init failed` | `O2_EXT_AUTH_SALT` 沒填或被改過（必須和第一次啟動時一樣） |
| `otel-collector` 一直重啟，log：`open /certs/gateway.key: permission denied` | 私鑰擁有者不是 uid 10001 → 第 3 步最後的 `chown` |
| agent 的 log：`context deadline exceeded` / `Unavailable`，但 4317 連得上 | gateway 收到了但寫不進 OpenObserve（OpenObserve 沒起來）→ 先看 `docker compose -f docker-compose.prod.yml ps` |
| agent 的 log：timeout，連 4317 都連不上 | 防火牆（第 4 步）或 `OTEL_GATEWAY_BIND` 不是 `0.0.0.0` |
| 收不到 LINE | `docker logs grafana 2>&1 \| grep "Notify for alerts failed"`：`401` = token 無效。不送訊息就能檢查 token：`curl -s -H "Authorization: Bearer <token>" https://api.line.me/v2/bot/info`（200 = 有效）。改 `.env` 後要 `up -d grafana`。測試發送：Grafana 13 的 `POST /grafana/apis/notifications.alerting.grafana.app/v1beta1/namespaces/default/receivers/<base64url(contact point 名稱)>/test`，body 為 `{"integration": <該 contact point 現有的 integration>, "alert": {...}}`（舊的 `/api/alertmanager/.../receivers/test` 已移除） |
| bucket 裡某個 stream 很晚才出現 | 資料很少時 WAL 要等下一筆資料才封檔，屬正常；查詢不受影響（會讀 WAL） |

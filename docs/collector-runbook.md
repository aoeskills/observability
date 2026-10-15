# Collector runbook：架設與維護收集端

**collector** 是收資料、存資料、看資料、發告警的那一台主機（角色定義見 [README 的「角色與用詞」](../README.md#角色與用詞)）。它跑整套服務（`docker-compose.prod.yml`）：gateway（`otel-collector`，agent 唯一連的入口 :4317）、OpenObserve、Grafana、provisioner、網頁入口、postgres-exporter。狀態都在外部的 bucket + 資料庫，容器裡只有 OpenObserve 尚未上傳的 WAL、Grafana 外掛快取和產生出來的設定檔。

下面 `<collector>` 是 collector 的 ssh 名稱，repo 放在 collector 的 `~/observability`。自己部署的值（主機、IP、bucket、資料庫）記在 `SITE.md`（見 README）。

## 0. 前提

| 需要 | 說明 |
|---|---|
| collector 主機 | Linux、Docker + Docker Compose ≥ 2.20（`include`）、`git`；能連到 bucket 和資料庫。參考用量：整套約 1 GB RAM（OpenObserve、Grafana 各數百 MB） |
| bucket | **S3 API 相容**（OpenObserve 用 S3 client 連）：endpoint、region、bucket 名稱、有讀寫權限的 access key / secret key。OpenObserve 不會自己建 bucket |
| 資料庫 | 一台 SQL 資料庫，目前用 **Postgres**：一個使用者，三個 DB（`openobserve`、`grafana`、`monitoring`）。使用者有 CREATEDB 時 db-init 會自己建，否則先手動建好 |
| 網路 | agent 能連進 collector 的 tcp 4317（預設值）。其他服務都只聽 127.0.0.1 |

**不要**在 bucket 設生命週期規則（保存期限由 OpenObserve 自己刪，見 `retention.json`；生命週期規則刪掉的檔案仍會留在 OpenObserve 的檔案清單裡）。

## 1. 取得 repo

collector 上不放個人帳號的金鑰，建議用 repo 專屬的唯讀 deploy key：

```bash
ssh-keygen -t ed25519 -N "" -C "<collector> observability deploy" -f ~/.ssh/observability_deploy && cat ~/.ssh/observability_deploy.pub
```

把印出的公鑰加到 repo 的 deploy keys（唯讀），然後：

```bash
GIT_SSH_COMMAND="ssh -i ~/.ssh/observability_deploy -o IdentitiesOnly=yes" git clone <repo URL> ~/observability
```

```bash
git -C ~/observability config core.sshCommand "ssh -i ~/.ssh/observability_deploy -o IdentitiesOnly=yes"
```

之後的更新一律是：在操作者的電腦上改、push，collector 上 `git pull`。不要在 collector 上直接改 repo 的檔案。

## 2. 填 `.env` 和 `.prod.env`

```bash
cd ~/observability && cp .env.example .env && cp .prod.env.example .prod.env && chmod 600 .env .prod.env
```

| 檔案 | 變數 | 填什麼 |
|---|---|---|
| `.env` | `ZO_ROOT_USER_EMAIL` / `ZO_ROOT_USER_PASSWORD` | OpenObserve 管理帳號（也是 gateway 寫入用的帳密）。密碼規則見下 |
| `.env` | `GRAFANA_ADMIN_USER` / `GRAFANA_ADMIN_PASSWORD` | Grafana 管理帳號 |
| `.env` | `LINE_CHANNEL_ACCESS_TOKEN` / `LINE_USER_ID` | LINE bot 的 channel token 和收訊者 ID |
| `.env` | `OTEL_GATEWAY_BIND=0.0.0.0` | gateway 對外開放（預設只聽 127.0.0.1，其他 agent 連不進來） |
| `.env` | `O2_ORG`、`DASHBOARD_SYNC_INTERVAL`、`O2_RETENTION_DAYS` | 照範例：`default`、`60`、`30` |
| `.prod.env` | `O2_S3_SERVER_URL` / `O2_S3_REGION` / `O2_S3_BUCKET` | bucket 的 S3 endpoint、region、bucket 名稱 |
| `.prod.env` | `O2_S3_ACCESS_KEY` / `O2_S3_SECRET_KEY` | bucket 的 access key / secret key |
| `.prod.env` | `O2_EXT_AUTH_SALT` | `openssl rand -hex 24`。**設定後永遠不能改**（和資料庫裡的 OpenObserve 資料綁在一起） |
| `.prod.env` | `PG_HOST` / `PG_PORT` / `PG_USER` / `PG_PASSWORD` / `PG_SSLMODE` | 資料庫的位址、port、使用者、密碼、`require` |

**密碼規則**：
- OpenObserve 要求 root 密碼 8～128 字元，同時有小寫、大寫、數字、特殊字元，否則啟動失敗（`ZO_ROOT_USER_PASSWORD is too weak`）。很多雲端資料庫也有同樣的要求。
- `PG_PASSWORD` 會被放進網址（OpenObserve 的連線字串），特殊字元只能用 `-` `.` `_` `~`，否則 OpenObserve 啟動失敗（`InvalidPort`）。
- `.env` 裡不要有 `$`（docker compose 和 Grafana 都會把它當變數展開）、引號、空白。
- 一個同時符合全部規則的產生方式：

```bash
printf 'Aa1-%s\n' "$(openssl rand -hex 20)"
```

## 3. CA 和 gateway 憑證

agent 用 mTLS 連 gateway：agent 出示自家 CA 簽的 client 憑證，gateway 出示同一個 CA 簽的伺服器憑證。**簽憑證是 collector 的職責**：CA 放在 collector 的 `~/observability/pki/`（不進 git），gateway 的憑證在這裡產生，每台 agent 的憑證也在這裡簽（[agent-runbook.md](agent-runbook.md) 第 1 步，`scripts/pki.sh sign`）。

建 CA（只做一次，有效 10 年；`pki/` 已經有 `ca.key` 時會拒絕）：

```bash
cd ~/observability && scripts/pki.sh init
```

gateway 的私鑰和憑證（有效 825 天）：參數列出 agent 連線用的所有 IP / 名稱（SAN），通常是 collector 對外的 IP 或名稱，加上 collector 自己的 agent 用的 `127.0.0.1`。然後把私鑰交給 `otel-collector` 容器的使用者（uid 10001，權限維持 600；不做的話 gateway 會因 `open /certs/gateway.key: permission denied` 一直重啟），再重啟 gateway：

```bash
cd ~/observability && scripts/pki.sh gateway <collector IP 或名稱> 127.0.0.1 && sudo chown 10001:10001 pki/gateway/gateway.key && docker compose -f docker-compose.prod.yml restart otel-collector
```

到期前、或 collector 的 IP / 名稱改變時，重做 gateway 這一行（CA 不用重建，agent 不受影響）。

**`pki/ca.key` 要備份**（第 8 步）。遺失的話就沒辦法再替 agent 簽憑證，只能重建 CA（移走 `pki/`，`init` + `gateway`），然後每台 agent 都重做 agent runbook 第 1 步。

## 4. 對外開放 4317

在主機和雲端的防火牆允許 tcp 4317 連進 collector。來源可以是任意 IP：沒有自家 CA 簽的 client 憑證的連線會被 gateway 拒絕；agent 的 IP 固定的話也可以只開給它們。

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
- 啟動約 3 分鐘後，LINE 收到一則「監控啟動」彙整。

collector 通常也當自己的 agent：照 [agent-runbook.md](agent-runbook.md) 做，`<agent>` 和 `<collector>` 是同一台，`GATEWAY_ENDPOINT=127.0.0.1:4317`。

## 6. 更新

在操作者的電腦上改、push，然後在 collector：

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
| 已存的資料 | 已上傳的在 bucket / 資料庫；尚未上傳的在 volume `openobserve-wal`，`down` 不刪 volume，重開後接著用 |
| 儀表板、pipeline、門檻、機器清單 | 在資料庫或由 provisioner 重新產生，不受影響 |
| 告警 | 停機期間不會有告警；告警狀態在資料庫，重開後接著評估。DOWN 告警要連續 4 分鐘，agent 補送很快，一般不會誤報 |
| LINE | 重開約 3 分鐘後送一則啟動彙整（含過去 12 小時的停機，停機期間沒資料的時段不算停機）；停機期間錯過的定時彙整不補送；已發過的告警不會重發 |
| 網頁 | 停機期間打不開，重開約 1～2 分鐘 |

- **不要 `down -v`**：會刪掉尚未上傳到 bucket 的資料和 Grafana 外掛快取。尚未上傳的資料通常是最近約 20 分鐘；資料很少的 stream 可能更久，因為 WAL 檔過了 10 分鐘後要等「下一筆資料」進來才會封檔。
- 整台主機重開機：服務是 `restart: unless-stopped`，Docker 起來後自己跑回來；但先 `down` 過的不會自動起來，要自己 `up -d`。

## 8. 備份（升級前）

`pg_dump` 的版本必須 ≥ 資料庫伺服器的版本（stack 自帶的 `postgres:17-alpine` 對較新的伺服器會拒絕），用對應版本的 image。在 collector 上（`<版本>` 換成伺服器的主版本，例如 `18`）：

```bash
cd ~/observability && set -a && . ./.prod.env && set +a && mkdir -p backups && for db in openobserve grafana monitoring; do docker run --rm -e PGPASSWORD="$PG_PASSWORD" postgres:<版本>-alpine pg_dump "host=$PG_HOST port=$PG_PORT user=$PG_USER dbname=$db sslmode=$PG_SSLMODE" | gzip > "backups/$db-$(date +%Y%m%d).sql.gz"; done
```

`backups/` 不進 git。用完可以 `docker rmi` 那個 image 省磁碟。bucket 裡的資料不用另外備份，但資料庫遺失的話 bucket 裡的檔案就沒有索引（資料還在、查不到）。

**CA**（建立後做一次，不用每次升級都做）：`backups/` 和 CA 在同一顆磁碟上，所以 CA 要另外存到 collector 以外的安全地方（例如密碼管理器）。`.env`、`.prod.env` 也一樣，它們同樣只存在 collector 上。在操作者的電腦上執行：

```bash
ssh <collector> 'tar czf - -C observability pki/ca.key pki/ca.crt' > observability-ca-$(date +%Y%m%d).tgz
```

## 9. 疑難排解

| 症狀 | 原因與處理 |
|---|---|
| `openobserve` 一直 `Restarting`，log：`postgres connect options create failed: Configuration(InvalidPort)` | `PG_PASSWORD` 有網址保留字元 → 照第 2 步的規則換密碼 |
| `openobserve` 一直重啟，log：`ZO_ROOT_USER_PASSWORD is too weak` | root 密碼缺大寫 / 數字 / 特殊字元其中一類 → 改 `.env`，`up -d` |
| `openobserve` 一直重啟，log：`db init failed` | `O2_EXT_AUTH_SALT` 沒填或被改過（必須和第一次啟動時一樣） |
| `otel-collector` 一直重啟，log：`open /certs/gateway.key: permission denied` | 私鑰擁有者不是 uid 10001 → 第 3 步最後的 `chown` |
| agent 的 log：`context deadline exceeded` / `Unavailable`，但 4317 連得上 | gateway 收到了但寫不進 OpenObserve（OpenObserve 沒起來）→ 先看 `docker compose -f docker-compose.prod.yml ps` |
| agent 的 log：timeout，連 4317 都連不上 | 防火牆（第 4 步）或 `OTEL_GATEWAY_BIND` 不是 `0.0.0.0` |
| 磁碟一直變滿 | 容器 log 每個上限 50 MB（compose 的 `x-logging`）；新增服務也要套上。其餘多半是 OpenObserve 的 WAL 或 image |
| 收不到 LINE | 告警：`docker logs grafana 2>&1 \| grep "Notify for alerts failed"`；彙整：`docker logs provisioner 2>&1 \| grep digest`。`401` = token 無效。不送訊息就能檢查 token：`curl -s -H "Authorization: Bearer <token>" https://api.line.me/v2/bot/info`（200 = 有效）。改 `.env` 後要 `up -d grafana provisioner`。測試發送：Grafana 13 的 `POST /grafana/apis/notifications.alerting.grafana.app/v1beta1/namespaces/default/receivers/<base64url(contact point 名稱)>/test`，body 為 `{"integration": <該 contact point 現有的 integration>, "alert": {...}}`（舊的 `/api/alertmanager/.../receivers/test` 已移除） |
| bucket 裡某個 stream 很晚才出現 | 資料很少時 WAL 要等下一筆資料才封檔，屬正常；查詢不受影響（會讀 WAL） |

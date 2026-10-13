# 從舊的 Prometheus + Grafana 帶過來的東西

舊系統在 GCP VM 的 `~/monitoring`（沒有 repo），這裡只當**參考**。下表是舊系統裡「不是預設、是刻意加上去的」設計；
目前全部都在這個 repo 的 Grafana 上沿用，實作在 `provisioner/grafana/`（差異見 AGENTS.md 的 Grafana 一節）。

## 告警設計（`provisioner/grafana/gen_alerts.py`）

| 規則 | 條件 | 等多久才通知 | 備註 |
|---|---|---|---|
| 機器沒有回應 Machine DOWN | `up{job="node"} == 0` | 4 分鐘 = 連續 5 次 | GCP 上是 1 分鐘；owner 要求和 Service DOWN 一樣 |
| ~~服務沒有回應 Service DOWN~~ | `probe_success == 0`（blackbox，只有 HTTP 200 算正常） | 4 分鐘 = 連續 5 次探測失敗 | **已移除（2026-10-13）**：observer 改成純 push，不再探測服務 |
| 用量超過 WARN | CPU / 記憶體 / 磁碟 `/` ≥ warn 門檻，且**還沒到** crit | 2 分鐘 | 到 crit 時只發 CRIT，不會兩則都發 |
| 用量超過 CRIT | ≥ crit 門檻 | 2 分鐘 | |
| 門檻資料讀不到 | `absent(usage_threshold_warn{machine="*"})` | 1 分鐘 | 監控「告警自己的依賴」壞掉 |
| 心跳 Heartbeat | 永遠成立 | – | 只為了讓 12 小時摘要永遠有東西可送（見下） |

- **門檻**：每台機器、每種資源（cpu / memory / disk）有 warn、crit 兩級；機器自己沒設就用預設列 `*`（75 / 85）。存在 PostgreSQL，在 Fleet Overview 卡片上按 ✎ 直接改，原則是「門檻不寫在檔案裡」。
- **通知策略**（兩條路由）：
  - **立即**：問題一發生就送一則 LINE，恢復時再送一則；同一個問題不重複提醒。
  - **12 小時摘要**：每 12 小時**一定**送一則，內容是「目前有哪些問題」或「全部正常」。靠 Heartbeat 讓摘要群組永遠非空，所以「12 小時沒收到摘要」本身就代表監控壞了（dead man's switch）。`repeat_interval` 11h30m 要比 `group_interval` 12h 小一點，否則每隔一次會被跳過。
- 沒資料（noData）視為正常，查詢出錯（execErr）視為告警。
- 規則標題中英並列。
- 送出方式：LINE Messaging API push（webhook ＋自訂 JSON payload），token 只放在不進版控的 env 檔。
- LINE 免費方案每月 200 則，摘要本身就佔約 60 則；新增會常常觸發的規則前要先想配額。
- 測試方式：產生器有 `test` 模式（計時器縮短到 1 分鐘、LINE 換成本機假 endpoint 記錄每個 request），測完才動正式的。

## 其他設計

| 項目 | 做法 |
|---|---|
| 機器顯示名稱 | GCP 上：每台機器用 textfile collector 發布 `provider_instance_name_info{name,provider}`，儀表板依序 fallback 到 hostname、instance（所以只改 instance 時卡片名稱不變）。這裡：agent 把 `instance` 設成 `host.name`（`HOST_NAME`），卡片直接顯示它，textfile 已移除 |
| Label 慣例 | GCP 上每個 target 都有 `project`；這裡改用 OpenTelemetry 標準的 `service_namespace`（見 AGENTS.md 的 Projects）（GCP 上 blackbox 另有 `service`，已隨服務探測移除） |
| HTTP 存活探測 | GCP 上用 blackbox-exporter（只有 200 算正常）；**這裡已移除**：純 push，observer 不探測 |
| 只聽 127.0.0.1 | 沿用；GCP 上用 Caddy 依路徑分流 + SSH tunnel，本機不需要 Caddy |
| 儀表板 | UI 不能存（`allowUiUpdates: false`），由 generator 產生；這裡更進一步：產生的檔案不進 repo，由 `provisioner` 在執行時產生 |
| Fleet Overview | Business Text 外掛：每台機器一張卡片、依門檻變色、卡片上直接改門檻 |
| 單機細節 | 1860 Node Exporter Full ＋ Availability 區塊，gauge 的警告線讀門檻表 |
| 測試習慣 | 先在拋棄式實例測、只在那裡測過的要明講「未在正式環境驗證」 |

## 為什麼是 Grafana + OpenObserve 分工（2026-10-12）

曾經試過全部放 OpenObserve（門檻放擴充表、OpenObserve 告警、OpenObserve 儀表板），結果太繞，放棄的原因：
- OpenObserve 不能自訂編輯介面：HTML panel 會被消毒（不能有 `<script>`），custom chart 的 JS 在沙箱裡、`fetch` 被擋，做不出卡片上的 ✎。
- 擴充表只能整批上傳 CSV / 指定 URL，不能逐列編輯；告警讀不到擴充表；門檻也畫不到 gauge 上。
- 告警：silence 期間停止評估、API 的 `frequency` 單位是分鐘（文件寫秒）、沒有 Alertmanager 那種「定期重送目前狀態」的路由，12 小時摘要做不出來。

所以：**Grafana 負責機器 / 服務狀態、門檻與 LINE 告警**（GCP 設計原樣搬過來），**OpenObserve 負責儲存與 logs / traces**。
Grafana 沒有自己的 Prometheus，metrics 透過 OpenObserve 的 Prometheus 相容 API 讀。

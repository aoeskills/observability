# Decommission runbook：把一台機器上的這套 stack 清乾淨

**decommission**（除役）：讓一台機器不再參與監控，並把這套 stack 在那台機器上留下的東西全部移除：服務、資料、安裝的套件、設定檔，以及跑過、測過的痕跡。做完之後，那台機器上只剩 repo 本身（如果要留著的話）。

這份的流程就是 2026-10-14 清理這台 Mac 的步驟，當時它跑過 local 模式（collector 全套）、自己的 agent、Homebrew 的 node_exporter，以及 sclaser-food。只是暫時停機、或從 Fleet Overview 移除一台 agent，不需要做到這份這麼徹底，看 [agent-runbook.md](agent-runbook.md) 的「日常操作」就好。

**大部分步驟都無法復原**：volume、設定檔、備份刪了就沒了。每一步都先列出來、確認，再刪。

## 0. 先決定範圍

| 項目 | 要決定的事 |
|---|---|
| 送資料的 app | 這台機器上的 app（例如 sclaser-food）要不要停；它的資料庫要停、要刪，還是不動 |
| gitignore 的檔案 | `.env`、`agent/.env`、`agent/probes/*.yml`、`pki/`、`backups/`：要留還是刪。`backups/` 裡可能有唯一一份的備份。local 模式的 `pki/` 是這台機器自己的 CA，刪掉只影響 local 模式（prod 的 CA 在 collector 上） |
| collector 上的紀錄 | 這台機器在 prod 的機器 / 服務清單裡要 **Retire**，還是保留（保留就會一直顯示 DOWN，每則彙整都會列出它） |
| 其他專案共用的東西 | Docker 的 build cache、`alpine` 之類的通用 image，可能也有別的專案在用 |

## 1. 停掉送資料的 app

app 不停的話，下一步拆掉 agent 之後，它還會繼續嘗試送 OTLP；被探測的服務也還開著 port。

```bash
lsof -iTCP -sTCP:LISTEN -P | grep -E ':(3000|3001) '
```

用 port 和工作目錄確認 process 是哪個 app，再停掉：

```bash
lsof -a -p <pid> -d cwd
```

```bash
kill <pid>
```

- 停不掉的才用 `kill -9`。
- sclaser-food 可能同時有好幾個 `next-server`：`npm run start:daemon` 的 `:3000`、standalone 的 `:3001`，以及沒在 listen 的孤兒 process。用 `ps aux | grep next-server` 全部找出來。
- app 的資料庫容器只停不刪（`docker stop <container>`），除非第 0 步決定要刪。

## 2. 停掉這套 stack 的容器

local 模式（collector + 這台機器的 agent）：

```bash
docker compose -f docker-compose.local.yml down
```

agent 單獨跑的時候（在 `agent/` 目錄下；Linux agent 是在 `~/agent`）：

```bash
docker compose -f docker-compose.yml -f docker-compose.mac.yml down
```

測試用、不屬於 compose 的容器也要停，例如 `fake-line`：

```bash
docker rm -f fake-line
```

最後確認沒有容器還在用 4317 / 4318 / 5080 / 13000 / 80 / 9100：

```bash
lsof -iTCP -sTCP:LISTEN -P | grep -E ':(4317|4318|5080|13000|80|9100) '
```

## 3. 移除 node_exporter（Mac：Homebrew 安裝的）

Mac 上的 node_exporter 是 `scripts/setup-node-exporter-macos.sh` 用 Homebrew 裝的，參數檔放在 Homebrew 的 `etc/` 裡。Linux agent 的 node_exporter 在 agent 的 compose 裡，第 2 步就停了，這一步不用做。

```bash
brew services stop node_exporter
```

```bash
brew uninstall node_exporter
```

```bash
rm -f "$(brew --prefix)/etc/node_exporter.args" "$(brew --prefix)"/var/log/node_exporter*.log
```

下載過的安裝檔在 Homebrew 的快取裡：

```bash
find "$(brew --cache)" -maxdepth 2 -iname '*node_exporter*' -exec rm -rf {} +
```

## 4. 刪 volume

compose 的具名 volume：local 模式的 project 名稱是 `observability`；agent 從 `agent/` 單獨啟動時是 `agent`。

```bash
docker volume ls --format '{{.Name}}' | grep -E '^(observability|agent)_'
```

```bash
docker volume rm $(docker volume ls -q | grep -E '^(observability|agent)_')
```

匿名 volume（64 位十六進位的名稱）是 image 自己宣告的 volume，每次 `compose up` 都可能多留下一個。先看建立時間，確認都是這個專案跑的那幾天，而且沒有容器在用：

```bash
for v in $(docker volume ls -q -f dangling=true | grep -E '^[0-9a-f]{64}$'); do docker volume inspect "$v" --format '{{.Name}} {{.CreatedAt}}'; done
```

```bash
docker volume rm <name> ...
```

## 5. 刪 image

這個專案用到的 image，從 compose 檔列出來：

```bash
grep -h 'image:' docker-compose*.yml agent/*.yml | sed 's/.*image: *//' | sort -u
```

除了這些，還有：
- **provisioner**：本機 build 的 `observability-provisioner`。
- **OpenObserve 的其他版本**：例如曾經試過的 `v1.1.0-rc2`。
- **測試和憑證工具**：`python:3-alpine`（fake-line）、`telemetrygen`、`alpine/openssl`、`curlimages/curl`、`alpine`。

刪之前先確認沒有容器（包括已經停止的）在用：

```bash
docker ps -a --format '{{.Names}} {{.Image}}'
```

```bash
docker rmi <image> ...
```

```bash
docker image prune -f
```

`docker builder prune -af` 會清掉**整個 Docker** 的 build cache，不只這個專案的（2026-10-14 清了 20 GB，sclaser-food 的建置快取也在裡面）。不會遺失資料，只是其他專案下次 build 比較慢。不想動到別的專案就跳過這一步。

## 6. 刪 repo 裡 gitignore 的檔案

先列出來：

```bash
git clean -ndX
```

清單會包括：
- 設定與機密：`.env`、`agent/.env`
- 每台機器自己的探測設定：`agent/probes/*.yml`（`example.yml.sample` 是進 git 的，會留著）
- 憑證和備份：`pki/`、`backups/`
- 執行與工具留下的：`__pycache__/`、Claude Code 的 `.claude/` 標記目錄、Finder 的 `.DS_Store`

確認第 0 步要留的東西不在清單裡（或已經另外備份），再刪：

```bash
git clean -fdX
```

## 7. 刪 repo 外的測試痕跡

- **Claude Code 的暫存目錄**：`/tmp/claude-<uid>/-Users-<user>-Projects-observability/`（每個 session 一個子目錄，裡面是測試腳本、假資料腳本、輸出）。在**沒有** Claude Code session 開著這個專案的時候刪，不然會刪到正在執行的 session 的檔案。
- **自己放在 `$TMPDIR` 或 `/tmp` 的腳本**：例如 `/tmp/claude-<uid>/q/`。
- **不屬於這一步的**：Claude Code 的對話紀錄和記憶（`~/.claude/projects/-Users-<user>-Projects-observability/`）不是測試痕跡。要刪的話是另一個決定。

## 8. 確認

```bash
git status --short --ignored
```

```bash
docker ps -a; docker volume ls; docker images
```

```bash
brew list | grep -i exporter
```

```bash
lsof -iTCP -sTCP:LISTEN -P | grep -E ':(3000|3001|4317|4318|5080|13000|80|9100) '
```

- `git status --short --ignored` 沒有輸出，表示 repo 和 `origin` 上的一模一樣。
- Docker 裡只剩其他專案的東西。
- brew 裡沒有 exporter，上面的 port 都沒人在 listen。

## 9. collector 那一端

除役的機器不會自己從 prod 消失：agent 是主動推資料的，停掉之後 collector 只會看到它「沒有聲音」。

- **Retire**：要讓它消失，就在 Fleet Overview 它的紅色 DOWN 卡片上按 **Retire**，它的每個服務也在 Services 的卡片上各按一次。沒按的話，它會一直是 DOWN，「過去 12 小時彙整」每次都會列出它。
- **已經存的資料**：metrics、traces、logs 會留在 OpenObserve，直到保存期限到了（`retention.json`）。沒有辦法只刪一台機器的資料。
- **憑證**：它的 client 憑證不會失效，有效期是 2 年，而且 gateway 沒有撤銷清單。如果擔心憑證外流，照 [agent-runbook.md](agent-runbook.md) 的「撤銷一台 agent」重建 CA。

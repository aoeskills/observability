# Decommission runbook：把一台機器上的這套 stack 清乾淨

**decommission**（除役）：讓一台機器不再參與監控，並把這套 stack 在那台機器上留下的東西全部移除：服務、資料、安裝的套件、設定檔，以及跑過、測過的痕跡。做完之後，那台機器上只剩 repo 本身（如果要留著的話）。

適用於跑過 local 模式（collector 全套）、collector、或 agent（含 macOS 的 Homebrew node_exporter）的機器；不適用的步驟跳過。只是暫時停機、或從 Fleet Overview 移除一台 agent，不需要做到這份這麼徹底，看 [agent-runbook.md](agent-runbook.md) 的「日常操作」就好。

**大部分步驟都無法復原**：volume、設定檔、備份刪了就沒了。每一步都先列出來、確認，再刪。

## 0. 先決定範圍

| 項目 | 要決定的事 |
|---|---|
| 送資料的 app | 這台機器上送資料或被探測的 app 要不要停；它的資料庫要停、要刪，還是不動 |
| gitignore 的檔案 | `.env`、`.prod.env`、`agent/.env`、`agent/probes/*.yml`、`pki/`、`backups/`、`SITE.md`：要留還是刪。`backups/` 裡可能有唯一一份的備份；collector 的 `pki/` 是 CA，刪了就不能再簽 agent 憑證 |
| collector 上的紀錄 | 這台機器在 collector 的機器 / 服務清單裡要 **Retire**，還是保留（保留就會一直顯示 DOWN，每則彙整都會列出它） |
| 其他專案共用的東西 | Docker 的 build cache、`alpine` 之類的通用 image，可能也有別的專案在用 |

## 1. 停掉送資料的 app

app 不停的話，下一步拆掉 agent 之後，它還會繼續嘗試送 OTLP；被探測的服務也還開著 port。用 `agent/probes/*.yml` 裡的 port 找出 process：

```bash
lsof -iTCP -sTCP:LISTEN -P | grep -E ':(<port>) '
```

用工作目錄確認 process 是哪個 app，再停掉（停不掉的才用 `kill -9`）：

```bash
lsof -a -p <pid> -d cwd
```

```bash
kill <pid>
```

- 同一個 app 可能有好幾個 process（daemon、沒在 listen 的孤兒 process），用 `ps aux | grep <app>` 全部找出來。
- app 的資料庫容器只停不刪（`docker stop <container>`），除非第 0 步決定要刪。

## 2. 停掉這套 stack 的容器

local 模式（collector + 本機的 agent）：

```bash
docker compose -f docker-compose.local.yml down
```

collector（prod）：

```bash
docker compose -f docker-compose.prod.yml down
```

agent（在 `~/agent`；macOS 加上 `-f docker-compose.yml -f docker-compose.mac.yml`）：

```bash
docker compose down
```

測試用、不屬於 compose 的容器也要停，例如 `fake-line`：

```bash
docker rm -f fake-line
```

最後確認沒有程式還在聽這套 stack 的 port（預設值；改過的話換成自己的）：

```bash
lsof -iTCP -sTCP:LISTEN -P | grep -E ':(4317|4318|5080|13000|80|9100|9115|9001) '
```

## 3. 移除 node_exporter（macOS：Homebrew 安裝的）

macOS 上的 node_exporter 是 `scripts/setup-node-exporter-macos.sh` 用 Homebrew 裝的，參數檔放在 Homebrew 的 `etc/` 裡。Linux agent 的 node_exporter 在 agent 的 compose 裡，第 2 步就停了，這一步不用做。

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

compose 的具名 volume 以 project 名稱開頭：collector / local 模式是 repo 目錄名稱（預設 `observability`），agent 是 `agent`（目錄 `~/agent` 或 repo 的 `agent/`）。

```bash
docker volume ls --format '{{.Name}}' | grep -E '^(observability|agent)_'
```

```bash
docker volume rm $(docker volume ls -q | grep -E '^(observability|agent)_')
```

匿名 volume（64 位十六進位的名稱）是 image 自己宣告的 volume，每次 `compose up` 都可能多留下一個。先看建立時間，確認都是這套 stack 跑的那段期間，而且沒有容器在用：

```bash
for v in $(docker volume ls -q -f dangling=true | grep -E '^[0-9a-f]{64}$'); do docker volume inspect "$v" --format '{{.Name}} {{.CreatedAt}}'; done
```

```bash
docker volume rm <name> ...
```

## 5. 刪 image

這套 stack 用到的 image，從 compose 檔列出來（agent 上在 `~/agent` 執行 `grep -h 'image:' *.yml`）：

```bash
grep -h 'image:' docker-compose*.yml agent/*.yml | sed 's/.*image: *//' | sort -u
```

除了這些，還有：
- **provisioner**：本機 build 的 `<project>-provisioner`（預設 `observability-provisioner`）。
- **用過的其他版本**：例如升級前的 OpenObserve image。
- **測試和憑證工具**：例如 `python:3-alpine`（fake-line）、`telemetrygen`、`postgres:<版本>-alpine`（備份）。

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

`docker builder prune -af` 會清掉**整個 Docker** 的 build cache，不只這套 stack 的。不會遺失資料，只是其他專案下次 build 比較慢。不想動到別的專案就跳過這一步。

## 6. 刪 repo 裡 gitignore 的檔案

（agent 上沒有 repo：直接刪 `~/agent`，裡面有 `.env`、`certs/`（私鑰）、`probes/`。）

先列出來：

```bash
git clean -ndX
```

清單會包括：
- 設定與機密：`.env`、`.prod.env`、`agent/.env`、`SITE.md`
- 每台機器自己的探測設定：`agent/probes/*.yml`（`example.yml.sample` 是進 git 的，會留著）
- 憑證和備份：`pki/`、`agent/certs/`、`backups/`
- 執行與工具留下的：`__pycache__/`、`.DS_Store` 等

確認第 0 步要留的東西不在清單裡（或已經另外備份），再刪：

```bash
git clean -fdX
```

## 7. 刪 repo 外的測試痕跡

測試時放在 `$TMPDIR`、`/tmp` 或其他地方的腳本、假資料、輸出，例如送測試資料的腳本、`telemetrygen` 的輸出、測試用的憑證。這些不在 repo 裡，只能靠自己記得放過哪裡。

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

- `git status --short --ignored` 沒有輸出，表示 repo 和遠端上的一模一樣。
- Docker 裡只剩其他專案的東西。
- brew 裡沒有 exporter，第 2 步的 port 都沒人在 listen。

## 9. collector 那一端

除役的機器不會自己從 collector 消失：agent 是主動推資料的，停掉之後 collector 只會看到它「沒有聲音」。

- **Retire**：要讓它消失，就在 Fleet Overview 它的紅色 DOWN 卡片上按 **Retire**，它的每個服務也在 Services 的卡片上各按一次。沒按的話，它會一直是 DOWN，「過去 12 小時彙整」每次都會列出它。
- **已經存的資料**：metrics、traces、logs 會留在 OpenObserve，直到保存期限到了（`retention.json`）。沒有辦法只刪一台機器的資料。
- **憑證**：它的 client 憑證不會失效，有效期是 2 年，而且 gateway 沒有撤銷清單。如果擔心憑證外流，照 [agent-runbook.md](agent-runbook.md) 的「撤銷一台 agent」重建 CA。
- 除役的是 collector 本身時：bucket 和資料庫在外部，要不要刪是另一個決定；刪資料庫而保留 bucket 會讓 bucket 裡的檔案變成查不到的孤兒。

# AGENT-INSTALL — установка cfsmcp2 для ИИ-агента

Этот файл — **протокол установки**. Выполни шаги по порядку. Лишний раз не спрашивай, но **точки подключения** (пути к дампам на хосте, §3.0) — обязательный вопрос перед Docker, если их ещё нет в `docker-compose.yml`. Если нет Docker или LM Studio — **предложи установить** (§2.1, §2.2), не ставь сам и не качай установщик без согласия. Таймзону контейнера (§3.15) по умолчанию бери с хоста; другую — только если пользователь назвал смещение или город.

Репозиторий: `https://github.com/folonsd-ai/cfsmcp2`

**По умолчанию — Docker** с точками подключения (`MOUNT_POINTS` + volumes). Так выгрузка 1С читается с диска хоста **без zip**. **Windows Portable (exe)** — если пользователь на Windows и просит «portable» / «exe» / установку без Docker и без Python (§4.5). Native (venv) — только если пользователь явно просит «без Docker» / «venv», либо отказался ставить Docker и portable (§2.1).

Протокол рассчитан на **Windows, macOS и Linux**. Сначала определи ОС (не копируй PowerShell на Mac; на Unix предпочитай `python3`).

Порт **8559** (Docker / native), MCP-имя **`cfsmcp2`**. **Windows portable** по умолчанию — порт **8561** (§4.5).

### Команда пользователя агенту (примеры)

| Способ | Что отправить агенту в Cursor |
|---|---|
| Docker (по умолчанию) | Установи cfsmcp2 из `https://github.com/folonsd-ai/cfsmcp2` по `AGENT-INSTALL.md`. |
| **Windows portable** | Установи cfsmcp2 **portable для Windows** из `https://github.com/folonsd-ai/cfsmcp2` по `AGENT-INSTALL.md` §4.5. |
| Portable + каталог сразу | Установи cfsmcp2 portable для Windows из `https://github.com/folonsd-ai/cfsmcp2` в `D:\tools\cfsmcp2` по `AGENT-INSTALL.md` §4.5. |
| Skill + portable | Установи skill cfsmcp2 из `https://github.com/folonsd-ai/cfsmcp2` и cfsmcp2 **portable для Windows** из того же репозитория по `AGENT-INSTALL.md` §4.5. |

Если пользователь явно просит **portable / exe / без Docker и без Python** на Windows — **не** уходи в §3 Docker и **не** спрашивай `MOUNT_POINTS` (источники 1С — «Локальный путь» в UI, §7). **Откуда ставить:** ZIP `cfsmcp2-win-portable-v*.zip` с [GitHub Releases](https://github.com/folonsd-ai/cfsmcp2/releases/latest) этого репозитория (`gh release download --repo folonsd-ai/cfsmcp2`, §4.5.1). Клон исходников для portable **не обязателен**; клон нужен для skill (§ Skill) или сборки ZIP, если релиза ещё нет.

## Skill (правила использования tools)

**AGENT-INSTALL** поднимает MCP-сервер. **Skill** учит агента вызывать tools
(без fan-out, маршруты, anti-patterns). Это разные шаги.

| Что | Где | Когда |
|-----|-----|-------|
| Skill | `.cursor/skills/cfsmcp2/` в репо | Копировать в `<workspace>/.cursor/skills/cfsmcp2/` или `~/.cursor/skills/cfsmcp2/` |
| MCP-сервер | Docker / native / Windows portable (§4.5) | Health UI (Docker/native `:8559`, portable `:8561` по умолчанию), контексты `ready` |

Если пользователь просит **«установи skill»** (без сервера) — см.
`.cursor/skills/cfsmcp2/SKILL.md` § «Установить skill». Не путай с `skills-cursor/`.

Если workspace **не** клон cfsmcp2 — для skill достаточно скопировать папку
`.cursor/skills/cfsmcp2/` из клона; полный репозиторий на диске нужен только
для поднятия сервера.

## 0. Где работать

1. Если текущий workspace — уже клон `cfsmcp2` (есть `docker-compose.yml`, `app/`, этот файл) — работай **в корне этого репозитория**.
2. Если репозитория ещё нет:
   ```bash
   git clone --depth 1 https://github.com/folonsd-ai/cfsmcp2.git
   ```
   Дальнейшие команды — в корне клона.
3. Не коммить и не пушь без явной просьбы пользователя.

| ОС | Как понять |
|---|---|
| Windows | `$env:OS` = `Windows_NT` |
| macOS | `uname -s` → `Darwin` |
| Linux | `uname -s` → `Linux` |

На Windows для health не вызывай голый `curl` в PowerShell (алиас `Invoke-WebRequest`) — используй `curl.exe` или `Invoke-RestMethod`.

## Цель

**Ветка Docker (по умолчанию):**

1. Проверить Docker (§2.1) и LM Studio (§2.2). Чего нет — **предложить установить** (Docker Desktop / LM Studio), не уходить в native молча и не пропускать LM Studio без слова.
2. **Предложить точки подключения** и спросить host-пути к выгрузкам (§3.0) — до первого `compose up`. В том же коротком сообщении: таймзона контейнера — как на хосте или другое смещение (§3.15).
3. Прописать пути в `docker-compose.yml` (`MOUNT_POINTS` + volumes), задать `TZ`, поднять **Docker** (native — §4, только по выбору пользователя).
4. Проверить UI / health / runtime.
5. Подключить MCP-сервер `cfsmcp2` в ИИ-клиенте.
6. Кратко отчитаться.

**Ветка Windows portable (§4.5)** — если пользователь просит portable или выбрал его в §2.1:

1. LM Studio — по §2.2 (предложить, не ставить сам).
2. **Спросить каталог установки** (§4.5.1), если пользователь **не** указал путь в запросе.
3. Скачать ZIP с Releases, распаковать, запустить launcher / сервер, health на порту из launcher (по умолчанию **8561**).
4. Подключить MCP с URL **из окна launcher** (не `:8559` по умолчанию).
5. Кратко отчитаться (§8); контекст 1С — §7 («Локальный путь»).

## 1. Контекст

| Параметр | Значение |
|---|---|
| Порт | `8559` (UI + MCP) |
| UI | http://127.0.0.1:8559/ |
| MCP URL | `http://127.0.0.1:8559/mcp/` (канон со слэшем) |
| Данные | `./data` (SQLite, dumps, zvec) — volume `./data:/data` |
| Эмбеддинги | LM Studio на хосте `:1234` → в контейнере `http://host.docker.internal:1234` |
| Точки | `MOUNT_POINTS` + bind-mounts в `docker-compose.yml` |
| Таймзона | `TZ` (POSIX, напр. `UTC-3` = UTC+3). По умолчанию — смещение **хоста**; иначе то, что назвал пользователь. Без `TZ` контейнер живёт в UTC |

Если порт `8559` уже занят cfsmcp2 — не поднимай второй экземпляр: проверь health и переходи к MCP. Если занят чужим процессом — разберись, порт без просьбы не меняй. Перед Docker останови native uvicorn на этом порту, если он был запущен.

## 2. Почему Docker + точки

- Hierarchical-выгрузка **не упаковывается**: host-каталог монтируется в `/mnt/…`, UI → «Точка подключения» → browse.
- Несколько конфигураций — несколько named mounts. По умолчанию одна точка: `dumps`.
- Данные индексов в `./data` переживают пересборку образа.
- UI сам определяет docker/native (`RUNTIME_MODE=auto`); в Docker нет tkinter «Обзор» хоста — есть browse внутри точки.

Native оставляй, если: пользователь сказал «без Docker» / «venv»; либо Docker нет и пользователь **отказался** его ставить (§2.1). Не переходи на venv молча.

### 2.1. Есть ли Docker

**До** правок compose и `docker compose up` проверь:

```bash
docker compose version
```

Ок: есть номер версии → §2.2, затем §3.  
Не ок: команда не найдена, «daemon is not running», Compose v1 без плагина — Docker для протокола **не найден**.

Если не найден — **одним коротким сообщением** предложи установку **Docker Desktop** (не ставь сам, не качай установщик без согласия):

> Docker не найден. Поставьте [Docker Desktop](https://docs.docker.com/get-docker/) (Windows / macOS) или Docker Engine + Compose на Linux. После установки перезапустите терминал (и сам Docker Desktop) и напишите — продолжим с точками подключения. Если Docker ставить не хотите — на Windows можно **portable exe** (§4.5) или native (Python venv, §4).

| Ответ | Что делать |
|---|---|
| Поставит / уже ставит Docker | Подожди подтверждения, снова `docker compose version`, затем §3. Не выдумывай, что Docker уже есть |
| Portable / exe (Windows) | §4.5 |
| Native / venv / без Docker | §4 |
| Молчание после предложения | Ещё раз не спрашивай цикл; не запускай native/portable без явного согласия. Напиши, что ждёшь Docker или «portable» / «native» |

Демон установлен, но не запущен (Windows: Docker Desktop не стартовал): попроси открыть Docker Desktop, подожди, повтори `docker compose version`. Это не повод сразу уходить в native.

Если в том же проходе нет и LM Studio (§2.2) — оба предложения можно дать **одним** сообщением, затем ждать ответы.

### 2.2. Есть ли LM Studio

Нужен для семантического поиска (эмбеддинги). Сервер cfsmcp2 без него поднимается; FTS и карточки объектов работают, `semantic_search` / hybrid без живого Local Server нет.

**Проверка** (таймаут 2–3 с; на Windows не голый `curl`):

```bash
# macOS / Linux / Git Bash
curl -sS --max-time 3 http://127.0.0.1:1234/v1/models

# Windows
curl.exe -sS --max-time 3 http://127.0.0.1:1234/v1/models
# или: Invoke-RestMethod http://127.0.0.1:1234/v1/models
```

Ок: JSON со списком моделей (или пустой `data`, но HTTP 200) → LM Studio Local Server жив → §3. Модель эмбеддинга на этом шаге не обязана быть загружена; если список пуст, в конце напомни выбрать `text-embedding-multilingual-e5-small` (или аналог) в Local Server.

Не ок: отказ соединения, таймаут, «connection refused» — Local Server **не найден** (не установлен или не запущен).

Тогда **предложи установить / запустить** (не ставь сам, не качай установщик без согласия):

> LM Studio на порту 1234 не отвечает. Для семантического поиска поставьте [LM Studio](https://lmstudio.ai/), включите Local Server на порту **1234**, загрузите embedding-модель (рекомендуется `text-embedding-multilingual-e5-small`). Когда сервер онлайн — напишите. Docker/native можно поднять и без этого: полнотекст и карточки будут, семантика появится после LM Studio.

| Ответ | Что делать |
|---|---|
| Поставит / уже ставит / «сейчас запущу» | Подожди, снова запрос на `:1234`, затем дальше. Не выдумывай, что LM Studio уже online |
| «Позже / без семантики / пропускаем» | Продолжай установку cfsmcp2. В конце явно: семантика offline, пока не будет Local Server |
| Молчание | Ещё раз не крути цикл. Продолжай Docker/native; в отчёте §8 напиши, что LM Studio не ответил |

Offline после установки cfsmcp2 не «чини» правкой исходников и URL в репозитории. Скажи проверить Local Server и модель. URL в compose уже `http://host.docker.internal:1234` (Docker) / `http://127.0.0.1:1234` (native). Меняй в UI, только если индикатор offline и в `app_settings` другой адрес (БД важнее env).

## 3. Docker (по умолчанию)

### 3.0. Предложить точки подключения

**До** `docker compose up` **предложи** точки и спроси host-пути (одним коротким сообщением, можно вместе с §2.1/§2.2, если ещё ждёшь установщики):

1. Предложи одну точку: `dumps=/mnt/dumps` (общий корень выгрузок конфигураций и расширений). Другие id не предлагай, пока пользователь сам не назовёт второй диск или отдельное дерево.
2. Спроси **host-путь** для `dumps`: родительский каталог (или выше), внутри которого лежат одна или несколько Hierarchical-выгрузок — **не** сам каталог с `Configuration.xml`.
3. Таймзона контейнера: **как на хосте** (назови POSIX, напр. `UTC-3` для Москвы) **или другое смещение** (`UTC-5` для Екатеринбурга). Если не ответил — ставь смещение хоста.

Пример формулировки:

> Для Docker нужна точка подключения: контейнер видит только то, что смонтируем. Предлагаю `dumps=/mnt/dumps`. Напишите путь на диске к **родителю** выгрузок (папка выше `Configuration.xml`). Таймзона контейнера: как на этой машине (`TZ=…`) или другое смещение?

Зачем: без bind-mount контейнер **не видит** диск хоста; zip для Hierarchical не нужен и нежелателен. Полученные пути сразу пиши в `docker-compose.yml` (§3.1), потом поднимай стек.

**Корень точки — не выгрузка.** Bind-mount задаёт верхнюю границу дерева, которое контейнер и UI видят. Обзор в «Точке подключения» ходит **только вниз** от `/mnt/…`, выше точки подняться нельзя. Поэтому в volume кладут родителя (или каталог ещё выше), а каталог с `Configuration.xml` выбирают уже в UI внутри точки. Если смонтировать саму выгрузку, Обзор откроется сразу в её корне: соседние выгрузки недоступны, сменить источник без правки compose нельзя.

Если пользователь ответил «пока нет / позже» — можно поднять только `./data:/data` и пустой/закомментированный `MOUNT_POINTS`, но явно скажи, что контекст из файла добавят после правки volumes и пересоздания контейнера.  
Если пути уже есть в compose и пользователь их подтвердил — не переспрашивай. **Не выдумывай** диски и папки.

### 3.1. Точки подключения

Отредактируй `docker-compose.yml` по ответам из §3.0:

1. В `environment` задай `MOUNT_POINTS` (id → путь **внутри контейнера**):
   ```yaml
   RUNTIME_MODE: auto
   MOUNT_POINTS: dumps=/mnt/dumps
   ```
   Вторая точка только если пользователь сам назвал ещё один корень. Опционально label: `dumps=/mnt/dumps|Dumps`.
2. В `volumes` свяжи **host-путь пользователя** с тем же контейнерным путём. Host-путь — **родитель выгрузки** (или выше), не каталог, в котором лежит `Configuration.xml`:
   ```yaml
   volumes:
     - ./data:/data
     - W:/1c/projects:/mnt/dumps       # Windows: родитель, внутри — папки выгрузок
     # - /home/user/1c:/mnt/dumps      # Linux
     # - /Users/user/1c:/mnt/dumps     # macOS
   ```
   Host-путь — то, что сказал пользователь (после нормализации «на уровень выше», если он назвал саму выгрузку); контейнерный (`/mnt/…`) — тот же, что в `MOUNT_POINTS`.

   Пример. Выгрузка на хосте: `W:/1c/projects/ERP/cf` (`Configuration.xml` в `cf`). В compose: `W:/1c/projects:/mnt/dumps` (или `W:/1c/projects/ERP:/mnt/dumps`). В UI: точка `dumps` → Обзор → `ERP/cf` (или `cf`). **Неверно:** `W:/1c/projects/ERP/cf:/mnt/dumps`.

`./data:/data` не трогай — там БД и zvec (индексы), это не каталог исходных дампов 1С.

### 3.15. Таймзона контейнера

Без `TZ` контейнер в **UTC**. Пакет `tzdata` в образ **не** кладём (раздувает слой). Достаточно POSIX-смещения: `UTC-3` = UTC+3 (Москва), `UTC-5` = UTC+5 (Екатеринбург). Знак в POSIX **обратный**.

**Правило:** смещение **хоста**, если пользователь не назвал другое. Не хардкодь Москву, если хост в другой зоне.

1. Возьми смещение хоста (часы от UTC) и запиши POSIX `TZ`:
   - Windows: `[TimeZoneInfo]::Local.GetUtcOffset([datetime]::UtcNow)` → при +3 ч ставь `UTC-3`
   - Linux / macOS: `date +%z` (`+0300` → `UTC-3`)
2. Если пользователь указал другую зону — переведи в POSIX сам (`Asia/Yekaterinburg` → `UTC-5`), имена `Europe/Moscow` **без tzdata не работают**.
3. Запиши в `.env` в корне (в git не коммитится) или в окружение перед compose:

   ```
   TZ=UTC-3
   ```

   ```bash
   TZ=UTC-5 docker compose up -d --build          # Linux / macOS
   $env:TZ = "UTC-5"; docker compose up -d --build  # Windows PowerShell
   ```

   В compose уже есть `TZ: ${TZ:-UTC-3}`. `UTC-3` там — только запас, когда переменную не задали. `rebuild.bat` ставит `TZ` с хоста сам.
4. Смена зоны: новый `TZ` и `docker compose up -d` (пересоздать контейнер, `--build` не нужен).

Native (§4): `TZ` не ставь — процесс берёт зону ОС сам.

### 3.2. Запуск

```bash
docker compose up -d --build
docker compose ps
docker compose logs --tail=50
```

Ожидается сервис `cfsmcp2` running / **healthy**, без traceback.

Health:

```bash
# macOS / Linux / Git Bash
curl -sS http://127.0.0.1:8559/api/health

# Windows
curl.exe -s http://127.0.0.1:8559/api/health
# или PowerShell:
Invoke-RestMethod http://127.0.0.1:8559/api/health
```

Ожидается: `{"status":"ok","service":"cfsmcp2"}`.

Runtime (точки):

```bash
curl -sS http://127.0.0.1:8559/api/system/runtime
```

Ожидается: `"mode":"docker"`, `"path_delivery":"mounts"`, у нужных mounts `"ok":true`. Если `ok:false` — volume не смонтирован или путь хоста неверный.

`LM_STUDIO_URL` в compose уже `http://host.docker.internal:1234`. На Linux в compose есть `extra_hosts: host.docker.internal:host-gateway`. Override UI: `RUNTIME_MODE=docker|native`, если автодетект ошибся.

## 4. Native (запасной путь)

Только если пользователь просит venv / «без Docker», либо Docker нет и он **отказался** ставить его (§2.1). Нужен **Python 3.13+**. LM Studio всё равно проверь по §2.2 (native без точек, но семантика та же).

| ОС | Python | venv | pip |
|---|---|---|---|
| Windows | `py -3.13` / `python` | `.venv\Scripts\python.exe` | `.venv\Scripts\pip.exe` |
| macOS / Linux | `python3.13` / `python3` | `.venv/bin/python` | `.venv/bin/pip` |

```bash
# macOS / Linux
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# Windows
py -3.13 -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

Запуск в фоне (агент не должен ждать вечно):

```bash
# macOS / Linux
mkdir -p data
nohup .venv/bin/python app/main.py >> data/cfsmcp2.stdout.log 2>&1 &
echo $! > data/cfsmcp2.pid
```

```powershell
# Windows
Start-Process -FilePath ".venv\Scripts\python.exe" -ArgumentList "app\main.py" -WorkingDirectory (Get-Location) -WindowStyle Hidden
```

Health — как в §3. В UI: **«Локальный путь»** + «Обзор…» (tkinter, localhost). `MOUNT_POINTS` в native не используются для UI.

Не используй `--reload` на рабочей индексации.

## 4.5. Windows Portable (exe)

Только **Windows**. Не нужны Docker и Python на машине пользователя — один каталог с `cfsmcp2.exe`, встроенный launcher (окно + трей) и сервер (тот же exe, режим `--server`).

Когда предлагать: пользователь на Windows и просит portable / exe / «поставь без Docker»; или Docker нет, Python ставить не хочет. **Не** путай с native venv (§4) и **не** собирай PyInstaller без явной просьбы разработчика — для установки бери **готовый ZIP** с [GitHub Releases](https://github.com/folonsd-ai/cfsmcp2/releases/latest).

### 4.5.1. Протокол агента (установка portable)

Только **Windows**. Источник сборки — репозиторий из запроса пользователя (по умолчанию `https://github.com/folonsd-ai/cfsmcp2`): скачай **последний** asset `cfsmcp2-win-portable-v*.zip` с Releases (`folonsd-ai/cfsmcp2`). Работай **вне** клона исходников — в каталоге установки, который назвал пользователь (или который он подтвердил).

**Куда ставить (обязательный вопрос).** Перед скачиванием ZIP **одним коротким сообщением** спроси полный путь к **пустому или уже используемому** каталогу установки, **если пользователь не написал его в запросе** (см. таблицу команд выше). **Не выдумывай** диски и папки; **не** распаковывай в `%TEMP%` как постоянную установку.

Пример формулировки:

> Куда распаковать portable cfsmcp2? Укажите полный путь (например `D:\tools\cfsmcp2` или `%LOCALAPPDATA%\Programs\cfsmcp2-portable`). В этой папке будет `cfsmcp2.exe`, рядом `data\` с индексами.

| Ответ | Действие |
|---|---|
| Назвал путь | Нормализуй (`Expand-EnvironmentVariables`), создай каталог при необходимости |
| «Как удобнее» / не знает | Предложи **один** вариант: `%LOCALAPPDATA%\Programs\cfsmcp2-portable` — и дождись «да» или другого пути |
| В каталоге уже есть `cfsmcp2.exe` | Спроси: **обновить** (ZIP поверх, сохранить `data\` и `cfsmcp2.ini`) или **новая папка** |

**Скачивание и распаковка** (предпочтительно `gh`, иначе API GitHub):

```powershell
$InstallRoot = "D:\tools\cfsmcp2"   # путь от пользователя
New-Item -ItemType Directory -Force -Path $InstallRoot | Out-Null
$Zip = Join-Path $env:TEMP "cfsmcp2-win-portable.zip"
gh release download --repo folonsd-ai/cfsmcp2 --pattern "cfsmcp2-win-portable*.zip" --output $Zip
Expand-Archive -Path $Zip -DestinationPath $InstallRoot -Force
# если ZIP с корневой папкой — перенеси содержимое так, чтобы cfsmcp2.exe лежал прямо в $InstallRoot
```

Без `gh`: GET `https://api.github.com/repos/folonsd-ai/cfsmcp2/releases/latest`, asset с именем `cfsmcp2-win-portable-v*.zip`, скачать в `$Zip`, затем `Expand-Archive`. Если релиза ещё нет — только тогда из клона репозитория: `powershell -ExecutionPolicy Bypass -File packaging\package-portable.ps1`, ZIP в `dist\`.

**Первый запуск.**

1. Запусти GUI: `Start-Process -FilePath (Join-Path $InstallRoot "cfsmcp2.exe") -WorkingDirectory $InstallRoot`.
2. Попроси пользователя нажать **Старт** в окне (или в трее), пока агент не может кликать по GUI — **не** выдумывай, что сервер уже online.
3. Для проверки health **без GUI** (если пользователь согласен или окно недоступно): фоновый сервер тем же exe — `Start-Process -FilePath (Join-Path $InstallRoot "cfsmcp2.exe") -ArgumentList "--server" -WorkingDirectory $InstallRoot -WindowStyle Hidden`. Порт и host — из `cfsmcp2.ini` рядом с exe, если файл уже есть; иначе **8561** / `127.0.0.1`.
4. Опционально перед первым GUI-запуском можно создать `cfsmcp2.ini` с `[launcher] autostart_server=true`, чтобы сервер стартовал сам — только если пользователь просил «без ручного Старт».

**После «Запущен»:** health (порт из launcher / ini):

```powershell
curl.exe -sS --max-time 5 http://127.0.0.1:8561/api/health
```

**MCP:** смержи в `.cursor/mcp.json` URL **из поля MCP в окне** (по умолчанию `http://127.0.0.1:8561/mcp/`). Skill — по § Skill (клон + копия `.cursor/skills/cfsmcp2/`), если пользователь просил skill.

**Не делай для portable:** правки `docker-compose.yml`, вопросы про `MOUNT_POINTS`, клон репозитория «ради сервера» (только ZIP или сборка при отсутствии релиза).

### Установка (кратко для человека)

1. Скачай архив `cfsmcp2-win-portable-v*.zip` из Releases (если релиза ещё нет — собери из клона: `powershell -ExecutionPolicy Bypass -File packaging\package-portable.ps1`, ZIP в `dist\`).
2. Распакуй в любую папку, например `D:\tools\cfsmcp2-win-portable\`. В корне — **только** `cfsmcp2.exe`, рядом `_internal\`, при первом запуске создаётся `data\`.
3. Запусти `cfsmcp2.exe`. Иконка сразу в трее; сворачивание убирает окно с панели задач.
4. В окне: **Старт** → дождись статуса **«Запущен»** (пока идёт «Запуск…», **Открыть UI** недоступна).
5. **Открыть UI** или URL из поля UI (по умолчанию http://127.0.0.1:8561/ ). MCP — URL из поля MCP (по умолчанию http://127.0.0.1:8561/mcp/ ).
6. Настройки порта и «только этот компьютер / по сети» — в окне launcher, сохраняются в `cfsmcp2.ini` рядом с exe.

| Параметр | Portable (по умолчанию) |
|---|---|
| Порт UI + MCP | `8561` |
| UI | http://127.0.0.1:8561/ |
| MCP URL | http://127.0.0.1:8561/mcp/ |
| Данные | `data\` рядом с exe (SQLite, dumps, zvec) |
| LM Studio | http://127.0.0.1:1234 (как native, §2.2) |
| Источники 1С | **«Локальный путь»** + «Обзор…» (tkinter на хосте) |

Health (после «Старт»):

```powershell
curl.exe -sS --max-time 3 http://127.0.0.1:8561/api/health
```

Ожидается HTTP 200. Порт и host смотри в окне launcher, если меняли `cfsmcp2.ini`.

### Обновление portable

- В окне launcher: щелчок по бейджу **версии** (`v…`) → проверка GitHub → при новой версии скачивание, установка и предложение перезапуска. Каталоги `data\` и `cfsmcp2.ini` не перезаписываются.
- Вручную: ZIP с Releases → распаковать поверх установки, **не** заменяя `data\` и `cfsmcp2.ini`.

### MCP (portable)

URL и порт — **из окна launcher** (не подставляй `:8559`, если в UI указан другой порт). Пример для порта по умолчанию:

```json
{
  "mcpServers": {
    "cfsmcp2": {
      "url": "http://127.0.0.1:8561/mcp/"
    }
  }
}
```

Один экземпляр launcher на каталог установки (lock в `data\.launcher.lock`). Перед пересборкой разработчиком закрой `cfsmcp2.exe`.

Подробнее для пользователя: [README-portable.txt](README-portable.txt).

## 5. LM Studio (после подъёма сервера)

Проверка и предложение установки — **§2.2** (до или вместе с Docker). Здесь только эксплуатация:

1. Docker: URL в compose уже `http://host.docker.internal:1234`. Native и portable: `http://127.0.0.1:1234`.
2. На чистой установке URL в UI прописывать не обязательно.
3. Если после §2.2 Local Server так и offline — в отчёте §8 напиши это; не чини переписыванием проекта.

## 6. MCP для разных ИИ-клиентов

Сервер — **Streamable HTTP** на `/mcp/` (канон — со слэшем). Docker/native по умолчанию: `http://127.0.0.1:8559/mcp/`; **Windows portable** — URL из окна launcher (по умолчанию `http://127.0.0.1:8561/mcp/`). **26 tools** (группы: контексты, поиск, объект, BSL, формы, СКД, статус). Список и правила маршрута — [README.md](README.md) § MCP. `list_code_modules` требует `q` (≥2 символа); полный перечень модулей не отдаётся. Если пользователь назвал конфигурацию — искать **только в ней**.

### Cursor

Смержи ключ `cfsmcp2` в `.cursor/mcp.json`, не затирая другие серверы:

```json
{
  "mcpServers": {
    "cfsmcp2": {
      "url": "http://127.0.0.1:8559/mcp/"
    }
  }
}
```

### Другие клиенты

| Клиент | Файл конфига | Ключ объекта | Нюанс |
|---|---|---|---|
| Cursor | `.cursor/mcp.json` | `"mcpServers"` | лучше `/mcp/` со слэшем |
| Claude Code | `.mcp.json` | `"mcpServers"` | нужен `"type": "http"` |
| Claude Desktop | Windows: `%APPDATA%\Claude\`; macOS: `~/Library/Application Support/Claude/`; Linux: `~/.config/Claude/` | `"mcpServers"` | при необходимости `"type": "http"` |
| VS Code / Copilot | `.vscode/mcp.json` | `"servers"` (**не** `mcpServers`) | `"type": "http"` |
| Cline / Roo | UI | обычно `"mcpServers"` | URL вручную |

Claude Code:

```json
{
  "mcpServers": {
    "cfsmcp2": {
      "type": "http",
      "url": "http://127.0.0.1:8559/mcp/"
    }
  }
}
```

VS Code:

```json
{
  "servers": {
    "cfsmcp2": {
      "type": "http",
      "url": "http://127.0.0.1:8559/mcp/"
    }
  }
}
```

После изменения конфига переподключи MCP (перезагрузка окна / сессии / приложения).

## 7. Первый контекст

После health OK **не предлагай upload zip**, если выгрузка уже на диске хоста.

1. UI → добавить сущность → **«Точка подключения»** (Docker) или **«Локальный путь»** (native / portable).
2. Docker: выбрать точку `dumps` → Обзор → каталог с `Configuration.xml` или `.txt`. Путь будет вида `/mnt/dumps/...`.
3. Дождаться `ready` (большой dump — минуты–часы).
4. MCP: `list_contexts` — только **enabled + ready**.

Если после переезда в Docker старый host-путь сущности недоступен — «Источник» → подсказки по имени папки → выбрать → Загрузить.

Если пользователь назвал конфигурацию по имени — **не** гоняй один запрос по всем `list_contexts`.

## 8. Что сообщить пользователю в конце

1. Способ: **Docker** (+ какие `MOUNT_POINTS` / ok, какая `TZ`) / **Windows portable** / native / ошибка.
2. Health: OK / нет; runtime mounts ok.
3. Какой клиент настроен, путь к конфигу и фрагмент с `cfsmcp2`.
4. Дальше: LM Studio online (§2.2) → точка / путь → `ready` → `list_contexts`. Если LM Studio предложили и ещё нет — так и скажи.

## 9. Запреты

- Не удалять `./data`.
- Не менять порт без просьбы.
- Не делать git commit / push без просьбы.
- Не «чинить» offline LM Studio переписыванием проекта.
- Не копировать выгрузки 1С в репозиторий и не коммитить `data/`.
- Не подставлять выдуманные host-пути в volumes.

## Обновление

- Docker: `docker compose up -d --build` (после правки `MOUNT_POINTS`/volumes — пересоздать контейнер). Смена `TZ` — `up -d` с новой переменной, `--build` не обязателен.
- Windows portable: бейдж версии в launcher или ZIP с Releases поверх установки (сохранить `data\`, `cfsmcp2.ini`).
- Native: обновить pip в venv, перезапустить процесс.
- Конфиг MCP — только если URL ещё не настроен или отличается.
- Прерванная индексация при старте дожимается сама.

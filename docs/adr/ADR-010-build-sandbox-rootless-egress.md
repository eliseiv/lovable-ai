# ADR-010 — Изоляция build-песочницы: rootless Docker + egress-allowlist (закрытие TD-001)

**Статус:** Accepted · **Дата:** 2026-06-02 · **Sprint:** 4

Закрывает [Q-INFRA-1](../99-open-questions.md#q-infra-1) (топология build-хостов) и [Q-DEPLOY-1](../99-open-questions.md#q-deploy-1) (supply-chain `npm ci`), погашает [TD-001](../100-known-tech-debt.md#td-001). Реализует продуктовые решения [08 §4-3, §4-4](../08-product-decisions.md#sprint-4--sandbox--security).

## Context

В Sprint 1 build недоверенного LLM-кода (`npm ci && vite build`) исполняется в эфемерном build-контейнере, который `worker` запускает через **смонтированный `docker.sock`** с базовой изоляцией (`cap-drop ALL`, non-root, ресурс-лимиты), **без** усиленного runtime и **без** egress-ограничений ([07-deployment.md → «Модель изоляции сборки по спринтам»](../07-deployment.md#модель-изоляции-сборки-по-спринтам)). Это [TD-001](../100-known-tech-debt.md#td-001): монтированный `docker.sock` = эскалация до хоста при компрометации воркера; отсутствие egress-allowlist = exfiltration/SSRF/атака на внутреннюю сеть из postinstall-скриптов npm-пакетов.

Центр threat-model — исполнение недоверенного кода при сборке ([05-security.md](../05-security.md)). Требуется зафиксировать **исполняемый** runtime-контракт изоляции, оставленный на уровне «направление» в [08 §4-3/§4-4](../08-product-decisions.md#sprint-4--sandbox--security).

Кандидаты runtime изоляции: (1) shared `docker.sock` (S1, статус-кво), (2) gVisor/`runsc` (user-space kernel), (3) **rootless Docker** (демон + контейнеры без root на хосте, user-namespace remap).

## Decision

**Build-песочница исполняется через rootless Docker на выделенных build-хостах, с egress-allowlist только к npm-registry.** Выбор зафиксирован продуктово ([08 §4-3](../08-product-decisions.md#sprint-4--sandbox--security)); ADR фиксирует исполняемую конфигурацию.

### A. Runtime: rootless Docker (вместо shared docker.sock / gVisor)

- На build-хосте запускается **rootless Docker-демон** под непривилегированным служебным пользователем (не root). Компрометация build-контейнера или самого rootless-демона **не даёт root на хосте** — user-namespace remap отображает root-внутри-контейнера на unprivileged UID хоста.
- `worker` (`queue=build`) обращается к rootless-демону через его сокет (rootless `docker.sock` в `$XDG_RUNTIME_DIR`), а **не** к привилегированному системному `docker.sock`. Это снимает корень риска [TD-001](../100-known-tech-debt.md#td-001): даже при компрометации воркера доступный сокет — rootless, без прямого root-доступа к хосту.
- Выбор `BUILD_SANDBOX_RUNTIME` параметризован env (`rootless` дефолт; зарезервировано значение `runsc` для опционального наложения gVisor поверх rootless без смены контракта запуска — см. Alternatives).

### B. Конфигурация запуска build-контейнера (нормативная)

Каждый build-контейнер запускается эфемерно (`docker run --rm`) с флагами (нормативный источник — [05-security.md → threat-model](../05-security.md#threat-model-центр--build-sandbox) и [modules/deploy/03-architecture.md §1](../modules/deploy/03-architecture.md#1-sandbox-исполнение-недоверенного-кода)):

| Флаг | Значение | Назначение |
|---|---|---|
| `--cap-drop ALL` | — | Снятие всех Linux capabilities. |
| `--security-opt no-new-privileges` | — | Запрет escalation через setuid-бинарники. |
| `--read-only` | — | Read-only rootfs; запись только в смонтированный workspace. |
| `--tmpfs /tmp` | размер-лимит | Эфемерный `/tmp` (npm temp), не на rootfs. |
| `-v {workspace}:/workspace` | rw, **только** workspace | Единственная writable-точка на диске (исходники + `node_modules` + `dist` + tool-caches, см. §B-2). |
| `--user {non-root UID}` | напр. `10001:10001` | Non-root внутри контейнера (поверх user-namespace remap rootless). |
| `-e HOME=…` `-e npm_config_cache=…` `-e XDG_CACHE_HOME=…` `-e XDG_CONFIG_HOME=…` | writable-пути в workspace (см. §B-2) | **Обязательно** под `--read-only` + non-root: даёт инструментам (npm/vite/esbuild) писучий HOME и cache-директории, иначе они пишут в `/.npm` (HOME=`/`) на read-only rootfs → ENOENT. |
| `--cpus` | `BUILD_CPU_LIMIT` | CPU-лимит (resource-exhaustion). |
| `--memory` | `BUILD_MEM_LIMIT` | RAM-лимит + OOM-kill. |
| `--pids-limit` | `BUILD_PIDS_LIMIT` | Анти-fork-bomb. |
| `--network {build_egress_net}` | egress-allowlist сеть | Сеть только с доступом к npm-registry (см. C). |
| `-e http_proxy={BUILD_EGRESS_PROXY_URL}` `-e https_proxy={BUILD_EGRESS_PROXY_URL}` | URL egress-proxy | **Обязательно при непустом `BUILD_EGRESS_NETWORK`** — единственный маршрут `npm ci` к registry в `internal`-сети (см. §C). |
| `--security-opt seccomp={BUILD_SECCOMP_PROFILE}` | **условно** (см. §B-1) | Фильтр syscalls. Передаётся **только** при непустом `BUILD_SECCOMP_PROFILE`; иначе не передаётся — действует встроенный default seccomp Docker. |
| wall-clock timeout | `BUILD_TIMEOUT_S` (на стороне воркера) | Жёсткий kill зависшей сборки (`docker rm -f`). |

#### B-1. Параметризация seccomp-профиля (нормативная)

Docker **не** имеет токена `seccomp=default`: допустимы только **путь к JSON-профилю** или `unconfined`. Если `--security-opt seccomp` не передан вовсе, Docker применяет **встроенный default seccomp-профиль автоматически** (syscall-фильтр активен). Отсюда env-контракт:

| | Значение `BUILD_SECCOMP_PROFILE` | Поведение build-кода | Провизия файла |
|---|---|---|---|
| **Дефолт (базовая защита)** | пусто / не задано | **НЕ** передаёт `--security-opt seccomp=...` → действует встроенный default seccomp Docker (syscall-фильтр работает) | **не требуется** |
| **Ужесточение (опционально)** | путь к кастомному JSON-профилю (напр. `/etc/lovable/seccomp/build.json`) | передаёт `--security-opt seccomp={BUILD_SECCOMP_PROFILE}` | **devops** провижит файл по этому пути на build-хосте/в образе worker |

**Обоснование выбора (built-in default, а не обязательный файл в репозитории):** базовую защиту даёт уже встроенный Docker default seccomp — он активен без передачи флага, поэтому требовать провизию файла для базового кейса избыточно (лишний артефакт + риск рассинхронизации пути). Кастомный ужесточённый профиль — **опциональное** усиление: devops провижит JSON по пути из `BUILD_SECCOMP_PROFILE`, build-код условно подставляет флаг. Это убирает хардкод-константу профиля в коде (backend ранее был вынужден захардкодить несуществующий токен `default.json`) и не вводит обязательной провизии для базового сценария.

- **Семантика build-кода (нормативно):** `if settings.build_seccomp_profile: docker_args += ["--security-opt", f"seccomp={settings.build_seccomp_profile}"]` — пустое значение → флаг **отсутствует** в `argv`. Хардкод-константа (`_SECCOMP_PROFILE='default.json'`) **запрещена** — заменяется на `settings.build_seccomp_profile`.
- Env-ключ `BUILD_SECCOMP_PROFILE` (имя/тип/дефолт/потребитель) — [07-deployment.md → env-контракт](../07-deployment.md#канонический-список-ключей) (single normative source имён ключей).
- `unconfined` через этот ключ **не предусмотрен** контрактом (отключение seccomp ослабляет песочницу недоверенного кода); потребность в нём — через новый ADR.

#### B-2. Writable HOME + tool-caches под read-only rootfs (нормативная, прод-фикс 2026-06-04)

**Проблема (прод-инцидент).** Build-контейнер бежит non-root (`--user 10001:10001`) c `--read-only` rootfs. У non-root-пользователя `HOME` в образе не задан / резолвится в `/`, поэтому npm (а далее vite/esbuild) пытается создать кэш в `/.npm` (= `$HOME/.npm`) на **read-only rootfs** → `npm error code ENOENT … mkdir '/.npm'`, сборка падает **до** реального `vite build`. Та же писучесть HOME/XDG-cache нужна vite (`node_modules/.vite`, esbuild-кэш) и любому инструменту, пишущему в HOME/XDG.

**Решение — единственная writable-точка на диске уже есть: `/workspace`.** `/workspace` (= `{builds_root}/{job_id}`) монтируется RW, лежит на диске под дисковой квотой workspace и эфемерен (сносится после сборки). `/tmp` — tmpfs в памяти, размер ограничен (`256m`) и конкурирует с `BUILD_MEM_LIMIT`; полный `npm ci` тянет кэш в сотни МБ, поэтому **tool-cache размещается в `/workspace` на диске, не в tmpfs `/tmp`** (см. §B-3). HOME и cache-директории инструментов задаются env на подкаталоги `/workspace`:

| Env (внутри контейнера) | Значение (нормативно) | Назначение |
|---|---|---|
| `HOME` | `/workspace/.home` | Писучий HOME; устраняет резолв `$HOME`→`/`. По умолчанию npm-кэш = `$HOME/.npm` уже стал бы писучим, но кэш фиксируется явно (ниже) и **на диске**. |
| `npm_config_cache` | `/workspace/.npm` | npm cache (`npm ci`) на диск-workspace, не в `/.npm` на rootfs и не в tmpfs. Имя ключа npm регистронезависимо; `NPM_CONFIG_CACHE` эквивалентен. |
| `XDG_CACHE_HOME` | `/workspace/.cache` | XDG-кэш (vite/esbuild и пр. tooling) на диск-workspace. |
| `XDG_CONFIG_HOME` | `/workspace/.config` | XDG-конфиг tooling на диск-workspace (не пишется в read-only HOME/rootfs). |

- **Семантика build-кода (нормативно):** `_build_argv` ОБЯЗАН добавить `-e HOME=/workspace/.home -e npm_config_cache=/workspace/.npm -e XDG_CACHE_HOME=/workspace/.cache -e XDG_CONFIG_HOME=/workspace/.config` (значения — константы пути внутри контейнера, не env-хоста: они фиксируют расположение **внутри** `/workspace`, который и так RW). Каталоги создаются самими инструментами (npm/vite mkdir-ят cache при первом запуске); HOME-подкаталог npm также создаёт при необходимости. Доп. `--tmpfs`/mount под HOME/cache **не нужен** — `/workspace` уже writable.
- **Инварианты НЕ ослаблены:** `--read-only` rootfs сохраняется; `--user` non-root сохраняется; cap-drop/no-new-privileges/seccomp/pids/cpu/mem/egress — без изменений. Писучесть достигается **только** перенаправлением HOME/cache в уже-смонтированный RW `/workspace`, без снятия `--read-only` и без root. Кэш эфемерен вместе с workspace (cleanup после сборки), supply-chain-граница не меняется: npm всё равно ходит в registry только через egress-proxy (§C).
- **vite/esbuild:** при `HOME`+`XDG_CACHE_HOME` на `/workspace` их кэши (`node_modules/.vite`, esbuild) тоже попадают в writable workspace; отдельных env для них не требуется (они уважают HOME/XDG и пишут рядом с `node_modules`, который и так в `/workspace`).
- **Приёмочный критерий регрессии (проверяем qa, [06-testing-strategy.md → Sprint 4](../06-testing-strategy.md#покрытие-по-спринтам-dod-привязка)):** build-контейнер под `--read-only` + non-root (`--user 10001:10001`) завершает `npm ci` **без** `ENOENT … mkdir '/.npm'`; cache-каталоги создаются под `/workspace/.npm`|`.cache`|`.config`|`.home`, **не** на rootfs (`/.npm` отсутствует). Автоматизируемо как unit на `_build_argv` (наличие `-e HOME/npm_config_cache/XDG_CACHE_HOME/XDG_CONFIG_HOME` с дословными значениями) + live-приёмка реального `npm ci` под rootless+read-only (открытый приёмочный пункт, живой стек).

#### B-3. tmpfs sizing vs cache-on-disk (нормативно)

- Tool-cache (npm/vite/esbuild) размещается в **`/workspace` на диске** (под дисковой квотой workspace), а **не** в tmpfs `/tmp` — `npm ci` для нетривиального дерева раздувает кэш до сотен МБ, что (1) переполнило бы tmpfs `/tmp:size=256m` и (2) расходовало бы RAM против `BUILD_MEM_LIMIT` (tmpfs живёт в памяти контейнера). `/tmp` остаётся как был — эфемерный scratch для коротких temp-файлов npm/инструментов (`--tmpfs /tmp:rw,size=256m`), его размер **не** требуется увеличивать под кэш.
- Следствие для квот: дисковая квота workspace должна покрывать `node_modules` + `dist` + tool-cache (`.npm`/`.cache`). Это не новый лимит — workspace-квота ADR-010 уже покрывает эфемерное дерево сборки; кэш живёт и сносится вместе с ним.

Workspace (`{builds_root}/{job_id}`) — эфемерный, очищается после сборки (успех/фейл), с дисковой квотой. Это сохраняет инварианты S1 (cleanup `/var/builds/{job_id}`), усиливая runtime и сеть.

> **Провижининг host-каталога `BUILDS_ROOT` + path-consistency (прод-фикс 2026-06-04).** Bind-source `-v {builds_root}/{job_id}:/workspace` резолвит **rootless-демон относительно ФС хоста**, где он работает, а не ФС worker-контейнера. Поэтому host-каталог `BUILDS_ROOT` обязан существовать **до** старта worker, быть bind-смонтирован в worker по **идентичному абсолютному пути** (`-v ${BUILDS_ROOT}:${BUILDS_ROOT}`, не именованный volume) и иметь ownership «worker uid 10001 пишет / rootless-демон читает». Нормативная топология провижининга (где живёт, кто создаёт, права) — [07-deployment.md → Провижининг build-workspace](../07-deployment.md#провижининг-build-workspace-и-sites-каталога-host-bind-path-consistency--прод-фикс-2026-06-04). Прод-инцидент: первый реальный build упал на `[Errno 13] Permission denied: '/var/builds'` — каталог отсутствовал и не провижился.

### C. Egress-allowlist (только npm-registry)

- Build-контейнер подключается к **отдельной Docker-сети** (`BUILD_EGRESS_NETWORK`, в dev `internal: true`), из которой **нет прямого выхода в интернет и нет доступа к внутренней сети** (Postgres/Redis/MinIO/Traefik) и к cloud-metadata (`169.254.169.254`) / private CIDR. **Следствие:** прямого сетевого маршрута к npm-registry у build-контейнера НЕТ — он обязан ходить туда **только** через egress-proxy.
- Единственный разрешённый исходящий путь — к **egress-proxy** (forward-proxy в этой сети), который пропускает запросы **только** к хостам из `NPM_REGISTRY_ALLOWLIST` (дефолт: `registry.npmjs.org` + при необходимости CDN `*.npmjs.org`). Всё прочее — DROP/`403`.

#### C-1. Две стороны одного механизма (нормативно)

Egress-allowlist состоит из двух согласованных сторон — обе обязательны, одна без другой не работает:

| Сторона | Параметр | Где живёт | Назначение |
|---|---|---|---|
| **Registry-allowlist** | `NPM_REGISTRY_ALLOWLIST` | конфиг egress-proxy (squid) | какие хосты proxy пропускает дальше; всё прочее — DROP/`403`. |
| **Proxy-URL (транспорт)** | `BUILD_EGRESS_PROXY_URL` | инжектится воркером в build-контейнер | как `npm ci`/Node находят proxy и через него достигают registry (прямого маршрута нет). |

- **Механизм инъекции proxy (нормативно):** воркер при запуске build-контейнера передаёт `BUILD_EGRESS_PROXY_URL` как переменные окружения `http_proxy` и `https_proxy` (`docker run -e http_proxy={BUILD_EGRESS_PROXY_URL} -e https_proxy={BUILD_EGRESS_PROXY_URL} …`). Это **стандартный** для npm и Node (undici/fetch) способ задать forward-proxy — не требует генерации файлов и работает для всего исходящего HTTP(S)-трафика сборки. Регистронезависимые дубликаты (`HTTP_PROXY`/`HTTPS_PROXY`) допустимы, но не обязательны.
- **Registry-pointing (`npm_config_registry` / `.npmrc`) — отдельная ось:** указывает npm на **хост** registry (из allowlist); инжектится воркером, **не** из LLM-дерева (запрещённый dotfile по [Q-PIPELINE-1](../99-open-questions.md#q-pipeline-1)). Это НЕ транспорт: маршрут к этому хосту обеспечивает именно `http_proxy`/`https_proxy`. **`.npmrc` НЕ используется для задания proxy** (proxy задаётся env — единый механизм для npm и Node).
- **Нормативное требование к `_build_argv` (backend):** при непустом `BUILD_EGRESS_NETWORK` build-контейнер ОБЯЗАН получить proxy-конфигурацию — `_build_argv` добавляет `-e http_proxy={settings.build_egress_proxy_url}` и `-e https_proxy={settings.build_egress_proxy_url}`. Псевдокод: `if settings.build_egress_network: argv += ["-e", f"http_proxy={settings.build_egress_proxy_url}", "-e", f"https_proxy={settings.build_egress_proxy_url}"]`. Пропуск инъекции при непустой egress-сети = build без маршрута к registry → `npm ci` падает под реальной S4-сетью (баг, не опция).
- Это предотвращает exfiltration/SSRF/атаку на внутреннюю сеть из postinstall-скриптов произвольных пакетов ([Q-DEPLOY-1](../99-open-questions.md#q-deploy-1)).

#### C-2. Source-ACL build-клиентов: мульти-инстанс (нормативно, прод-фикс 2026-06-04)

**Проблема (прод-инцидент при деплое клона `nexoraweb`).** `infra/egress-proxy/squid.conf` содержит `acl build_clients src 172.31.0.0/16` — захардкоженную build-egress-подсеть `corelysite`. Параметризация IPAM-пула `build_egress` через `BUILD_EGRESS_SUBNET` ([07-deployment.md → env-контракт](../07-deployment.md#канонический-список-ключей)) была введена для изоляции пулов между инстансами (клон задаёт свободный /16, напр. `172.30.0.0/16`, чтобы избежать `Pool overlaps`), но **зависящий от неё** src-ACL squid параметризован **не был** (упущение при вводе `BUILD_EGRESS_SUBNET`). `squid.conf` статически монтируется из репозитория одним файлом для **обоих** инстансов (`./egress-proxy/squid.conf:/etc/squid/squid.conf:ro`), envsubst не применяется. Build-контейнер клона (`src 172.30.0.4`) **не входит** в `172.31.0.0/16` → squid `TCP_DENIED/403 CONNECT registry.npmjs.org:443` → `npm error E403` → сборка падает (`no_progress`). На `corelysite` (172.31) работает. Это docs↔конфиг-рассогласование: одна сторона мульти-инстанс-параметризации (`ipam.subnet`) ушла в env, парная (`src`-ACL) — осталась захардкоженной.

**Решение (нормативно): `acl build_clients src 172.16.0.0/12`** — единый CIDR, покрывающий **весь** RFC1918-диапазон Docker-bridge подсетей (`172.16.0.0` – `172.31.255.255`), в т.ч. и `172.31` (corelysite), и `172.30` (nexoraweb), и любой будущий build-egress /16 инстанса в этом диапазоне. Статический конфиг остаётся **одним** файлом для всех инстансов, envsubst/шаблон/новый env-ключ **не вводятся**.

> **⚠️ РЕВИЗОВАНО [ADR-033](ADR-033-build-egress-ipam-exhaustion-rfc1918-10-block.md) (2026-06-16):** допущение «любой будущий build-egress инстанса ⊂ `172.16.0.0/12`» **более невыполнимо** — на shared-хосте все 16 /16-подсетей `172.16.0.0/12` исчерпаны (блокер деплоя 5-го инстанса `orvianix`). build-egress 5-го и последующих инстансов выносится в выделенный `10.201.0.0/16` (`BUILD_EGRESS_SUBNET=10.201.<N>.0/24`, N=порядковый номер инстанса), а src-ACL получает **вторую** строку `acl build_clients src 10.201.0.0/16` (объединение с `172.16.0.0/12`). 4 живых инстанса (`172.16/29/30/31`) остаются в /12 (backward-compat). Узкий `10.201.0.0/16` (не весь `10.0.0.0/8`) + anti-SSRF `to_private dst 10.0.0.0/8` без изменений. Актуальный нормативный текст ACL и правило subnet — [ADR-033](ADR-033-build-egress-ipam-exhaustion-rfc1918-10-block.md) + [07-deployment.md → egress-proxy ACL](../07-deployment.md#egress-proxy-acl-build-клиентов-мульти-инстанс-нормативно--прод-фикс-2026-06-04).

**Почему безопасность не страдает (threat-model).** Src-ACL `build_clients` в threat-model ADR-010 §C — это **defence-in-depth поверх сетевой изоляции**, а не первичный контроль. Первичная граница — сетевая: build-контейнер сидит **только** в `build_egress` (`internal: true`, нет маршрута наружу кроме как через сам proxy), а egress-proxy слушает `3128` **только** на `build_egress` + `egress_uplink` и наружу порт **не публикует** (`docker-compose.prod.yml` сервис `egress-proxy`: нет `ports:`). Следовательно **физически** достучаться до `egress-proxy:3128` могут **исключительно** build-контейнеры данного инстанса на его собственной `build_egress`-сети — независимо от ширины src-ACL. Семантика контроля — «**только** build-контейнеры», а **не** «конкретная подсеть»; расширение ACL до `172.16.0.0/12` оставляет **эффективное** множество источников неизменным (те же build-контейнеры инстанса), потому что недостижимые источники отсечены сетевой изоляцией ещё до ACL. Точность «ACL = ровно подсеть инстанса» заменяется сетевой изоляцией (первичный слой) + комментарием в `squid.conf`, фиксирующим это обоснование.

**Обратная совместимость.** `172.31.0.0/16` (corelysite) ⊂ `172.16.0.0/12` → редеплой `corelysite` после правки конфига ничего не меняет (его build-клиенты по-прежнему проходят ACL). Клон получает доступ автоматически (его /16 ⊂ /12).

**Не конфликтует с anti-SSRF.** `acl to_private dst 172.16.0.0/12` + `http_access deny to_private` остаются без изменений: это **`dst`** (запрет *адресата* в приватную сеть, анти-DNS-rebind/SSRF), тогда как `build_clients` — **`src`** (источник запроса). Расширение `src`-ACL не ослабляет `dst`-фильтр.

**Вариант B (envsubst per-instance, отвергнут).** Альтернатива — шаблонизировать `squid.conf` и подставлять `SQUID_BUILD_CLIENTS_CIDR=${BUILD_EGRESS_SUBNET}` через envsubst в entrypoint egress-proxy → ACL точно = подсеть инстанса. Отвергнута по принципу простоты: требует кастомного образа или override entrypoint поверх pinned `ubuntu/squid` (envsubst-шаг + шаблон + новый env-ключ), ради точности ACL, **дублирующей** уже существующую сетевую изоляцию (первичный контроль). Выигрыша в реальной границе нет — отвергнута; зарезервирована как ужесточение через новый ADR, если появится сценарий, где сетевая изоляция перестаёт быть достаточной.

**Граница egress-политики:** lockdown применяется **исключительно к build-песочнице**. Доверенные application-процессы (FastAPI web, Celery worker/beat) **не** ограничиваются — их исходящий трафик к Anthropic API, Adapty `getProfile`, Apple JWKS остаётся разрешённым. Нормативный источник границы — [05-security.md → «Граница egress-политики»](../05-security.md#граница-egress-политики-build-sandbox-vs-application-процессы-требование-к-sprint-4) (single normative source). Egress-proxy/allowlist-сеть навешиваются на build-контейнер, а не на воркер.

### D. Реализуемость dev (compose) vs prod

- **Dev (compose):** build-egress-сеть моделируется как Docker-сеть `internal: true` + egress-proxy-сервис (allowlist к npm-registry). Rootless Docker в dev — внутри WSL2-Linux (build не на голом Windows, [07-deployment.md → Windows-dev](../07-deployment.md#windows-dev-специфика)); допустимо запускать build-контейнеры через rootless-демон внутри WSL2. `BUILD_SANDBOX_RUNTIME=rootless`.
- **Prod:** build-воркеры — на **отдельных build-хостах** с rootless Docker ([07-deployment.md → Прод-топология](../07-deployment.md#прод-топология)); egress-сеть + proxy/firewall-allowlist на уровне хоста/сети. Application-хосты (api/llm-worker/beat) от build-хостов отделены, их egress не lockdown-ится.

## Consequences

**Плюсы:** снят корневой риск [TD-001](../100-known-tech-debt.md#td-001) (root-эскалация через `docker.sock`); supply-chain exfiltration/SSRF закрыты egress-allowlist; конфигурация запуска зафиксирована нормативно (девопс/бэкенд не угадывают); dev≈prod (одинаковая модель сети+runtime); параметризация через env позволяет наложить gVisor поверх без смены контракта; seccomp параметризован env (`BUILD_SECCOMP_PROFILE`), базовая защита через встроенный Docker default (без обязательной провизии файла), ужесточённый профиль — опциональная провизия devops (§B-1) — устраняет хардкод несуществующего токена `default.json` в build-коде; writable HOME/tool-cache под read-only rootfs зафиксированы перенаправлением `HOME`/`npm_config_cache`/`XDG_*` в уже-RW `/workspace` (§B-2/§B-3) — `npm ci`/`vite build` пишут кэш на диск-workspace без снятия `--read-only` и без root (прод-фикс ENOENT `/.npm`); src-ACL build-клиентов squid расширен до `172.16.0.0/12` (§C-2) — допускает build-контейнеры **любого** инстанса (его `BUILD_EGRESS_SUBNET` ⊂ /12) при сохранении реальной границы (сетевая изоляция = первичный контроль), без шаблона/envsubst (прод-фикс `TCP_DENIED/403` для клона `nexoraweb`).

**Минусы:** rootless Docker имеет известные ограничения (overlayfs/cgroup nuances, производительность сети) — приемлемо для CPU-bound build; egress-proxy — доп. компонент (образ + сеть), заведён в [02-tech-stack.md](../02-tech-stack.md); тесты sandbox-escape/egress-блокировки автоматизируемы лишь частично ([06-testing-strategy.md](../06-testing-strategy.md), S4).

## Alternatives

- **Оставить shared `docker.sock` (S1).** Отвергнута: не закрывает [TD-001](../100-known-tech-debt.md#td-001) (root-эскалация), нет egress-контроля.
- **gVisor/`runsc` как основной runtime.** Не выбран как дефолт ([08 §4-3](../08-product-decisions.md#sprint-4--sandbox--security) фиксирует rootless): rootless проще операционно и достаточен совместно с egress-allowlist + cap-drop/seccomp. gVisor **зарезервирован** как опциональное усиление (`BUILD_SANDBOX_RUNTIME=runsc`) без изменения остального контракта запуска.
- **DinD (Docker-in-Docker, privileged).** Отвергнута: `--privileged` DinD усиливает, а не ослабляет риск хоста; противоречит цели снять root-эскалацию.
- **Egress без proxy, чистый iptables-DROP всего кроме IP npm.** Отвергнута как единственный механизм: IP npm-CDN динамичны; hostname-allowlist через forward-proxy надёжнее. iptables-изоляция (private CIDR/metadata DROP) — дополняющий слой, не замена proxy.

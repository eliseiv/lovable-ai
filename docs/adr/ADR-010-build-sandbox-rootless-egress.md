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
| `-v {workspace}:/workspace` | rw, **только** workspace | Единственная writable-точка (исходники + `node_modules` + `dist`). |
| `--user {non-root UID}` | напр. `10001:10001` | Non-root внутри контейнера (поверх user-namespace remap rootless). |
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

**Граница egress-политики:** lockdown применяется **исключительно к build-песочнице**. Доверенные application-процессы (FastAPI web, Celery worker/beat) **не** ограничиваются — их исходящий трафик к Anthropic API, Adapty `getProfile`, Apple JWKS остаётся разрешённым. Нормативный источник границы — [05-security.md → «Граница egress-политики»](../05-security.md#граница-egress-политики-build-sandbox-vs-application-процессы-требование-к-sprint-4) (single normative source). Egress-proxy/allowlist-сеть навешиваются на build-контейнер, а не на воркер.

### D. Реализуемость dev (compose) vs prod

- **Dev (compose):** build-egress-сеть моделируется как Docker-сеть `internal: true` + egress-proxy-сервис (allowlist к npm-registry). Rootless Docker в dev — внутри WSL2-Linux (build не на голом Windows, [07-deployment.md → Windows-dev](../07-deployment.md#windows-dev-специфика)); допустимо запускать build-контейнеры через rootless-демон внутри WSL2. `BUILD_SANDBOX_RUNTIME=rootless`.
- **Prod:** build-воркеры — на **отдельных build-хостах** с rootless Docker ([07-deployment.md → Прод-топология](../07-deployment.md#прод-топология)); egress-сеть + proxy/firewall-allowlist на уровне хоста/сети. Application-хосты (api/llm-worker/beat) от build-хостов отделены, их egress не lockdown-ится.

## Consequences

**Плюсы:** снят корневой риск [TD-001](../100-known-tech-debt.md#td-001) (root-эскалация через `docker.sock`); supply-chain exfiltration/SSRF закрыты egress-allowlist; конфигурация запуска зафиксирована нормативно (девопс/бэкенд не угадывают); dev≈prod (одинаковая модель сети+runtime); параметризация через env позволяет наложить gVisor поверх без смены контракта; seccomp параметризован env (`BUILD_SECCOMP_PROFILE`), базовая защита через встроенный Docker default (без обязательной провизии файла), ужесточённый профиль — опциональная провизия devops (§B-1) — устраняет хардкод несуществующего токена `default.json` в build-коде.

**Минусы:** rootless Docker имеет известные ограничения (overlayfs/cgroup nuances, производительность сети) — приемлемо для CPU-bound build; egress-proxy — доп. компонент (образ + сеть), заведён в [02-tech-stack.md](../02-tech-stack.md); тесты sandbox-escape/egress-блокировки автоматизируемы лишь частично ([06-testing-strategy.md](../06-testing-strategy.md), S4).

## Alternatives

- **Оставить shared `docker.sock` (S1).** Отвергнута: не закрывает [TD-001](../100-known-tech-debt.md#td-001) (root-эскалация), нет egress-контроля.
- **gVisor/`runsc` как основной runtime.** Не выбран как дефолт ([08 §4-3](../08-product-decisions.md#sprint-4--sandbox--security) фиксирует rootless): rootless проще операционно и достаточен совместно с egress-allowlist + cap-drop/seccomp. gVisor **зарезервирован** как опциональное усиление (`BUILD_SANDBOX_RUNTIME=runsc`) без изменения остального контракта запуска.
- **DinD (Docker-in-Docker, privileged).** Отвергнута: `--privileged` DinD усиливает, а не ослабляет риск хоста; противоречит цели снять root-эскалацию.
- **Egress без proxy, чистый iptables-DROP всего кроме IP npm.** Отвергнута как единственный механизм: IP npm-CDN динамичны; hostname-allowlist через forward-proxy надёжнее. iptables-изоляция (private CIDR/metadata DROP) — дополняющий слой, не замена proxy.

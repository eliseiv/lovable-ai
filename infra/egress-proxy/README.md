# infra/egress-proxy/ — egress-allowlist build-песочницы (Sprint 4)

Forward-proxy, через который **только** build-контейнер недоверенного LLM-кода
(`npm ci && vite build`) выходит наружу — **исключительно** к npm-registry.

Источник истины:
- [`docs/adr/ADR-010-build-sandbox-rootless-egress.md`](../../docs/adr/ADR-010-build-sandbox-rootless-egress.md) §C
- [`docs/05-security.md`](../../docs/05-security.md) → threat-model (supply-chain / Egress·SSRF), «Граница egress-политики»
- [`docs/07-deployment.md`](../../docs/07-deployment.md) → env-контракт (`NPM_REGISTRY_ALLOWLIST`, `BUILD_EGRESS_NETWORK`)

## Образ

`ubuntu/squid:6.6-24.04_edge` (Canonical-maintained, pinned). Ранее был
`6.6-24.04_stable`, но Canonical удалил `_stable`-канал ubuntu/squid из Docker Hub
(`not found`); `_edge` той же ветки `6.6-24.04` — единственный поддерживаемый
pinned-тег (squid 6.14, НЕ floating `latest`/`edge`). `squid.conf` приведён к
совместимости с squid 6.13+ (убрана устаревшая `dns_v4_first`; дедуплицирован
`dstdomain` — `.npmjs.org` уже покрывает `registry.npmjs.org`, дубль теперь FATAL).
Squid выбран как
forward-proxy с hostname-allowlist (`dstdomain`) — ADR-010 §C: hostname-allowlist
надёжнее чистого iptables-DROP по IP (CDN npm динамичны). iptables-DROP private
CIDR/metadata — дополняющий слой (здесь продублирован ACL-ами `to_private`/
`to_metadata`).

## Что разрешено / запрещено

| Назначение | Политика |
|---|---|
| `registry.npmjs.org`, `*.npmjs.org` (CDN) | ALLOW (только из build-egress подсети `src 172.16.0.0/12`) |
| `169.254.169.254` (cloud-metadata) | DENY (раньше allow — анти-DNS-rebind) |
| private CIDR (10/8, 172.16/12, 192.168/16, 127/8, 169.254/16) | DENY (это `dst`-фильтр адресата, не `src`) |
| всё прочее | DENY (default-deny `http_access deny all`) |

Хосты allowlist в `squid.conf` (`acl npm_registry dstdomain ...`) должны
соответствовать `NPM_REGISTRY_ALLOWLIST` из `.env` (дефолт `registry.npmjs.org`).
При расширении allowlist — менять обе точки синхронно.

**Src-ACL build-клиентов (`acl build_clients src 172.16.0.0/12`) — мульти-инстанс.**
Покрывает весь Docker-bridge диапазон RFC1918, поэтому допускает build-контейнеры
**любого** инстанса (его `BUILD_EGRESS_SUBNET` — напр. `172.31` corelysite, `172.30`
nexoraweb — ⊂ `172.16/12`) **без** правки конфига/envsubst. Это defence-in-depth
поверх **первичной** сетевой изоляции (proxy слушает 3128 только в `build_egress`,
наружу порт не публикуется → достучаться могут лишь build-контейнеры данного
инстанса). Нормативное обоснование и обратная совместимость —
[`ADR-010 §C-2`](../../docs/adr/ADR-010-build-sandbox-rootless-egress.md) и
[`docs/07-deployment.md → egress-proxy ACL build-клиентов`](../../docs/07-deployment.md).
`src`-ACL (источник) ≠ `dst to_private 172.16/12` (адресат, анти-SSRF) — это
разные оси, расширение `src` не ослабляет `dst`-фильтр.

## Сеть

`egress-proxy` подключён к двум сетям:
- `build_egress` (`internal: true`) — здесь его видит build-контейнер; выхода
  в интернет/внутреннюю сеть у этой сети нет;
- `egress_uplink` (обычная bridge) — даёт самому proxy выход в интернет к npm.

Build-контейнер сидит **только** в `build_egress` → его единственный путь
наружу — этот proxy. Воркер инжектит build-контейнеру `http_proxy`/`https_proxy`
+ `npm_config_registry` на `http://egress-proxy:3128` и `.npmrc` (НЕ из
LLM-дерева, [Q-PIPELINE-1]).

## Логирование и healthcheck

Образ `ubuntu/squid` сбрасывает привилегии до user `proxy` (uid 13). Поэтому
squid пишет логи в **штатные файлы** `/var/log/squid/access.log` и
`/var/log/squid/cache.log` (каталог owned `proxy` → writable), а в `docker logs`
их выносит **сам образ**: его `entrypoint.sh` держит `tail -F` этих файлов в
фоне и форвардит на stdout контейнера. Прямые `access_log stdio:/dev/stdout` /
`cache_log stdio:/dev/stderr` в `squid.conf` **запрещены** — `/dev/stdout|stderr`
принадлежат root, и после privilege-drop squid падает
`FATAL: Cannot open '/dev/stdout' for writing`.

Healthcheck сервиса `egress-proxy` — **реальная** проверка: процесс squid жив
(`pidof squid`) **и** реально слушает `3128` (TCP-проба через `bash /dev/tcp`,
т.к. в образе нет `nc`/`ss`/`netstat`/`curl`/`squidclient`). Прежний
`squid -k parse` валидировал только синтаксис конфига и был ложно-зелёным
(контейнер мог крэшить в цикле, оставаясь «healthy», и впустую разблокировал
`worker` через `depends_on: service_healthy`).

## Граница (важно)

Lockdown — **только** на build-песочнице. `api`/`worker`/`beat` через этот proxy
**не** ходят: их исходящий трафик к Anthropic / Adapty `getProfile` / Apple JWKS
остаётся прямым (docs/05-security.md → «Граница egress-политики»).

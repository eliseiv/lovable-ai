# App Store trusted roots (`APPSTORE_ROOT_CERT_DIR`, ADR-039)

Каталог доверенных root-сертификатов для верификации x5c-цепочки прямых StoreKit 2
JWS-транзакций (`app/billing/storekit.py`). Верификатор загружает все сертификаты каталога
(`*.cer` / `*.der` / `*.pem` / `*.crt`); цепочка транзакции обязана терминироваться в одном из
них. Пустой/несконфигурированный каталог → верификатор отказывает (`422` fail-closed) — начисление
не производится.

## Состав (per-instance безопасность — нормативно, docs/07-deployment.md §Прямой StoreKit-путь)

- **`AppleRootCA-G3.cer`** — Apple production root (DER), публичный, коммитится в git; присутствует
  на **всех** инстансах.
- **Xcode StoreKit-Testing сертификат** (`StoreKitTestCertificate.cer`) — **НЕ** в этом каталоге на
  prod. На prod-инстансе каталог содержит **только** Apple production root. Xcode-тест-сертификат
  монтируется в `APPSTORE_ROOT_CERT_DIR` **только** на тест/dev-инстансах (иначе кто угодно
  self-sign'ит Xcode-JWS локальным тест-сертификатом → бесплатные токены/pro).

Провизия/монтирование per-instance (prod = только Apple root + реальный `APPSTORE_BUNDLE_ID`) —
зона devops (docs/07-deployment.md → Правило конфиг-артефакта: App Store roots).

#!/bin/bash
cd /opt/corelysite
SEED=$(grep '^SEED_API_KEY=' .env | cut -d= -f2)
echo "=== raw POST /v1/projects ==="
curl -s -X POST https://corelysite.shop/v1/projects -H "Authorization: Bearer $SEED" -H 'Content-Type: application/json' -H 'Idempotency-Key: fmt-probe-1' -d '{"prompt":"format probe"}'
echo ""
echo "=== raw GET /v1/projects (list) ==="
curl -s https://corelysite.shop/v1/projects -H "Authorization: Bearer $SEED" | head -c 400
echo ""
echo "=== jq? ==="; which jq || echo no-jq
echo "=== python? ==="; docker exec corelysite-api-1 python -c "print('py-ok')" 2>/dev/null || echo no-py

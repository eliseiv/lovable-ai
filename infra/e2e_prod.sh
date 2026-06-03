#!/bin/bash
# Full E2E probe vs prod https://corelysite.shop (no ANTHROPIC -> generation pipeline not exercised).
BASE="https://corelysite.shop"
cd /opt/corelysite
SEED=$(grep '^SEED_API_KEY=' .env | cut -d= -f2)
ADW=$(grep '^ADAPTY_WEBHOOK_SECRET=' .env | cut -d= -f2)
AUTHH="Authorization: Bearer $SEED"
CT="Content-Type: application/json"
PASS=0; FAIL=0
chk() { if [ "$2" = "$3" ]; then echo "  PASS  $1 -> $3"; PASS=$((PASS+1)); else echo "  FAIL  $1 expected=$2 got=$3"; FAIL=$((FAIL+1)); fi; }

echo "===== cleanup existing projects via API (not a DB wipe) ====="
for pid in $(curl -s $BASE/v1/projects -H "$AUTHH" | grep -o '"id":"p_[^"]*"' | sed 's/"id":"//;s/"//'); do
  curl -s -o /dev/null -X DELETE $BASE/v1/projects/$pid -H "$AUTHH"
  echo "  deleted $pid"
done
sleep 2

echo "===== 1. Infra ====="
chk "GET /healthz" 200 "$(curl -s -o /dev/null -w '%{http_code}' $BASE/healthz)"
chk "GET /readyz" 200 "$(curl -s -o /dev/null -w '%{http_code}' $BASE/readyz)"
chk "GET /docs" 200 "$(curl -s -o /dev/null -w '%{http_code}' $BASE/docs)"
chk "GET /openapi.json" 200 "$(curl -s -o /dev/null -w '%{http_code}' $BASE/openapi.json)"

echo "===== 2. Auth ====="
chk "POST /v1/projects no-bearer" 401 "$(curl -s -o /dev/null -w '%{http_code}' -X POST $BASE/v1/projects -H "$CT" -H 'Idempotency-Key: e1' -d '{"prompt":"x"}')"
chk "POST /v1/projects bad-bearer" 401 "$(curl -s -o /dev/null -w '%{http_code}' -X POST $BASE/v1/projects -H 'Authorization: Bearer lv_bad_bad' -H "$CT" -H 'Idempotency-Key: e2' -d '{"prompt":"x"}')"
chk "GET /v1/projects no-bearer" 401 "$(curl -s -o /dev/null -w '%{http_code}' $BASE/v1/projects)"
chk "POST /v1/auth/apple empty" 422 "$(curl -s -o /dev/null -w '%{http_code}' -X POST $BASE/v1/auth/apple -H "$CT" -d '{}')"

echo "===== 3. Projects / idempotency ====="
chk "POST /v1/projects no Idempotency-Key" 422 "$(curl -s -o /dev/null -w '%{http_code}' -X POST $BASE/v1/projects -H "$AUTHH" -H "$CT" -d '{"prompt":"e2e site"}')"
R1=$(curl -s -X POST $BASE/v1/projects -H "$AUTHH" -H "$CT" -H 'Idempotency-Key: e2e-A' -d '{"prompt":"e2e portfolio"}')
JOB1=$(echo "$R1" | sed -n 's/.*"job_id":"\([^"]*\)".*/\1/p')
PID1=$(echo "$R1" | sed -n 's/.*"project_id":"\([^"]*\)".*/\1/p')
echo "  -> project=$PID1 job=$JOB1"
chk "POST /v1/projects creates job" yes "$([ -n "$JOB1" ] && echo yes || echo no)"
JOB2=$(curl -s -X POST $BASE/v1/projects -H "$AUTHH" -H "$CT" -H 'Idempotency-Key: e2e-A' -d '{"prompt":"e2e portfolio"}' | sed -n 's/.*"job_id":"\([^"]*\)".*/\1/p')
chk "idempotent replay same job" "$JOB1" "$JOB2"
chk "GET /v1/projects list" 200 "$(curl -s -o /dev/null -w '%{http_code}' $BASE/v1/projects -H "$AUTHH")"
chk "GET /v1/projects/{pid}" 200 "$(curl -sL -o /dev/null -w '%{http_code}' $BASE/v1/projects/$PID1 -H "$AUTHH")"
chk "GET /v1/projects/{bad}" 404 "$(curl -sL -o /dev/null -w '%{http_code}' $BASE/v1/projects/p_nonexistent -H "$AUTHH")"

echo "===== 4. Jobs / SSE ====="
chk "GET /v1/jobs/{jid}" 200 "$(curl -sL -o /dev/null -w '%{http_code}' $BASE/v1/jobs/$JOB1 -H "$AUTHH")"
chk "GET /v1/jobs/{bad}" 404 "$(curl -sL -o /dev/null -w '%{http_code}' $BASE/v1/jobs/j_nonexistent -H "$AUTHH")"
echo "  job state (no ANTHROPIC): $(curl -s $BASE/v1/jobs/$JOB1 -H "$AUTHH" | sed -n 's/.*"state":"\([^"]*\)".*/\1/p')"
SSE=$(curl -s -m 4 -N $BASE/v1/jobs/$JOB1/events -H "$AUTHH" | head -c 120)
chk "SSE emits frames" yes "$([ -n "$SSE" ] && echo yes || echo no)"
chk "SSE no-auth" 401 "$(curl -s -o /dev/null -w '%{http_code}' $BASE/v1/jobs/$JOB1/events)"

echo "===== 5. Billing ====="
chk "webhook no-sig" 401 "$(curl -s -o /dev/null -w '%{http_code}' -X POST $BASE/v1/billing/webhook/adapty -H "$CT" -d '{"event":"x"}')"
BODY='{"event_type":"subscription_started","event_id":"e2e-evt-1","customer_user_id":"nobody","profile":{}}'
SIG=$(printf '%s' "$BODY" | openssl dgst -sha256 -hmac "$ADW" -r | cut -d' ' -f1)
chk "webhook valid HMAC" 200 "$(curl -s -o /dev/null -w '%{http_code}' -X POST $BASE/v1/billing/webhook/adapty -H "$CT" -H "adapty-signature: $SIG" -d "$BODY")"
chk "webhook replay idempotent" 200 "$(curl -s -o /dev/null -w '%{http_code}' -X POST $BASE/v1/billing/webhook/adapty -H "$CT" -H "adapty-signature: $SIG" -d "$BODY")"
chk "GET /v1/billing/me" 200 "$(curl -s -o /dev/null -w '%{http_code}' $BASE/v1/billing/me -H "$AUTHH")"
echo "  billing/me: $(curl -s $BASE/v1/billing/me -H "$AUTHH" | head -c 200)"

echo "===== 6. Devices ====="
chk "POST /v1/devices" 201 "$(curl -s -o /dev/null -w '%{http_code}' -X POST $BASE/v1/devices -H "$AUTHH" -H "$CT" -d '{"apns_token":"e2eaa11bb22cc33dd44ee55ff6677","platform":"ios","environment":"production"}')"
chk "POST /v1/devices bad platform" 422 "$(curl -s -o /dev/null -w '%{http_code}' -X POST $BASE/v1/devices -H "$AUTHH" -H "$CT" -d '{"apns_token":"y","platform":"nokia"}')"
chk "DELETE /v1/devices/{token}" 204 "$(curl -sL -o /dev/null -w '%{http_code}' -X DELETE $BASE/v1/devices/e2eaa11bb22cc33dd44ee55ff6677 -H "$AUTHH")"
chk "DELETE /v1/devices/{other}" 404 "$(curl -sL -o /dev/null -w '%{http_code}' -X DELETE $BASE/v1/devices/never_registered -H "$AUTHH")"

echo "===== 7. Quota-gate (Free=1 project) ====="
curl -s -o /dev/null -X POST $BASE/v1/projects -H "$AUTHH" -H "$CT" -H 'Idempotency-Key: e2e-B' -d '{"prompt":"second"}'
chk "over Free project limit" 402 "$(curl -s -o /dev/null -w '%{http_code}' -X POST $BASE/v1/projects -H "$AUTHH" -H "$CT" -H 'Idempotency-Key: e2e-C' -d '{"prompt":"third"}')"

echo "===== 8. DELETE project lifecycle ====="
chk "DELETE /v1/projects/{pid}" 202 "$(curl -sL -o /dev/null -w '%{http_code}' -X DELETE $BASE/v1/projects/$PID1 -H "$AUTHH")"
sleep 2
chk "deleted project GET" 404 "$(curl -sL -o /dev/null -w '%{http_code}' $BASE/v1/projects/$PID1 -H "$AUTHH")"

echo "===== 9. RFC-7807 ====="
chk "error problem+json" yes "$(curl -s -o /dev/null -w '%{content_type}' $BASE/v1/projects | grep -q problem && echo yes || echo no)"

echo ""
echo "====================================================="
echo "  E2E PROD:  PASS=$PASS  FAIL=$FAIL"
echo "====================================================="

# FargoVPN 4.6.9

## Push / Web Push

- VAPID public key is now derived from the private P-256 key as the source of truth; restored/stale `config.py` public keys are synchronized automatically.
- `PUSH_VAPID_SUBJECT` is validated before Push delivery; localhost/local/invalid subjects are rejected because Apple Web Push may return `BadJwtToken` for invalid subjects.
- VAPID diagnostics expose the effective subject and key-pair health without exposing the private key.
- Push subscriptions store the VAPID public-key fingerprint value used at registration, allowing stale subscriptions after VAPID rotation to be detected and removed.
- Apple/FCM provider JSON `reason` values are extracted into the Push delivery log, including `BadJwtToken` and `VapidPkHashMismatch`.
- `VapidPkHashMismatch`, 404 and 410 endpoints are removed automatically; `BadJwtToken` is retained with an explicit configuration diagnostic because it is not itself evidence that the endpoint is dead.
- Browser registration now uses an explicit 65-byte uncompressed P-256 `ArrayBuffer` and detects a subscription created with a different `applicationServerKey`.
- On `AbortError: Error retrieving push subscription`, the panel performs one controlled Service Worker/cache reset and retries registration once.
- Push subscribe backend returns HTTP 503 with the precise VAPID configuration problem instead of a generic failure.

## iPhone / Safari

- Existing PWA requirement remains explicit: iOS/iPadOS web push requires the web app to be launched from the Home Screen.
- New diagnostics display VAPID subject and provider reason, making Safari `BadJwtToken` failures distinguishable from browser subscription failures.

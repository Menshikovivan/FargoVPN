from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "app"


def test_notification_click_never_navigates_to_push_payload_url():
    worker = (ROOT / "service-worker.js").read_text(encoding="utf-8")
    assert "data.url" not in worker
    assert "event.notification.data.url" not in worker
    assert "new URL(String(raw" not in worker
    assert "const target = appPrefix;" in worker
    assert "data: { source: 'fargovpn' }" in worker
    assert "`${self.location.origin}${BASE}`" in worker


def test_service_worker_keeps_push_and_static_cache_handlers():
    worker = (ROOT / "service-worker.js").read_text(encoding="utf-8")
    assert "self.addEventListener('push'" in worker
    assert "self.registration.showNotification" in worker
    assert "caches.open(CACHE)" in worker
    assert "request.method !== 'GET'" in worker

from __future__ import annotations

import os
from pathlib import Path

import pytest

try:
    from playwright.sync_api import sync_playwright
except Exception:  # pragma: no cover - optional visual audit dependency
    sync_playwright = None


ROOT = Path(__file__).resolve().parents[1]
CSS = ROOT / "app" / "static" / "panel.css"


@pytest.mark.skipif(sync_playwright is None, reason="Playwright is not installed")
def test_panel_graphics_fit_family_billing_and_dense_views_at_common_widths():
    chrome = os.environ.get(
        "FARGOVPN_CHROMIUM", r"C:\Program Files\Google\Chrome\Application\chrome.exe"
    )
    if not Path(chrome).exists():
        pytest.skip("Chrome is not available for the visual layout check")
    html = """<!doctype html><html lang="ru"><head><meta name="viewport" content="width=device-width,initial-scale=1"></head>
    <body class="panel-page"><aside id="panel-sidebar"><nav><a>Пользователи</a></nav></aside>
    <main><header class="page-header"><h1>Пользователь</h1><div class="actions"><button>Действие</button></div></header>
    <section class="grid"><div class="card full"><h2>Оплата и семейный тариф</h2>
    <p>Статус: Ожидает оплаты · Telegram аккаунтов: 4 · сумма за период: 800 ₽</p><p>Индивидуальная сумма заменяет автоматический тариф.</p>
    <form action="/users/id/1/payment-price"><div class="setting"><label>Индивидуальная сумма, ₽</label><input value="800"></div><button>Сохранить сумму</button></form>
    <div class="setting"><label>Семейные Telegram аккаунты</label><p>Каждый аккаунт должен открыть ссылку и пройти проверку PIN.</p>
    <ul><li>TG 2 · @member2</li><li>TG 3 · @member3</li></ul><form action="/users/id/1/family-link"><button>Создать ссылку привязки на 30 минут</button></form></div>
    </div><div class="card full table-wrap"><table><tr><th>Пользователь</th><th>Статус</th><th>Дата</th></tr><tr><td>Пример</td><td>Активен</td><td>Сегодня</td></tr></table></div></section></main></body></html>"""
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, executable_path=chrome, args=["--no-sandbox"])
        page = browser.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)
        page.set_content(html)
        page.add_style_tag(content=CSS.read_text(encoding="utf-8"))
        for width in (360, 600, 800, 1000, 1440):
            page.set_viewport_size({"width": width, "height": 900})
            metrics = page.evaluate("""() => ({
                viewport: document.documentElement.clientWidth,
                page: document.documentElement.scrollWidth,
                mainClient: document.querySelector('main').clientWidth,
                mainScroll: document.querySelector('main').scrollWidth,
                mainLeft: Math.round(document.querySelector('main').getBoundingClientRect().left),
                sidebarRight: Math.round(document.querySelector('#panel-sidebar').getBoundingClientRect().right),
                familyBorder: getComputedStyle(document.querySelector('.setting:has(form[action$=\"/family-link\"])')).borderTopWidth,
                paymentColumns: getComputedStyle(document.querySelector('form[action$=\"/payment-price\"]')).gridTemplateColumns
            })""")
            assert metrics["page"] <= metrics["viewport"], (width, metrics)
            assert metrics["mainScroll"] <= metrics["mainClient"] + 1, (width, metrics)
            if 900 < width <= 1120:
                assert metrics["mainLeft"] >= metrics["sidebarRight"], (width, metrics)
                assert metrics["familyBorder"] != "0px", (width, metrics)
            if width <= 600:
                assert len(metrics["paymentColumns"].split()) == 1, (width, metrics)
        assert not errors, errors
        browser.close()

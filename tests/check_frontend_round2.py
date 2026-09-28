"""浏览器回归：真实页面与可控 bridge，验证分页、上传和小屏幕交错操作。

运行：/root/projects/tmp/AstrBot/.venv/bin/python tests/check_frontend_round2.py
只在浏览器中提供本地静态页面，不启动 Web 服务，也不访问真实图库。
"""

import asyncio
import base64
import io
import os
from pathlib import Path
from urllib.parse import urlparse

from PIL import Image, ImageDraw
from playwright.async_api import async_playwright


GALLERY = Path(__file__).resolve().parents[1] / "pages" / "gallery"
png_buffer = io.BytesIO()
Image.new("RGB", (16, 12), "red").save(png_buffer, "PNG")
PNG = png_buffer.getvalue()

BRIDGE = """
window.__calls = { get: [], post: [], upload: [] };
window.__all = Array.from({length: 150}, (_, i) => ({
  category: 'alpha', name: `img_${String(i).padStart(3, '0')}.png`,
  mtime: 1, size: 78, width: 16, height: 12, is_animated: false,
}));
window.__get = null;
window.__upload = null;
window.AstrBotPluginPage = {
  ready: async () => ({}), t: (_, fallback) => fallback,
  onContext: () => () => {},
  apiGet: async (endpoint, params) => {
    window.__calls.get.push({endpoint, params});
    if (window.__get) {
      const result = window.__get(endpoint, params);
      if (result !== undefined) return result;
    }
    if (endpoint === 'overview') return {categories: [
      {name: 'alpha', description: '', count: 150},
      {name: 'beta', description: '', count: 0},
    ], total: 150, image_host_configured: !!window.__configured};
    if (endpoint === 'images') {
      let items = window.__all.filter(i => !params.category || i.category === params.category);
      if (params.q) items = items.filter(i => i.name.includes(params.q));
      return {items: items.slice(params.offset, params.offset + params.limit), total: items.length};
    }
    if (endpoint === 'thumb') return {content: window.__png, is_animated: false};
    if (endpoint === 'image') return {
      content: window.__png, mime: 'image/png', width: 16, height: 12, size: 78,
    };
    if (endpoint === 'sync/status') {
      if (window.__statusFail) throw Error('图床暂时不可用');
      return {configured: true, provider: 'test', to_upload_count: 0, to_download_count: 0};
    }
    if (endpoint === 'sync/process') return {running: false};
    return {configured: false};
  },
  apiPost: async (endpoint, body) => {
    window.__calls.post.push({endpoint, body});
    if (endpoint === 'images/copy') return {
      copied: [body.items[0]], conflicting: body.items.slice(1), missing: [],
    };
    return {};
  },
  upload: async (endpoint, file) => {
    window.__calls.upload.push({endpoint, name: file.name});
    if (window.__upload) return window.__upload(endpoint, file);
    return {outcome: 'added', filename: file.name};
  },
  download: async () => ({}),
};
""".replace("window.__calls =", "window.__png = '"
            + base64.b64encode(PNG).decode() + "';\nwindow.__calls =", 1)


async def page_for(browser, viewport=None, configured=False):
    page = await browser.new_page(viewport=viewport or {"width": 1280, "height": 900})
    page.set_default_timeout(8000)
    await page.add_init_script(BRIDGE)
    if configured:
        await page.add_init_script("window.__configured = true; window.__statusFail = true;")

    async def serve(route):
        path = GALLERY / urlparse(route.request.url).path.lstrip("/")
        if path.is_file():
            await route.fulfill(path=path)
        else:
            await route.fulfill(status=404, body="")

    await page.route("http://meme-review.test/**", serve)
    await page.goto("http://meme-review.test/index.html")
    await page.wait_for_function("document.querySelectorAll('#grid .cell').length === 48")
    return page


async def batch_keeps_second_page(browser):
    page = await page_for(browser)
    await page.locator("#pager button").click()
    await page.wait_for_function("document.querySelectorAll('#grid .cell').length === 96")
    for index in (60, 61):
        await page.locator("#grid .check-btn").nth(index).click()
    await page.locator("#batch-copy").click()
    await page.locator("#target-list li").filter(has_text="beta").click()
    await page.wait_for_function("document.querySelectorAll('#grid .cell').length === 48")
    assert await page.locator("#batch-count").inner_text() == "已选 1 张"
    await page.close()


async def search_invalidates_old_pager(browser):
    page = await page_for(browser)
    await page.evaluate("""() => {
      window.__gates = [];
      window.__get = (endpoint, params) => {
        if (endpoint === 'images' && params.q)
          return new Promise(resolve => window.__gates.push({params, resolve}));
      };
    }""")
    await page.locator("#search").fill("needle")
    await page.wait_for_function("window.__gates.length === 1")
    assert await page.locator("#pager button").count() == 0
    await page.evaluate("""() => {
      const items = Array.from({length: 100}, (_, i) => ({
        category: 'alpha', name: `needle_${i}.png`, size: 1, mtime: 2,
      }));
      window.__gates[0].resolve({items: items.slice(0, 48), total: 100});
    }""")
    await page.wait_for_function("document.querySelectorAll('#grid .cell').length === 48")
    names = await page.locator("#grid .name").all_text_contents()
    assert all("needle_" in name for name in names)
    await page.locator("#pager button").click()
    await page.wait_for_function("window.__gates.length === 2")
    offset = await page.evaluate("window.__gates[1].params.offset")
    assert offset == 48
    await page.evaluate("""() => {
      const items = Array.from({length: 100}, (_, i) => ({
        category: 'alpha', name: `needle_${i}.png`, size: 1, mtime: 2,
      }));
      window.__gates[1].resolve({items: items.slice(48, 96), total: 100});
    }""")
    await page.wait_for_function("document.querySelectorAll('#grid .cell').length === 96")
    await page.close()


async def upload_rejects_late_queue_and_refreshes_once(browser):
    page = await page_for(browser)
    await page.evaluate("""() => {
      window.__upload = (endpoint, file) => new Promise(resolve => {
        window.__finishUpload = () => resolve({outcome: 'added', filename: file.name});
      });
    }""")
    await page.locator("#file-input").set_input_files({
        "name": "first.png", "mimeType": "image/png", "buffer": PNG,
    })
    await page.locator("#upload-category").select_option("alpha")
    await page.locator("#upload-confirm").click()
    await page.wait_for_function("window.__calls.upload.length === 1")
    await page.evaluate("""() => {
      const files = new DataTransfer();
      files.items.add(new File([Uint8Array.from(atob(window.__png), c => c.charCodeAt(0))],
        'later.png', {type:'image/png'}));
      window.dispatchEvent(new ClipboardEvent('paste', {
        clipboardData: files, bubbles: true, cancelable: true,
      }));
    }""")
    await page.wait_for_function("document.getElementById('toast-root').textContent.includes('完成后再添加')")
    assert await page.locator("#upload-list li").count() == 1
    await page.evaluate("window.__finishUpload()")
    await page.wait_for_function("document.getElementById('upload-modal').hidden")
    await page.wait_for_function(
        "window.__calls.get.filter(c => c.endpoint === 'overview').length === 2"
    )
    await page.wait_for_timeout(100)
    assert await page.evaluate("window.__calls.upload.map(c => c.name)") == ["first.png"]
    assert await page.evaluate(
        "window.__calls.get.filter(c => c.endpoint === 'overview').length"
    ) == 2
    await page.close()


async def upload_refresh_failure_is_visible(browser):
    page = await page_for(browser)
    errors = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    await page.evaluate("""() => {
      window.__upload = async (_, file) => {
        window.__get = endpoint => {
          if (endpoint === 'overview') return Promise.reject(Error('模拟刷新断网'));
        };
        return {outcome: 'added', filename: file.name};
      };
    }""")
    await page.locator("#file-input").set_input_files({
        "name": "saved.png", "mimeType": "image/png", "buffer": PNG,
    })
    await page.locator("#upload-category").select_option("alpha")
    await page.locator("#upload-confirm").click()
    await page.wait_for_function("document.getElementById('upload-modal').hidden")
    await page.wait_for_function(
        "document.getElementById('toast-root').textContent.includes('图库列表刷新失败')"
    )
    assert not errors
    await page.close()


async def upload_refresh_keeps_batch_locked(browser):
    page = await page_for(browser)
    await page.evaluate("""() => {
      window.__upload = async (_, file) => {
        window.__get = endpoint => {
          if (endpoint === 'overview')
            return new Promise(resolve => window.__finishRefresh = () => resolve({
              categories: [{name:'alpha', count:151}], total:151,
              image_host_configured: false,
            }));
        };
        return {outcome: 'added', filename: file.name};
      };
    }""")
    await page.locator("#file-input").set_input_files({
        "name": "first.png", "mimeType": "image/png", "buffer": PNG,
    })
    await page.locator("#upload-category").select_option("alpha")
    await page.locator("#upload-confirm").click()
    await page.wait_for_function("!!window.__finishRefresh")
    assert await page.locator("#upload-more").is_disabled()
    await page.evaluate("""() => {
      const files = new DataTransfer();
      files.items.add(new File(['x'], 'during_refresh.png', {type:'image/png'}));
      window.dispatchEvent(new ClipboardEvent('paste', {
        clipboardData:files, bubbles:true, cancelable:true,
      }));
    }""")
    await page.wait_for_function(
        "document.getElementById('toast-root').textContent.includes('完成后再添加')"
    )
    assert await page.locator("#upload-list li").count() == 1
    await page.evaluate("window.__finishRefresh()")
    await page.wait_for_function("document.getElementById('upload-modal').hidden")
    await page.close()


async def controls_and_layout(browser):
    page = await page_for(browser, {"width": 375, "height": 667})
    screenshot = os.environ.get("MEME_REVIEW_SCREENSHOT_PATH")
    if screenshot:
        await page.screenshot(path=screenshot + ".page.png")
    assert await page.locator("#add-btn").evaluate(
        "el => el.scrollWidth <= el.clientWidth"
    )
    await page.locator("#file-input").set_input_files([
        {"name": f"mobile_{i}.png", "mimeType": "image/png", "buffer": PNG}
        for i in range(8)
    ])
    await page.locator("#upload-category").focus()
    await page.keyboard.press("ArrowDown")
    await page.keyboard.press("Enter")
    assert await page.locator("#upload-category").input_value() == "alpha"
    assert await page.locator("#upload-zoom").evaluate(
        "el => el.scrollWidth <= el.clientWidth"
    )
    for button_id in ("upload-more", "upload-clear", "upload-confirm"):
        assert await page.locator("#" + button_id).evaluate("""el => {
          const r = el.getBoundingClientRect();
          return r.bottom <= innerHeight &&
            document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2) === el;
        }"""), button_id
    await page.locator("#upload-confirm").focus()
    await page.keyboard.press("Tab")
    assert await page.locator("#upload-category").evaluate(
        "el => el === document.activeElement"
    )
    if screenshot:
        await page.screenshot(path=screenshot)
    await page.locator("#upload-clear").click()
    await page.locator("#confirm-ok").click()
    assert await page.locator("#upload-modal").is_hidden()
    await page.close()


async def sync_status_can_retry(browser):
    page = await page_for(browser, configured=True)
    await page.locator("#advanced summary").click()
    await page.wait_for_function(
        "document.getElementById('sync-status').textContent.includes('获取失败')"
    )
    await page.locator("#advanced summary").click()
    await page.evaluate("window.__statusFail = false")
    await page.locator("#advanced summary").click()
    await page.wait_for_function(
        "document.getElementById('sync-status').textContent.includes('图床：test')"
    )
    count = await page.evaluate(
        "window.__calls.get.filter(c => c.endpoint === 'sync/status').length"
    )
    assert count == 2
    await page.close()


async def thumbnail_and_viewer(browser):
    page = await page_for(browser)
    out = io.BytesIO()
    original = Image.new("RGB", (2400, 1600), "blue")
    ImageDraw.Draw(original).rectangle((1200, 0, 2399, 1599), fill="red")
    original.save(out, "PNG")
    await page.locator("#file-input").set_input_files({
        "name": "large.png", "mimeType": "image/png", "buffer": out.getvalue(),
    })
    await page.wait_for_function(
        "document.querySelector('.upload-thumb')?.src.startsWith('data:image/png')"
    )
    await page.locator(".upload-thumb").evaluate("img => img.decode()")
    side = await page.locator(".upload-thumb").evaluate(
        "img => Math.max(img.naturalWidth, img.naturalHeight)"
    )
    assert side <= 80
    colors = await page.locator(".upload-thumb").evaluate("""img => {
      const canvas = document.createElement('canvas');
      canvas.width = img.naturalWidth;
      canvas.height = img.naturalHeight;
      const ctx = canvas.getContext('2d');
      ctx.drawImage(img, 0, 0);
      return [
        [...ctx.getImageData(5, 5, 1, 1).data],
        [...ctx.getImageData(canvas.width - 5, 5, 1, 1).data],
      ];
    }""")
    assert colors[0][:3] == [0, 0, 255] and colors[1][:3] == [255, 0, 0]
    await page.locator("#upload-close").click()
    await page.locator("#grid .name").first.click()
    await page.wait_for_function("document.querySelector('#viewer-pane img')")
    await page.locator("#viewer-zoom").click()
    await page.locator("#viewer-next").click()
    await page.wait_for_function(
        "document.getElementById('viewer-title').textContent.includes('img_001')"
    )
    assert "scrollable" not in await page.locator("#viewer-pane").get_attribute("class")
    await page.close()


async def main():
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        for test in (
            batch_keeps_second_page,
            search_invalidates_old_pager,
            upload_rejects_late_queue_and_refreshes_once,
            upload_refresh_failure_is_visible,
            upload_refresh_keeps_batch_locked,
            controls_and_layout,
            sync_status_can_retry,
            thumbnail_and_viewer,
        ):
            await test(browser)
            print("通过:", test.__name__)
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())

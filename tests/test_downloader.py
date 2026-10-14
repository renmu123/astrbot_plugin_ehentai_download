import asyncio
import base64
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import httpx

from downloader import (
    DownloadError, Gallery, GalleryDownloader, MIB, Options,
    image_suffix, safe_name, validate_image_url,
)

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a/AQAAAAASUVORK5CYII=")
GALLERY = Gallery.parse("https://e-hentai.org/g/42/abcdef1234/")


def gallery_html(indexes, last=0, host="e-hentai.org"):
    links = "".join(f'<a href="https://{host}/s/token{index}/42-{index}">thumb</a>' for index in indexes)
    return f'<div id="gdt">{links}</div><table class="ptt"><a href="?p={last}">last</a></table>'


class Site:
    def __init__(self, pages=3):
        self.pages = pages
        self.requests = []
        self.fail_image_once = False
        self.original = False

    def handle(self, request):
        self.requests.append(request)
        path = request.url.path
        if path == "/api.php":
            assert json.loads(request.content) == {"method": "gdata", "gidlist": [[42, "abcdef1234"]], "namespace": 1}
            return httpx.Response(200, json={"gmetadata": [{"gid": 42, "token": "abcdef1234", "title": "A:/测试", "filecount": str(self.pages)}]})
        if path.startswith("/g/"):
            if self.pages == 3:
                indexes = [3] if request.url.params.get("p") == "1" else [2, 1]
                return httpx.Response(200, text=gallery_html(indexes, 1, request.url.host))
            return httpx.Response(200, text=gallery_html(range(1, self.pages + 1), host=request.url.host))
        if path.startswith("/s/"):
            index = path.rsplit("-", 1)[1]
            original = f'<div id="i7"><a href="/fullimg.php?page={index}">Download original</a></div>' if self.original else ""
            return httpx.Response(200, text=f'<img id="img" src="https://images.hath.network/{index}.png">{original}'
                                  + '<a id="loadfail" onclick="return nl(\'reload123\')">reload</a>')
        if path == "/fullimg.php":
            return httpx.Response(302, headers={"location": "https://images.hath.network/original.png"})
        if request.url.host == "images.hath.network":
            if self.fail_image_once:
                self.fail_image_once = False
                return httpx.Response(404)
            return httpx.Response(200, content=PNG, headers={"content-type": "image/png"})
        raise AssertionError(f"Unexpected request: {request.url}")


class ParsingTests(unittest.TestCase):
    def test_links_and_canonicalization(self):
        gallery = Gallery.parse("https://exhentai.org/g/42/abcdef1234/?p=2#x")
        self.assertEqual(gallery.url, "https://exhentai.org/g/42/abcdef1234/")

    def test_reject_other_urls(self):
        for url in ["http://e-hentai.org/g/1/a/", "https://e-hentai.org.evil.test/g/1/a/", "https://u:p@e-hentai.org/g/1/a/", "https://e-hentai.org:8443/g/1/a/", "https://e-hentai.org/s/a/1-2", "not a URL"]:
            with self.subTest(url=url), self.assertRaises(DownloadError):
                Gallery.parse(url)

    def test_private_image_urls_rejected(self):
        for url in ["file:///etc/passwd", "http://127.0.0.1/a", "http://127.1/a", "http://[::1]/a", "http://10.0.0.1/a", "http://localhost/a", "http://service.internal/a", "https://user:secret@images.hath.network/a"]:
            with self.subTest(url=url), self.assertRaises(DownloadError):
                validate_image_url(url)
        self.assertEqual(validate_image_url("http://8.8.8.8:1234/a"), "http://8.8.8.8:1234/a")

    def test_limit_placeholder_rejected(self):
        with self.assertRaisesRegex(DownloadError, "额度"):
            validate_image_url("https://ehgt.org/g/509.gif?x=1")

    def test_filename(self):
        self.assertNotIn("/", safe_name("../../A:书籍?"))
        self.assertEqual(safe_name("CON"), "gallery")
        self.assertEqual(safe_name("..."), "gallery")
        self.assertLessEqual(len(safe_name("书" * 100)), 70)

    def test_options_validation(self):
        for kwargs in [{"max_pages": 0}, {"retries": 6}, {"interval": -1}, {"proxy_mode": "wrong"}, {"proxy_mode": "custom"}, {"timeout": float("nan")}]:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                Options(**kwargs)


class DownloadTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.addCleanup(self.temporary.cleanup)
        self.site = Site()

    def downloader(self, **kwargs):
        return GalleryDownloader(Options(proxy_mode="none", interval=0, retries=0, **kwargs), transport=httpx.MockTransport(self.site.handle))

    def cache_directory(self):
        directory = self.root / f"{GALLERY.host}_{GALLERY.gid}_{GALLERY.token}_display"
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    async def test_pagination_pack_and_metadata(self):
        progress = []
        result = await self.downloader().download(GALLERY, self.root, lambda *args: progress.append(args))
        with zipfile.ZipFile(result.archive) as archive:
            self.assertEqual(archive.namelist(), ["0001.png", "0002.png", "0003.png", "metadata.json"])
            self.assertIsNone(archive.testzip())
            self.assertEqual(archive.read("0002.png"), PNG)
            self.assertEqual(json.loads(archive.read("metadata.json"))["source"], GALLERY.url)
        self.assertEqual(result.pages, 3)
        self.assertEqual(result.archive.parent, self.root)
        self.assertEqual(list(self.root.iterdir()), [result.archive])
        self.assertEqual(progress[-1][:3], ("打包", 3, 3))
        self.assertTrue(any(request.url.params.get("p") == "1" for request in self.site.requests))

    async def test_resume_failed_download_reuses_completed_images(self):
        handler = self.site.handle
        def fail_second_image(request):
            if request.url.host == "images.hath.network" and request.url.path == "/2.png":
                return httpx.Response(403)
            return handler(request)
        self.site.handle = fail_second_image
        downloader = self.downloader()
        with self.assertRaises(DownloadError):
            await downloader.download(GALLERY, self.root)
        self.assertEqual((self.cache_directory() / "0001.png").read_bytes(), PNG)
        self.assertFalse(list(self.root.glob("*.zip")))
        self.site.handle = handler
        self.site.requests.clear()
        result = await self.downloader().download(GALLERY, self.root)
        images = [request.url.path for request in self.site.requests if request.url.host == "images.hath.network"]
        self.assertEqual(images, ["/2.png", "/3.png"])
        self.assertEqual(list(self.root.iterdir()), [result.archive])

    async def test_corrupt_cached_page_is_redownloaded(self):
        downloader = self.downloader()
        directory = self.cache_directory()
        for index in (1, 3):
            (directory / f"{index:04d}.png").write_bytes(PNG)
        (directory / "0002.png").write_text("<html>invalid</html>")
        result = await downloader.download(GALLERY, self.root)
        with zipfile.ZipFile(result.archive) as archive:
            self.assertEqual(archive.read("0002.png"), PNG)
        images = [request for request in self.site.requests if request.url.host == "images.hath.network"]
        self.assertEqual(len(images), 1)

    async def test_cookie_scoping(self):
        await self.downloader(cookie="ipb_member_id=123; ipb_pass_hash=secret").download(GALLERY, self.root)
        for request in self.site.requests:
            if request.url.host == "e-hentai.org":
                self.assertIn("ipb_pass_hash=secret", request.headers.get("cookie", ""))
            if request.url.host == "images.hath.network":
                self.assertNotIn("cookie", request.headers)

    async def test_original_redirect_and_auth_cookie(self):
        self.site.original = True
        result = await self.downloader(original=True, cookie="ipb_member_id=123").download(GALLERY, self.root)
        original_requests = [request for request in self.site.requests if request.url.path == "/fullimg.php"]
        self.assertEqual(len(original_requests), 3)
        self.assertIn("ipb_member_id=123", original_requests[0].headers["cookie"])
        self.assertEqual(result.archive.parent, self.root)
        self.assertTrue(result.archive.stem.endswith("_original"))
        self.assertEqual(list(self.root.iterdir()), [result.archive])
        self.assertTrue(all("cookie" not in request.headers for request in self.site.requests if request.url.host == "images.hath.network"))

    async def test_original_without_link_uses_display_image(self):
        result = await self.downloader(original=True).download(GALLERY, self.root)
        self.assertTrue(result.archive.exists())

    async def test_original_and_display_archives_remain_separate(self):
        display = await self.downloader().download(GALLERY, self.root)
        original = await self.downloader(original=True).download(GALLERY, self.root)
        self.assertNotEqual(display.archive, original.archive)
        self.assertEqual(set(self.root.iterdir()), {display.archive, original.archive})
        for result, mode in ((display, "display"), (original, "original")):
            with zipfile.ZipFile(result.archive) as archive:
                self.assertEqual(json.loads(archive.read("metadata.json"))["download_mode"], mode)

    async def test_archive_move_failure_preserves_images_and_existing_zip(self):
        existing = self.root / "42_A__测试.zip"
        existing.write_bytes(b"previous archive")
        replace = Path.replace
        def fail_final_move(source, destination):
            if source.suffix == ".zip":
                raise OSError("move failed")
            return replace(source, destination)
        with patch.object(Path, "replace", fail_final_move):
            with self.assertRaisesRegex(OSError, "move failed"):
                await self.downloader().download(GALLERY, self.root)
        self.assertEqual(existing.read_bytes(), b"previous archive")
        directory = self.cache_directory()
        self.assertEqual(len(list(directory.glob("*.png"))), 3)
        self.assertEqual(len(list(directory.glob("*.zip"))), 1)
        result = await self.downloader().download(GALLERY, self.root)
        self.assertEqual(result.archive, existing)
        self.assertEqual(list(self.root.iterdir()), [result.archive])

    async def test_page_limit_stops_before_gallery_requests(self):
        with self.assertRaisesRegex(DownloadError, "页上限"):
            await self.downloader(max_pages=2).download(GALLERY, self.root)
        self.assertEqual(len(self.site.requests), 1)

    async def test_exhentai_requires_cookie(self):
        with self.assertRaisesRegex(DownloadError, "Cookie"):
            await self.downloader().download(Gallery("exhentai.org", 42, "abcdef1234"), self.root)
        self.assertEqual(self.site.requests, [])

    async def test_missing_page_never_creates_zip(self):
        handler = self.site.handle
        def missing(request):
            if request.url.params.get("p") == "1":
                return httpx.Response(200, text=gallery_html([]))
            return handler(request)
        self.site.handle = missing
        with self.assertRaisesRegex(DownloadError, "全部图片页"):
            await self.downloader().download(GALLERY, self.root)
        self.assertEqual(list(self.root.rglob("*.zip")), [])

    async def test_image_failure_reload_and_retry(self):
        self.site.fail_image_once = True
        downloader = GalleryDownloader(Options(proxy_mode="none", interval=0, retries=1), transport=httpx.MockTransport(self.site.handle))
        # Eliminate only the retry backoff, preserving the request sequence.
        async def no_wait(_):
            return None
        with patch("downloader.asyncio.sleep", no_wait):
            result = await downloader.download(GALLERY, self.root)
        self.assertTrue(result.archive.exists())
        self.assertTrue(any(request.url.params.get("nl") == "reload123" for request in self.site.requests))

    async def test_reload_handler_in_child_anchor_switches_failed_server(self):
        handler = self.site.handle
        self.site.pages = 1
        def nested_reload(request):
            if request.url.path.startswith("/s/"):
                self.site.requests.append(request)
                image_name = "fresh" if request.url.params.get("nl") == "reload_123-x" else "stale"
                return httpx.Response(200, text=f'<img id="img" src="https://images.hath.network/{image_name}.png">'
                                      + '<div id="loadfail"><a onclick="return nl( \'reload_123-x\' )">reload</a></div>')
            if request.url.path == "/stale.png":
                return httpx.Response(404)
            return handler(request)
        downloader = GalleryDownloader(Options(proxy_mode="none", interval=0, retries=1), transport=httpx.MockTransport(nested_reload))
        async def no_wait(_):
            return None
        with patch("downloader.asyncio.sleep", no_wait):
            result = await downloader.download(GALLERY, self.root)
        self.assertTrue(result.archive.exists())
        self.assertTrue(any(request.url.params.get("nl") == "reload_123-x" for request in self.site.requests))

    async def test_image_error_preserves_safe_network_reason(self):
        handler = self.site.handle
        self.site.pages = 1
        for error_type in (httpx.ProxyError, httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError):
            def fail(request):
                if request.url.host == "images.hath.network":
                    raise error_type("proxy-password=secret-value; signed-url=secret-value", request=request)
                return handler(request)
            with self.subTest(error=error_type.__name__):
                downloader = GalleryDownloader(Options(proxy_mode="none", interval=0, retries=0), transport=httpx.MockTransport(fail))
                with self.assertRaises(DownloadError) as raised:
                    await downloader.download(GALLERY, self.root)
                message = str(raised.exception)
                self.assertIn("第 1 页", message)
                self.assertIn("images.hath.network", message)
                self.assertIn(error_type.__name__, message)
                self.assertNotIn("secret-value", message)
                self.assertFalse(list(self.root.rglob("*.part")))

    async def test_image_error_preserves_http_status(self):
        handler = self.site.handle
        self.site.pages = 1
        def rejected(request):
            if request.url.host == "images.hath.network":
                return httpx.Response(403)
            return handler(request)
        downloader = GalleryDownloader(Options(proxy_mode="none", interval=0, retries=0), transport=httpx.MockTransport(rejected))
        with self.assertRaisesRegex(DownloadError, "HTTP 403"):
            await downloader.download(GALLERY, self.root)

    async def test_image_size_limit_cleans_partial_file(self):
        handler = self.site.handle
        def large(request):
            if request.url.host == "images.hath.network":
                return httpx.Response(200, content=PNG + b"x" * MIB)
            return handler(request)
        self.site.handle = large
        with self.assertRaisesRegex(DownloadError, "单张图片"):
            await self.downloader(max_image_mb=1).download(GALLERY, self.root)
        self.assertFalse(list(self.root.rglob("*.part")))
        self.assertFalse(list(self.root.rglob("*.zip")))

    async def test_html_instead_of_image_never_creates_zip(self):
        handler = self.site.handle
        def blocked(request):
            if request.url.host == "images.hath.network":
                return httpx.Response(200, content=b"<html>please login</html>", headers={"content-type": "image/png"})
            return handler(request)
        self.site.handle = blocked
        with self.assertRaisesRegex(DownloadError, "非图片内容"):
            await self.downloader().download(GALLERY, self.root)
        self.assertFalse(list(self.root.rglob("*.zip")))
        self.assertFalse(list(self.root.rglob("*.part")))

    async def test_gallery_limit_includes_cached_images(self):
        (self.cache_directory() / "0001.png").write_bytes(PNG + b"x" * MIB)
        with self.assertRaisesRegex(DownloadError, "画廊下载体积"):
            await self.downloader(max_gallery_mb=1).download(GALLERY, self.root)

    async def test_image_redirect_to_private_address_rejected(self):
        handler = self.site.handle
        def redirect(request):
            if request.url.host == "images.hath.network":
                return httpx.Response(302, headers={"location": "http://127.0.0.1/secret"})
            return handler(request)
        self.site.handle = redirect
        with self.assertRaisesRegex(DownloadError, "不允许"):
            await self.downloader().download(GALLERY, self.root)
        self.assertFalse(any(request.url.host == "127.0.0.1" for request in self.site.requests))

    async def test_quota_message_stops(self):
        handler = self.site.handle
        def quota(request):
            if request.url.path.startswith("/s/"):
                return httpx.Response(200, text="You have exceeded your image viewing limits")
            return handler(request)
        self.site.handle = quota
        with self.assertRaisesRegex(DownloadError, "额度"):
            await self.downloader().download(GALLERY, self.root)

    async def test_cancel_stream_removes_partial_file(self):
        entered = asyncio.Event()
        class SlowStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield PNG + b"x" * 65536
                entered.set()
                await asyncio.Event().wait()
        async def handler(request):
            if request.url.host == "images.hath.network":
                return httpx.Response(200, stream=SlowStream())
            return self.site.handle(request)
        downloader = GalleryDownloader(Options(proxy_mode="none", interval=0), transport=httpx.MockTransport(handler))
        task = asyncio.create_task(downloader.download(GALLERY, self.root))
        await asyncio.wait_for(entered.wait(), 2)
        self.assertTrue(list(self.root.rglob("*.part")))
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(list(self.root.rglob("*.part")))
        self.assertFalse(list(self.root.rglob("*.zip")))

    async def test_cancel_pack_removes_partial_zip(self):
        image = self.root / "0001.png"
        image.write_bytes(PNG + b"x" * (3 * MIB))
        archive = self.root / "test.zip"
        task = asyncio.create_task(self.downloader().pack(archive, [image], {}))
        await asyncio.sleep(0)
        self.assertTrue(archive.with_suffix(".zip.part").exists())
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(archive.exists())
        self.assertFalse(archive.with_suffix(".zip.part").exists())


if __name__ == "__main__":
    unittest.main()

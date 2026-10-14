"""E-Hentai gallery downloader; independent of AstrBot for offline testing."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import re
import shutil
import zipfile
from dataclasses import dataclass
from http.cookiejar import Cookie
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit, urlunsplit

import httpx
from bs4 import BeautifulSoup

HOSTS = {"e-hentai.org", "exhentai.org"}
GALLERY_RE = re.compile(r"/g/(\d+)/([a-zA-Z0-9]+)/?")
PAGE_RE = re.compile(r"/s/[a-zA-Z0-9]+/(\d+)-(\d+)")
MIB = 1024 * 1024


class DownloadError(Exception):
    """An error safe to display to a user (never includes credentials/URLs)."""


class RetryableError(DownloadError):
    pass


def retry_reason(error: httpx.RequestError | RetryableError) -> str:
    """Describe request failures without leaking raw errors or signed URLs."""
    if isinstance(error, RetryableError):
        return str(error)
    reasons = (
        (httpx.ProxyError, "代理连接或代理隧道建立失败"),
        (httpx.ConnectTimeout, "连接服务器超时"),
        (httpx.ReadTimeout, "读取图片或响应超时"),
        (httpx.WriteTimeout, "发送请求超时"),
        (httpx.PoolTimeout, "等待网络连接超时"),
        (httpx.ConnectError, "连接服务器失败，请检查代理、DNS 和 TLS 证书"),
        (httpx.RemoteProtocolError, "服务器提前断开连接或返回了无效响应"),
        (httpx.ReadError, "读取响应时连接中断"),
        (httpx.DecodingError, "服务器返回了无法解码的响应"),
    )
    for kind, description in reasons:
        if isinstance(error, kind):
            return f"{description}（{type(error).__name__}）"
    return f"网络请求异常（{type(error).__name__}）"


def reload_token(soup: BeautifulSoup) -> str | None:
    # The reload handler may be on the #loadfail container or a child anchor.
    for element in soup.select("#loadfail[onclick], #loadfail [onclick], a[onclick]"):
        match = re.search(r"\bnl\s*\(\s*(['\"])([A-Za-z0-9_-]+)\1\s*\)", element.get("onclick", ""))
        if match:
            return match[2]
    return None


@dataclass(frozen=True)
class Gallery:
    host: str
    gid: int
    token: str

    @property
    def url(self) -> str:
        return f"https://{self.host}/g/{self.gid}/{self.token}/"

    @classmethod
    def parse(cls, value: str) -> Gallery:
        try:
            parts = urlsplit(value.strip())
            match = GALLERY_RE.fullmatch(parts.path)
            valid = (parts.scheme == "https" and parts.hostname in HOSTS
                     and parts.port in (None, 443) and not parts.username
                     and not parts.password and match)
        except ValueError:
            valid = False
        if not valid:
            raise DownloadError("请输入完整的 HTTPS 画廊链接：https://e-hentai.org/g/编号/令牌/（也支持 ExHentai）。")
        return cls(parts.hostname, int(match[1]), match[2])


@dataclass(frozen=True)
class Options:
    proxy_mode: str = "system"
    proxy_url: str = ""
    cookie: str = ""
    original: bool = False
    timeout: float = 45
    retries: int = 2
    interval: float = 1
    max_pages: int = 500
    max_image_mb: int = 64
    max_gallery_mb: int = 512

    def __post_init__(self):
        if self.proxy_mode not in {"system", "custom", "none"}:
            raise ValueError("代理模式应为 system、custom 或 none。")
        if self.proxy_mode == "custom":
            parts = urlsplit(self.proxy_url)
            if parts.scheme not in {"http", "https", "socks5", "socks5h"} or not parts.hostname:
                raise ValueError("custom 模式需要有效的 HTTP/SOCKS5 代理地址。")
        if not (1 <= self.max_pages <= 2000 and 0 <= self.retries <= 5
                and 1 <= self.timeout <= 300 and 0 <= self.interval <= 60
                and 1 <= self.max_image_mb <= 1024 and 1 <= self.max_gallery_mb <= 10240):
            raise ValueError("下载参数超出范围，请检查配置项的提示。")


@dataclass(frozen=True)
class DownloadResult:
    archive: Path
    title: str
    pages: int
    size: int


def safe_name(title: str) -> str:
    title = re.sub(r'[\x00-\x1f<>:"/\\|?*]', "_", title).strip(" .")[:70].rstrip(" .")
    if not title or title.split(".")[0].upper() in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}:
        return "gallery"
    return title


def image_suffix(prefix: bytes) -> str:
    if prefix.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if prefix.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if prefix.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    if prefix.startswith(b"RIFF") and prefix[8:12] == b"WEBP":
        return ".webp"
    raise RetryableError("返回内容不是支持的图片，可能是访问限制或失效的图片服务器。")


def check_page(html: str) -> BeautifulSoup:
    lower = html.lower()
    if any(text in lower for text in ("you have exceeded your image viewing limits", "you have exceeded your image limit", "image viewing limit exceeded", "excessive pageloads", "temporarily banned")):
        raise DownloadError("站点图片额度已用尽或 IP 被临时限制，请等待额度恢复后重试。")
    if "sad panda" in lower or "you need to log in" in lower:
        raise DownloadError("站点拒绝访问，请检查 Cookie 和账号权限。")
    return BeautifulSoup(html, "html.parser")


def thumbnail_links(soup: BeautifulSoup, gallery: Gallery) -> dict[int, str]:
    links = {}
    for anchor in soup.select("#gdt a[href]"):
        url = urljoin(gallery.url, anchor["href"])
        parts = urlsplit(url)
        match = PAGE_RE.fullmatch(parts.path)
        if parts.scheme == "https" and parts.hostname == gallery.host and match and int(match[1]) == gallery.gid:
            links[int(match[2])] = url
    return links


def pagination(soup: BeautifulSoup, gallery: Gallery) -> int:
    last = 0
    for anchor in soup.select(".ptt a[href]"):
        parts = urlsplit(urljoin(gallery.url, anchor["href"]))
        if parts.hostname != gallery.host or parts.path.rstrip("/") != urlsplit(gallery.url).path.rstrip("/"):
            continue
        page = parse_qs(parts.query).get("p", ["0"])[0]
        if page.isdigit():
            last = max(last, int(page))
    return last


def validate_image_url(url: str) -> str:
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        if parts.scheme not in {"http", "https"} or not host or parts.username or parts.password or not parts.port and parts.netloc.endswith(":"):
            raise ValueError
        if host.lower() == "localhost" or host.lower().endswith((".localhost", ".local", ".internal")) or "." not in host and ":" not in host:
            raise ValueError
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if address is None and re.fullmatch(r"[0-9.]+", host):
            raise ValueError
        if address is not None and not address.is_global:
            raise ValueError
        if host in HOSTS and parts.scheme != "https":
            raise ValueError
    except ValueError:
        raise DownloadError("站点返回了不允许的图片地址。") from None
    if urlsplit(url).path.lower().endswith(("/509.gif", "/509.jpg", "/509.png")):
        raise DownloadError("站点图片额度已用尽，请等待额度恢复后重试。")
    return url


def make_cookies(raw: str) -> httpx.Cookies:
    cookies = httpx.Cookies()
    for entry in raw.strip().split(";"):
        if not entry.strip():
            continue
        key, separator, value = entry.strip().partition("=")
        if not separator or not re.fullmatch(r"[A-Za-z0-9_]+", key) or any(char in value for char in "\r\n"):
            raise ValueError("Cookie 格式错误，应为 name=value; name=value。")
        for host in HOSTS:
            cookies.jar.set_cookie(Cookie(0, key, value, None, False, host, False, False,
                                          "/", True, True, None, True, None, None, {}, False))
    return cookies


class GalleryDownloader:
    def __init__(self, options: Options, *, transport: httpx.AsyncBaseTransport | None = None):
        self.options = options
        self.transport = transport

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            proxy=self.options.proxy_url if self.options.proxy_mode == "custom" else None,
            trust_env=self.options.proxy_mode == "system", cookies=make_cookies(self.options.cookie),
            timeout=httpx.Timeout(self.options.timeout), follow_redirects=False,
            headers={"User-Agent": "Mozilla/5.0 AstrBot-EHDownloader/1.0", "Accept-Encoding": "identity"},
            transport=self.transport,
        )

    @staticmethod
    def check_status(response: httpx.Response):
        if response.status_code in (401, 403):
            raise DownloadError("站点拒绝访问（401/403），请检查账号 Cookie、访问权限和代理。")
        if response.status_code in (404, 410):
            raise DownloadError("画廊或文件不存在，可能已被删除。")
        if response.status_code == 429 or response.status_code >= 500:
            raise RetryableError(f"站点暂时不可用（HTTP {response.status_code}）。")
        if response.status_code != 200:
            raise DownloadError(f"站点请求失败（HTTP {response.status_code}）。")

    async def get_html(self, client: httpx.AsyncClient, url: str) -> str:
        # Gallery/image-page requests may only go to the two official gallery hosts.
        for attempt in range(self.options.retries + 1):
            try:
                current = url
                for _ in range(6):
                    parts = urlsplit(current)
                    if parts.scheme != "https" or parts.hostname not in HOSTS or parts.port not in (None, 443) or parts.username or parts.password:
                        raise DownloadError("画廊跳转到了登录页或不支持的站点，请检查 Cookie。")
                    async with client.stream("GET", current) as response:
                        if response.is_redirect:
                            current = urljoin(current, response.headers.get("location", ""))
                            continue
                        self.check_status(response)
                        body = bytearray()
                        async for chunk in response.aiter_bytes():
                            body.extend(chunk)
                            if len(body) > 4 * MIB:
                                raise DownloadError("画廊页面过大，停止解析。")
                        return body.decode("utf-8", errors="replace")
                raise DownloadError("站点跳转次数过多。")
            except (httpx.RequestError, RetryableError) as exc:
                if attempt == self.options.retries:
                    raise DownloadError(f"页面请求失败：{retry_reason(exc)}。") from None
                await asyncio.sleep(min(2 ** attempt, 8))
        raise AssertionError("unreachable")

    async def metadata(self, client: httpx.AsyncClient, gallery: Gallery) -> dict:
        for attempt in range(self.options.retries + 1):
            try:
                response = await client.post("https://api.e-hentai.org/api.php", json={
                    "method": "gdata", "gidlist": [[gallery.gid, gallery.token]], "namespace": 1,
                })
                self.check_status(response)
                entries = response.json().get("gmetadata", [])
                if not entries or not isinstance(entries[0], dict) or entries[0].get("error"):
                    raise DownloadError("无法获取画廊信息，请检查链接是否有效、画廊是否已删除。")
                data = entries[0]
                count = int(data.get("filecount", 0))
                if count < 1:
                    raise DownloadError("画廊没有可下载的页面。")
                if count > self.options.max_pages:
                    raise DownloadError(f"画廊有 {count} 页，超过配置的 {self.options.max_pages} 页上限。")
                return data
            except (httpx.RequestError, RetryableError) as exc:
                if attempt == self.options.retries:
                    raise DownloadError(f"元数据请求失败：{retry_reason(exc)}。") from None
                await asyncio.sleep(min(2 ** attempt, 8))
            except (ValueError, TypeError, AttributeError):
                raise DownloadError("站点返回的元数据格式不正确。") from None
        raise AssertionError("unreachable")

    async def collect_pages(self, client: httpx.AsyncClient, gallery: Gallery, count: int) -> dict[int, str]:
        soup = check_page(await self.get_html(client, gallery.url))
        links = thumbnail_links(soup, gallery)
        last = pagination(soup, gallery)
        if last >= count or last >= self.options.max_pages:
            raise DownloadError("画廊分页信息异常。")
        for page in range(1, last + 1):
            await asyncio.sleep(self.options.interval)
            soup = check_page(await self.get_html(client, gallery.url + f"?p={page}"))
            links.update(thumbnail_links(soup, gallery))
        if set(links) != set(range(1, count + 1)):
            raise DownloadError("未能收集全部图片页，请检查 Cookie、访问权限；如启用了 MPV，请切换到普通查看器。")
        return links

    async def fetch_image(self, client: httpx.AsyncClient, url: str, referer: str, target: Path, remaining: int) -> Path:
        temporary = target.with_suffix(".part")
        try:
            current = url
            for _ in range(6):
                validate_image_url(current)
                async with client.stream("GET", current, headers={"Referer": referer}) as response:
                    if response.is_redirect:
                        current = urljoin(current, response.headers.get("location", ""))
                        continue
                    if response.status_code in (404, 410) or (response.status_code == 403 and urlsplit(current).hostname not in HOSTS):
                        raise RetryableError(f"图片服务器返回 HTTP {response.status_code}，地址可能失效或被服务器拒绝")
                    self.check_status(response)
                    prefix = bytearray()
                    size = 0
                    with temporary.open("wb") as output:
                        async for chunk in response.aiter_bytes(chunk_size=64 * 1024):
                            size += len(chunk)
                            if size > self.options.max_image_mb * MIB:
                                raise DownloadError("单张图片超过配置的大小上限。")
                            if size > remaining:
                                raise DownloadError("画廊下载体积超过配置的大小上限。")
                            if len(prefix) < 32:
                                prefix.extend(chunk[:32 - len(prefix)])
                            output.write(chunk)
                    try:
                        suffix = image_suffix(bytes(prefix))
                    except RetryableError:
                        mime = response.headers.get("content-type", "").partition(";")[0].strip().lower()
                        if not re.fullmatch(r"[a-z0-9.+-]+/[a-z0-9.+-]+", mime):
                            mime = "未知"
                        raise RetryableError(f"图片服务器返回非图片内容（Content-Type: {mime}），可能是错误页或访问验证页") from None
                    expected = response.headers.get("content-length")
                    if not response.headers.get("content-encoding") and expected and expected.isdigit() and int(expected) != size:
                        raise RetryableError("图片未下载完整。")
                    destination = target.with_suffix(suffix)
                    temporary.replace(destination)
                    return destination
            raise DownloadError("图片跳转次数过多。")
        finally:
            temporary.unlink(missing_ok=True)

    async def download_page(self, client: httpx.AsyncClient, page_url: str, target: Path, remaining: int) -> Path:
        current_page = page_url
        for attempt in range(self.options.retries + 1):
            soup = check_page(await self.get_html(client, current_page))
            image = soup.select_one("#img[src]")
            if image is None:
                raise DownloadError("找不到图片，请检查 Cookie、账号权限或站点页面是否发生变化。")
            image_url = urljoin(page_url, image["src"])
            if self.options.original:
                original = soup.select_one("#i7 a[href*='fullimg.php']")
                if original is not None:
                    image_url = urljoin(page_url, original["href"])
            # Refresh a failed H@H endpoint using the normal viewer's reload token.
            token = reload_token(soup)
            try:
                return await self.fetch_image(client, image_url, page_url, target, remaining)
            except (httpx.RequestError, RetryableError) as exc:
                if attempt == self.options.retries:
                    page = PAGE_RE.fullmatch(urlsplit(page_url).path)
                    number = page[2] if page else "未知"
                    server = urlsplit(image_url)
                    host = server.hostname or "未知"
                    if server.port and server.port not in (80, 443):
                        host += f":{server.port}"
                    raise DownloadError(
                        f"第 {number} 页图片下载失败，共尝试 {attempt + 1} 次（图片服务器：{host}）。"
                        f"最后原因：{retry_reason(exc)}。已下载页面保留，可重新提交相同链接续下。"
                    ) from None
                if token:
                    parts = urlsplit(page_url)
                    query = parse_qs(parts.query)
                    query["nl"] = [token]
                    current_page = urlunsplit(parts._replace(query=urlencode(query, doseq=True)))
                await asyncio.sleep(min(2 ** attempt, 8))
        raise AssertionError("unreachable")

    async def pack(self, archive: Path, images: list[Path], metadata: dict):
        temporary = archive.with_suffix(".zip.part")
        try:
            with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_STORED) as bundle:
                for image in images:
                    with image.open("rb") as source, bundle.open(image.name, "w", force_zip64=True) as destination:
                        while chunk := source.read(MIB):
                            destination.write(chunk)
                            await asyncio.sleep(0)
                bundle.writestr("metadata.json", json.dumps(metadata, ensure_ascii=False, indent=2))
            temporary.replace(archive)
        finally:
            temporary.unlink(missing_ok=True)

    async def download(self, gallery: Gallery, root: Path, progress: Callable[[str, int, int, str], None] | None = None) -> DownloadResult:
        def report(stage: str, done: int, total: int, title: str):
            if progress:
                progress(stage, done, total, title)

        if gallery.host == "exhentai.org" and not self.options.cookie.strip():
            raise DownloadError("ExHentai 需要账号 Cookie，请先在插件配置中填写。")
        root = root.resolve()
        async with self.client() as client:
            data = await self.metadata(client, gallery)
            count = int(data["filecount"])
            title = str(data.get("title_jpn") or data.get("title") or gallery.gid)
            report("收集页面", 0, count, title)
            links = await self.collect_pages(client, gallery, count)
            mode = "original" if self.options.original else "display"
            directory = root / f"{gallery.host}_{gallery.gid}_{gallery.token}_{mode}"
            directory.mkdir(parents=True, exist_ok=True)
            images = []
            size = 0
            for index in range(1, count + 1):
                target = directory / f"{index:04d}"
                cached = None
                for suffix in (".jpg", ".png", ".gif", ".webp"):
                    candidate = target.with_suffix(suffix)
                    if candidate.is_file():
                        try:
                            with candidate.open("rb") as source:
                                valid = image_suffix(source.read(32)) == suffix
                            if valid and candidate.stat().st_size <= self.options.max_image_mb * MIB:
                                cached = candidate
                                break
                        except RetryableError:
                            pass
                        candidate.unlink()
                remaining = self.options.max_gallery_mb * MIB - size
                if cached is None:
                    report("下载图片", index - 1, count, title)
                    await asyncio.sleep(self.options.interval)
                    cached = await self.download_page(client, links[index], target, remaining)
                size += cached.stat().st_size
                if size > self.options.max_gallery_mb * MIB:
                    raise DownloadError("画廊下载体积超过配置的大小上限。")
                images.append(cached)
                report("下载图片", index, count, title)
            report("打包", count, count, title)
            suffix = "_original" if self.options.original else ""
            archive = directory / f"{gallery.gid}_{safe_name(title)}{suffix}.zip"
            await self.pack(archive, images, {**data, "source": gallery.url, "download_mode": mode})
            destination = root / archive.name
            archive.replace(destination)
            # Remove only this gallery's temporary directory after publishing the ZIP.
            if directory.is_symlink() or directory.resolve().parent != root:
                raise DownloadError("临时下载目录不在保存路径下，无法清理。")
            shutil.rmtree(directory)
            archive = destination
            return DownloadResult(archive, title, count, archive.stat().st_size)

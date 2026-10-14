from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import File
from astrbot.api.star import Context, Star, StarTools

from .downloader import DownloadError, DownloadResult, Gallery, GalleryDownloader, MIB, Options

ACTIVE = {"排队中", "收集页面", "下载图片", "打包"}
HELP = """EH 下载插件
/eh下载 <画廊链接>  提交下载（别名 /ehdl）
/eh任务 [任务ID]  查询当前会话的任务
/eh取消 <任务ID>  取消下载
/eh发送 <任务ID>  重新发送已完成的 ZIP
/eh帮助  显示帮助
下载完成后将 ZIP 保存到配置的保存目录，清理临时图片，仅发送文本提示。
支持在 AI 对话中请求下载、查询进度和取消任务。
默认仅管理员可用，可在配置中添加用户白名单。"""


@dataclass
class Job:
    id: str
    gallery: Gallery
    sender: str
    origin: str
    stage: str = "排队中"
    done: int = 0
    total: int = 0
    title: str = ""
    error: str = ""
    result: DownloadResult | None = None
    task: asyncio.Task | None = None

    def describe(self) -> str:
        text = f"{self.id} | {self.stage} | {self.done}/{self.total or '?'} 页"
        if self.title:
            text += f"\n{self.title}"
        if self.result:
            text += f"\n{self.result.archive.name}（{self.result.size / MIB:.1f} MiB）"
        if self.error:
            text += f"\n{self.error}"
        return text


class EHentaiDownloadPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        network = config.get("network_proxy", {})
        download = config.get("download", {})
        self.downloader = GalleryDownloader(Options(
            proxy_mode=network.get("proxy_mode", "system"), proxy_url=network.get("proxy_url", "").strip(),
            cookie=config.get("cookie", "").strip(), original=download.get("original", False),
            timeout=float(download.get("timeout", 45)), retries=int(download.get("retries", 2)),
            interval=float(download.get("interval", 1)), max_pages=int(download.get("max_pages", 500)),
            max_image_mb=int(download.get("max_image_mb", 64)), max_gallery_mb=int(download.get("max_gallery_mb", 512)),
        ))
        concurrency = int(download.get("concurrent_tasks", 1))
        self.max_pending = int(download.get("max_pending_tasks", 5))
        self.max_send_mb = int(download.get("max_send_mb", 100))
        if not (1 <= concurrency <= 3 and 1 <= self.max_pending <= 20 and 1 <= self.max_send_mb <= 10240):
            raise ValueError("并发任务数、任务上限或发送大小上限超出配置范围。")
        self.slots = asyncio.Semaphore(concurrency)
        save_path = config.get("save_path", "").strip()
        self.root = (
            Path(save_path).expanduser().resolve()
            if save_path
            else Path(StarTools.get_data_dir("astrbot_plugin_ehentai_download")) / "downloads"
        )
        self.jobs: dict[str, Job] = {}
        self.closing = False

    def allowed(self, event: AstrMessageEvent) -> bool:
        allowed_users = {str(user) for user in self.config.get("allowed_users", [])}
        return event.is_admin() or not self.config.get("admin_only", True) or str(event.get_sender_id()) in allowed_users

    def visible(self, event: AstrMessageEvent, job: Job) -> bool:
        return job.origin == event.unified_msg_origin and (event.is_admin() or job.sender == str(event.get_sender_id()))

    async def notify(self, event: AstrMessageEvent, message: str):
        try:
            await event.send(event.plain_result(message))
        except Exception as exc:
            logger.warning("EH 下载通知发送失败（%s）", type(exc).__name__)

    async def send_archive(self, event: AstrMessageEvent, job: Job):
        result = job.result
        if result is None or not result.archive.is_file():
            await self.notify(event, "下载文件不存在，请重新提交链接。")
            return
        if result.size > self.max_send_mb * MIB:
            await self.notify(event, f"任务 {job.id} 已下载，ZIP 超过 {self.max_send_mb} MiB 的发送上限，已保存在机器人服务器。"
                              + (f"\n{result.archive}" if event.is_admin() else "\n请联系管理员获取文件。"))
            return
        try:
            await event.send(event.chain_result([File(name=result.archive.name, file=str(result.archive.resolve()))]))
        except Exception as exc:
            logger.warning("EH 文件发送失败：任务 %s（%s）", job.id, type(exc).__name__)
            await self.notify(event, f"任务 {job.id} 已下载，但文件发送失败或平台不支持文件消息。可用 /eh发送 {job.id} 重试。"
                              + (f"\n服务器文件：{result.archive}" if event.is_admin() else "\n请联系管理员获取文件。"))

    async def run_job(self, event: AstrMessageEvent, job: Job):
        def progress(stage: str, done: int, total: int, title: str):
            job.stage, job.done, job.total, job.title = stage, done, total, title

        try:
            async with self.slots:
                job.stage = "收集页面"
                job.result = await self.downloader.download(job.gallery, self.root, progress)
                job.stage = "已完成"
                await self.notify(event, f"任务 {job.id} 下载完成：{job.result.pages} 页，{job.result.size / MIB:.1f} MiB。"
                                  f"\nZIP 已保存：{job.result.archive.name}"
                                  + (f"\n保存路径：{job.result.archive}" if event.is_admin() else ""))
        except asyncio.CancelledError:
            job.stage = "已取消"
            if not self.closing:
                await self.notify(event, f"任务 {job.id} 已取消。已下载页面保留，重新提交相同链接可续下。")
            raise
        except DownloadError as exc:
            failed_stage = job.stage
            job.stage, job.error = "失败", str(exc)
            logger.warning("EH 下载失败：任务 %s，阶段 %s，原因 %s", job.id, failed_stage, job.error)
            await self.notify(event, f"任务 {job.id} 下载失败：{job.error}")
        except Exception as exc:
            # Exception strings can contain proxy passwords, Cookie values or signed URLs.
            logger.error("EH 下载失败：任务 %s（%s）", job.id, type(exc).__name__)
            job.stage, job.error = "失败", "下载发生异常，请检查磁盘空间、配置及依赖。"
            await self.notify(event, f"任务 {job.id}：{job.error}")

    def submit_download(self, event: AstrMessageEvent, url: str) -> str:
        """Submit from either a command or an LLM tool without stopping the event."""
        if not self.allowed(event):
            return "仅管理员或配置白名单中的用户可使用下载插件。"
        if self.closing:
            return "插件正在停止，请稍后重试。"
        if not url:
            return HELP
        try:
            gallery = Gallery.parse(url)
        except DownloadError as exc:
            return str(exc)
        active = [job for job in self.jobs.values() if job.stage in ACTIVE]
        if any(job.gallery == gallery for job in active):
            return "该画廊已有下载任务，请等待完成。"
        if len(active) >= self.max_pending:
            return f"已有 {self.max_pending} 个未完成任务，请稍后再提交。"
        # Bound in-memory task history; files remain in the plugin data directory.
        for key in list(self.jobs):
            if len(self.jobs) < 100:
                break
            if self.jobs[key].stage not in ACTIVE:
                del self.jobs[key]
        job = Job(uuid4().hex[:12], gallery, str(event.get_sender_id()), event.unified_msg_origin)
        self.jobs[job.id] = job
        job.task = asyncio.create_task(self.run_job(event, job), name=f"eh-download-{job.id}")
        return f"已提交下载任务 {job.id}。用 /eh任务 {job.id} 查看进度，/eh取消 {job.id} 取消。"

    def describe_jobs(self, event: AstrMessageEvent, job_id: str = "") -> str:
        if not self.allowed(event):
            return "你没有使用下载插件的权限。"
        jobs = [job for job in self.jobs.values() if self.visible(event, job) and (not job_id or job.id == job_id)]
        return "\n\n".join(job.describe() for job in jobs[-10:]) or "没有找到可查看的任务（任务记录在重启后清空）。"

    async def cancel_download(self, event: AstrMessageEvent, job_id: str) -> str:
        job = self.jobs.get(job_id)
        if not self.allowed(event) or job is None or not self.visible(event, job):
            return "任务不存在或你没有操作权限。"
        elif job.task is None or job.task.done():
            return "任务已结束。"
        else:
            job.task.cancel()
            # Wait for file handles and the gallery slot to be released before permitting a new job.
            await asyncio.gather(job.task, return_exceptions=True)
            # Cancelling a task before its first step bypasses its exception handler.
            job.stage = "已取消"
            return f"已请求取消任务 {job.id}。"

    @filter.command("eh下载", alias={"ehdl"})
    async def download(self, event: AstrMessageEvent, url: str = ""):
        """下载指定 E-Hentai / ExHentai 画廊，完成后保存 ZIP 并发送文本提示。"""
        event.stop_event()
        yield event.plain_result(self.submit_download(event, url))

    @filter.command("eh任务", alias={"ehstatus"})
    async def status(self, event: AstrMessageEvent, job_id: str = ""):
        """查询下载任务；不填 ID 时展示当前会话最近的任务。"""
        event.stop_event()
        yield event.plain_result(self.describe_jobs(event, job_id))

    @filter.command("eh取消", alias={"ehcancel"})
    async def cancel(self, event: AstrMessageEvent, job_id: str):
        """取消当前会话中有权限管理的下载任务。"""
        event.stop_event()
        yield event.plain_result(await self.cancel_download(event, job_id))

    @filter.llm_tool(name="ehentai_download")
    async def download_tool(self, event: AstrMessageEvent, url: str) -> str:
        """用户请求下载 E-Hentai 或 ExHentai 画廊时，提交后台下载任务。

        立即返回提交结果，提交成功不代表下载完成；请向用户告知任务 ID。
        完成后 ZIP 保存到配置目录，自动清理临时图片，并在当前会话发送文本提示，不发送文件。
        查询进度请使用 ehentai_download_status，不要重复提交或循环轮询。

        Args:
            url(string): 用户提供的完整 HTTPS 画廊链接，格式 https://e-hentai.org/g/编号/令牌/，也支持 exhentai.org。缺少链接时先向用户询问，不要编造。
        """
        return self.submit_download(event, url)

    @filter.llm_tool(name="ehentai_download_status")
    async def status_tool(self, event: AstrMessageEvent, job_id: str = "") -> str:
        """用户询问 EH 画廊下载进度或结果时，查询当前会话中有权限查看的任务。

        仅按返回的实际阶段报告结果，不要将排队或下载中说成完成，也不要循环轮询。

        Args:
            job_id(string): 下载工具返回的任务 ID；留空或省略时列出当前会话最近最多十个可见任务。
        """
        return self.describe_jobs(event, job_id)

    @filter.llm_tool(name="ehentai_download_cancel")
    async def cancel_tool(self, event: AstrMessageEvent, job_id: str) -> str:
        """用户请求取消 EH 画廊下载时，取消当前会话中有权限管理的指定任务。

        已完整下载的图片会保留，之后可重新提交相同链接续下。

        Args:
            job_id(string): 要取消的任务 ID，使用下载或查询工具返回的真实 ID；不明确时先查询任务。
        """
        return await self.cancel_download(event, job_id)

    @filter.command("eh发送", alias={"ehfile"})
    async def resend(self, event: AstrMessageEvent, job_id: str):
        """重新发送已下载的 ZIP。"""
        event.stop_event()
        job = self.jobs.get(job_id)
        if not self.allowed(event) or job is None or not self.visible(event, job):
            yield event.plain_result("任务不存在或你没有操作权限。")
        elif job.stage in ACTIVE or job.result is None:
            yield event.plain_result("任务尚未生成 ZIP。")
        else:
            await self.send_archive(event, job)

    @filter.command("eh帮助", alias={"ehhelp"})
    async def help(self, event: AstrMessageEvent):
        """显示 EH 下载插件的用法。"""
        event.stop_event()
        yield event.plain_result(HELP)

    async def terminate(self):
        self.closing = True
        tasks = [job.task for job in self.jobs.values() if job.task and not job.task.done()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

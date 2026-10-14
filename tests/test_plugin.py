"""Exercise plugin task management using minimal AstrBot API stand-ins.

These tests do not claim to replace integration tests against an IM adapter.
"""

import asyncio
import importlib.util
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from downloader import DownloadResult


class FakeEvent:
    def __init__(self, sender="admin", admin=True, origin="test:group:1"):
        self.sender = sender
        self.admin = admin
        self.unified_msg_origin = origin
        self.sent = []
        self.fail_files = False
        self.stopped = False

    def is_admin(self):
        return self.admin

    def get_sender_id(self):
        return self.sender

    def stop_event(self):
        self.stopped = True

    def plain_result(self, value):
        return value

    def chain_result(self, value):
        return value

    async def send(self, value):
        if self.fail_files and isinstance(value, list):
            raise RuntimeError("sensitive exception text")
        self.sent.append(value)


def load_plugin(root):
    class FakeStar:
        def __init__(self, context):
            pass

    class FakeFile:
        def __init__(self, name, file):
            self.name, self.file = name, file

    class FakeLogger:
        def warning(self, *args):
            pass

        def error(self, *args):
            pass

    modules = {key: types.ModuleType(key) for key in [
        "astrbot", "astrbot.api", "astrbot.api.event", "astrbot.api.message_components", "astrbot.api.star",
    ]}
    modules["astrbot.api"].AstrBotConfig = dict
    modules["astrbot.api"].logger = FakeLogger()
    modules["astrbot.api.event"].AstrMessageEvent = FakeEvent
    modules["astrbot.api.event"].filter = types.SimpleNamespace(
        command=lambda *args, **kwargs: lambda function: function,
        llm_tool=lambda *args, **kwargs: lambda function: function,
    )
    modules["astrbot.api.message_components"].File = FakeFile
    modules["astrbot.api.star"].Star = FakeStar
    modules["astrbot.api.star"].Context = object
    modules["astrbot.api.star"].StarTools = types.SimpleNamespace(get_data_dir=lambda _: root)
    package_name = "_eh_plugin_test"
    package = types.ModuleType(package_name)
    package.__path__ = [str(Path(__file__).resolve().parents[1])]
    modules[package_name] = package
    spec = importlib.util.spec_from_file_location(f"{package_name}.main", Path(package.__path__[0]) / "main.py")
    module = importlib.util.module_from_spec(spec)
    modules[spec.name] = module
    with patch.dict(sys.modules, modules):
        spec.loader.exec_module(module)
    return module


async def collect(generator):
    return [message async for message in generator]


class FakeDownloader:
    def __init__(self, root, *, block=False, error=None):
        self.root = root
        self.block = block
        self.error = error
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0
        self.active = 0
        self.max_active = 0

    async def download(self, gallery, root, progress):
        self.calls += 1
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            progress("下载图片", 1, 2, "test title")
            self.started.set()
            if self.block:
                await self.release.wait()
            if self.error:
                raise self.error
            root.mkdir(parents=True, exist_ok=True)
            archive = root / f"{gallery.gid}.zip"
            archive.write_bytes(b"fake zip")
            return DownloadResult(archive, "test title", 2, archive.stat().st_size)
        finally:
            self.active -= 1


class PluginTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.addCleanup(self.temporary.cleanup)
        self.module = load_plugin(self.root)
        self.plugin = self.module.EHentaiDownloadPlugin(object(), {})
        self.event = FakeEvent()
        self.plugin.downloader = FakeDownloader(self.root)

    async def asyncTearDown(self):
        await self.plugin.terminate()

    async def submit(self, gid=42, event=None):
        return await collect(self.plugin.download(event or self.event, f"https://e-hentai.org/g/{gid}/abcdef1234/"))

    async def test_configured_save_path_and_cookie(self):
        destination = self.root / "custom downloads" / "nested"
        self.plugin = self.module.EHentaiDownloadPlugin(object(), {
            "save_path": f" {destination} ",
            "cookie": " ipb_member_id=123; ipb_pass_hash=test-secret ",
        })
        self.assertEqual(self.plugin.downloader.options.cookie, "ipb_member_id=123; ipb_pass_hash=test-secret")
        self.plugin.downloader = FakeDownloader(self.root)
        await self.submit()
        job = next(iter(self.plugin.jobs.values()))
        await job.task
        self.assertEqual(job.stage, "已完成")
        self.assertEqual(job.result.archive.parent, destination.resolve())
        self.assertTrue(job.result.archive.is_file())

    async def test_permissions_and_whitelist(self):
        user = FakeEvent(sender="user", admin=False)
        messages = await self.submit(event=user)
        self.assertIn("白名单", messages[0])
        self.assertEqual(len(self.plugin.jobs), 0)
        self.plugin.config["allowed_users"] = ["user"]
        await self.submit(event=user)
        job = next(iter(self.plugin.jobs.values()))
        await job.task
        self.assertEqual(job.stage, "已完成")
        self.assertEqual(len(user.sent), 1)
        self.assertIsInstance(user.sent[0], str)
        self.assertIn("下载完成", user.sent[0])
        self.assertIn("42.zip", user.sent[0])
        self.assertNotIn(str(self.plugin.root), user.sent[0])

    async def test_duplicate_and_queue_bound(self):
        self.plugin.downloader = FakeDownloader(self.root, block=True)
        self.plugin.max_pending = 2
        await self.submit()
        await self.plugin.downloader.started.wait()
        messages = await self.submit()
        self.assertIn("已有下载任务", messages[0])
        await self.submit(gid=43)
        messages = await self.submit(gid=44)
        self.assertIn("未完成任务", messages[0])
        self.assertEqual(len(self.plugin.jobs), 2)
        self.plugin.downloader.release.set()
        await asyncio.gather(*(job.task for job in self.plugin.jobs.values()))
        self.assertEqual(self.plugin.downloader.max_active, 1)

    async def test_cancel_queued_job_before_first_step(self):
        await self.submit()
        job = next(iter(self.plugin.jobs.values()))
        await collect(self.plugin.cancel(self.event, job.id))
        self.assertEqual(job.stage, "已取消")
        self.assertTrue(job.task.done())
        await self.submit()
        second = list(self.plugin.jobs.values())[-1]
        await second.task
        self.assertEqual(second.stage, "已完成")

    async def test_cancel_active_job_releases_slot(self):
        self.plugin.downloader = FakeDownloader(self.root, block=True)
        await self.submit()
        await self.plugin.downloader.started.wait()
        job = next(iter(self.plugin.jobs.values()))
        await collect(self.plugin.cancel(self.event, job.id))
        self.assertEqual(job.stage, "已取消")
        self.assertEqual(self.plugin.downloader.active, 0)
        self.plugin.downloader.release.set()
        await self.submit(gid=43)
        second = list(self.plugin.jobs.values())[-1]
        await second.task
        self.assertEqual(second.stage, "已完成")

    async def test_other_user_and_session_cannot_view_or_cancel(self):
        self.plugin.config["admin_only"] = False
        self.plugin.downloader = FakeDownloader(self.root, block=True)
        await self.submit(event=FakeEvent(sender="alice", admin=False))
        await self.plugin.downloader.started.wait()
        job = next(iter(self.plugin.jobs.values()))
        for event in [FakeEvent(sender="bob", admin=False), FakeEvent(origin="test:group:2")]:
            status = await collect(self.plugin.status(event, job.id))
            self.assertNotIn("test title", status[0])
            cancel = await collect(self.plugin.cancel(event, job.id))
            self.assertIn("权限", cancel[0])
            self.assertFalse(job.task.cancelled())

    async def test_failure_can_be_resubmitted(self):
        # Match the plugin's module-local exception identity.
        self.plugin.downloader = FakeDownloader(self.root, error=self.module.DownloadError("site rejected"))
        await self.submit()
        job = next(iter(self.plugin.jobs.values()))
        await job.task
        self.assertEqual(job.stage, "失败")
        self.assertIn("site rejected", self.event.sent[-1])
        self.plugin.downloader.error = None
        await self.submit()
        second = list(self.plugin.jobs.values())[-1]
        await second.task
        self.assertEqual(second.stage, "已完成")

    async def test_generic_exception_does_not_expose_secrets(self):
        self.plugin.downloader = FakeDownloader(self.root, error=RuntimeError("cookie=secret"))
        await self.submit()
        job = next(iter(self.plugin.jobs.values()))
        await job.task
        self.assertEqual(job.stage, "失败")
        self.assertNotIn("secret", "".join(str(value) for value in self.event.sent))

    async def test_completion_only_notifies_and_manual_resend_remains_available(self):
        self.event.fail_files = True
        await self.submit()
        job = next(iter(self.plugin.jobs.values()))
        await job.task
        self.assertEqual(job.stage, "已完成")
        self.assertTrue(job.result.archive.is_file())
        self.assertEqual(len(self.event.sent), 1)
        self.assertIsInstance(self.event.sent[0], str)
        self.assertIn("下载完成", self.event.sent[0])
        self.assertIn(str(job.result.archive), self.event.sent[0])
        self.event.fail_files = False
        await collect(self.plugin.resend(self.event, job.id))
        component = self.event.sent[-1][0]
        self.assertEqual(Path(component.file), job.result.archive)

    async def test_oversized_file_is_retained_without_upload(self):
        await self.submit()
        job = next(iter(self.plugin.jobs.values()))
        await job.task
        job.result = DownloadResult(job.result.archive, "test", 2, 101 * 1024 * 1024)
        self.event.sent.clear()
        await self.plugin.send_archive(self.event, job)
        self.assertEqual(len(self.event.sent), 1)
        self.assertIn("发送上限", self.event.sent[0])
        self.assertTrue(job.result.archive.is_file())

    async def test_tool_submit_returns_immediately_and_completion_is_text(self):
        self.plugin.downloader = FakeDownloader(self.root, block=True)
        result = await asyncio.wait_for(self.plugin.download_tool(
            self.event, "https://e-hentai.org/g/42/abcdef1234/",
        ), 1)
        job = next(iter(self.plugin.jobs.values()))
        self.assertIn(job.id, result)
        self.assertIn("已提交", result)
        self.assertFalse(job.task.done())
        self.assertFalse(self.event.stopped)
        self.assertEqual(self.event.sent, [])
        await self.plugin.downloader.started.wait()
        status = await self.plugin.status_tool(self.event, job.id)
        self.assertIn("下载图片", status)
        self.assertNotIn("已完成", status)
        self.plugin.downloader.release.set()
        await job.task
        self.assertIn("已完成", await self.plugin.status_tool(self.event))
        self.assertEqual(len(self.event.sent), 1)
        self.assertIsInstance(self.event.sent[0], str)
        self.assertIn("下载完成", self.event.sent[0])
        self.assertFalse(self.event.stopped)

    async def test_tools_enforce_permissions_and_accept_whitelist(self):
        user = FakeEvent(sender="user", admin=False)
        url = "https://e-hentai.org/g/42/abcdef1234/"
        self.assertIn("白名单", await self.plugin.download_tool(user, url))
        self.assertIn("权限", await self.plugin.status_tool(user))
        self.assertIn("权限", await self.plugin.cancel_tool(user, "unknown"))
        self.assertEqual(self.plugin.jobs, {})
        self.plugin.config["allowed_users"] = ["user"]
        result = await self.plugin.download_tool(user, url)
        job = next(iter(self.plugin.jobs.values()))
        self.assertIn(job.id, result)
        await job.task
        self.assertIn("已完成", await self.plugin.status_tool(user, job.id))
        self.assertNotIn(str(self.plugin.root), user.sent[0])

    async def test_tools_share_command_validation_duplicates_and_queue(self):
        self.assertIn("HTTPS", await self.plugin.download_tool(self.event, "https://example.com/g/42/token/"))
        self.assertEqual(self.plugin.jobs, {})
        self.plugin.downloader = FakeDownloader(self.root, block=True)
        self.plugin.max_pending = 1
        await self.submit()
        self.assertIn("已有下载任务", await self.plugin.download_tool(
            self.event, "https://e-hentai.org/g/42/abcdef1234/",
        ))
        self.assertIn("未完成任务", await self.plugin.download_tool(
            self.event, "https://e-hentai.org/g/43/abcdef1234/",
        ))
        self.assertEqual(len(self.plugin.jobs), 1)
        self.plugin.closing = True
        self.assertIn("正在停止", await self.plugin.download_tool(
            self.event, "https://e-hentai.org/g/44/abcdef1234/",
        ))

    async def test_tool_status_and_cancel_respect_user_and_session(self):
        self.plugin.config["admin_only"] = False
        self.plugin.downloader = FakeDownloader(self.root, block=True)
        owner = FakeEvent(sender="alice", admin=False)
        await self.plugin.download_tool(owner, "https://e-hentai.org/g/42/abcdef1234/")
        await self.plugin.downloader.started.wait()
        job = next(iter(self.plugin.jobs.values()))
        for event in (FakeEvent(sender="bob", admin=False), FakeEvent(origin="test:group:2")):
            self.assertNotIn(job.id, await self.plugin.status_tool(event, job.id))
            self.assertIn("权限", await self.plugin.cancel_tool(event, job.id))
            self.assertFalse(job.task.done())
            self.assertFalse(event.stopped)
        self.assertIn(job.id, await self.plugin.status_tool(self.event, job.id))
        result = await self.plugin.cancel_tool(owner, job.id)
        self.assertIn("取消", result)
        self.assertEqual(job.stage, "已取消")
        self.assertTrue(job.task.done())
        self.assertEqual(self.plugin.downloader.active, 0)
        self.assertIn("已结束", await self.plugin.cancel_tool(owner, job.id))
        self.assertFalse(owner.stopped)

    async def test_tool_cancel_before_job_starts(self):
        await self.plugin.download_tool(self.event, "https://e-hentai.org/g/42/abcdef1234/")
        job = next(iter(self.plugin.jobs.values()))
        result = await self.plugin.cancel_tool(self.event, job.id)
        self.assertIn(job.id, result)
        self.assertEqual(job.stage, "已取消")
        self.assertTrue(job.task.cancelled())
        self.assertFalse(self.event.stopped)

    async def test_terminate_cancels_every_job_and_suppresses_notifications(self):
        self.plugin.downloader = FakeDownloader(self.root, block=True)
        await self.submit()
        await self.submit(gid=43)
        await self.plugin.downloader.started.wait()
        self.event.sent.clear()
        await self.plugin.terminate()
        self.assertTrue(all(job.task.done() for job in self.plugin.jobs.values()))
        self.assertEqual(self.plugin.downloader.active, 0)
        self.assertEqual(self.event.sent, [])


if __name__ == "__main__":
    unittest.main()

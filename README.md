# AstrBot EH 画廊下载插件

参考 [yasdiox/astrbot_plugin_ehentai_metadata](https://github.com/yasdiox/astrbot_plugin_ehentai_metadata) 的链接格式、代理配置和 `gdata` 元数据查询方式，独立实现画廊下载功能。可与参考插件同时使用，无需 HTML 渲染器、浏览器或翻译数据库。

## 功能

- 接收 E-Hentai / ExHentai 的 HTTPS 画廊链接，收集全部分页后逐张下载图片。
- 默认下载网页展示图，可配置优先使用页面上的原图下载链接。
- 按阅读顺序命名图片（`0001.jpg` 等），打包 ZIP，附带 `metadata.json`。
- 后台任务排队、进度查询、取消和文件重发；同一画廊的活动任务去重。
- 支持模型工具调用，可在 AI 对话中提交下载、查询进度和取消任务。
- 网络失败时退避重试，并使用图片页的 `nl` 重载令牌切换失效图片地址。
- 下载完成后将 ZIP 移到 `save_path` 根目录，删除临时下载文件夹，仅发送文本提示。
- 失败、取消或重启后重新提交同一链接，会复用临时目录中已完整下载的图片。半张图片从头下载。
- 支持账号 Cookie、环境变量代理、自定义 HTTP/SOCKS5 代理和直连。
- 可配置权限、页数、体积和手动发送上限。

这是逐页下载后本地打包，不调用需要支付 GP/Credits 的 Archive Download，也不下载种子。图片浏览和原图访问仍受站点账号权限、额度及频率限制。仅下载你有权访问和保存的内容。

## 安装

需要 Python 3.10+，以及提供 `StarTools.get_data_dir` 的 AstrBot 4.x。建议使用当前稳定版 AstrBot。

将以下文件放到 AstrBot 的 `data/plugins/astrbot_plugin_ehentai_download/` 目录：

```text
astrbot_plugin_ehentai_download/
├── main.py
├── downloader.py
├── metadata.yaml
├── _conf_schema.json
├── requirements.txt
├── README.md
└── LICENSE
```

在 **AstrBot 实际使用的 Python 环境**中安装依赖，再重新加载插件：

```bash
python -m pip install -r data/plugins/astrbot_plugin_ehentai_download/requirements.txt
```

也可在 AstrBot 插件管理中使用本地上传功能安装 `dist/astrbot_plugin_ehentai_download-v1.0.2.zip`，或将本目录作为独立仓库发布后通过仓库链接安装。当前 `metadata.yaml` 未填写仓库地址，发布后再补上实际 `repo`，不要使用参考插件的仓库地址。

## 配置

在 AstrBot 管理面板中打开本插件的配置。默认仅管理员可使用，管理员身份在 AstrBot 中设置。若需要给其他用户开放，填写 `allowed_users`；关闭 `admin_only` 则向所有用户开放。

两个顶层自定义参数按 [AstrBot 插件配置文档](https://docs.astrbot.app/dev/star/guides/plugin-config.html) 在 `_conf_schema.json` 中定义，由 AstrBot 注入的 `AstrBotConfig` 读取：

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `save_path` | 空字符串 | ZIP 的保存根目录；留空使用插件数据目录下的 `downloads` |
| `cookie` | 空字符串 | 账号 Cookie 请求头的值；管理面板以密码输入框显示 |

`save_path` 可填写绝对路径，例如 Windows 的 `D:/EHDownloads` 或 Linux 的 `/data/ehdownloads`。也支持 `~` 和相对路径；相对路径以 AstrBot 的工作目录为基准。目录会在下载时自动创建，每个画廊使用一个临时子目录；完成后 ZIP 移到保存根目录，临时子目录自动删除。请使用 AstrBot 进程有写权限的路径，Docker 部署应填写容器内的路径。修改保存路径不会迁移旧文件，新下载及缓存查找会使用新目录。

网络无法直连时，将 `network_proxy.proxy_mode` 改为 `custom`，`proxy_url` 填为 `http://127.0.0.1:7890` 等实际代理地址。`system` 读取代理环境变量，不会自动读取 Windows 系统设置中的代理；`none` 禁用代理环境变量。Docker 内的 `127.0.0.1` 是容器自身，应填写容器可访问的代理地址。

需要登录时，在浏览器登录目标站点，从自己的请求中取得 Cookie 请求头的值，填入插件的 `cookie` 字段。例如：

```text
ipb_member_id=你的值; ipb_pass_hash=你的值; igneous=你的值
```

不要填 `Cookie:` 前缀，也不要提供整个浏览器请求头。ExHentai 需要账号 Cookie，且账号本身需要有访问权限；填写 Cookie 不能替代站点权限。插件仅对 E-Hentai / ExHentai 域名的 HTTPS 请求附带配置的 Cookie，不会将其附带到第三方图片服务器请求中。

`cookie` 的 `secret` 属性只遮罩管理面板的显示，不加密配置文件。保存路径和 Cookie 修改后请重新加载插件。

| 配置 | 默认值 | 说明 |
| --- | --- | --- |
| `download.original` | `false` | 优先原图；未提供原图链接时使用展示图；原图请求失败时停止 |
| `download.interval` | `1.0` | 逐页取图、画廊分页之间的间隔，单位秒 |
| `download.timeout` | `45` | 每次网络读写超时，单位秒 |
| `download.retries` | `2` | 网络错误或临时服务器错误后的重试次数 |
| `download.max_pages` | `500` | 单画廊页数上限，最大 2000 |
| `download.max_image_mb` | `64` | 单张图片大小上限，单位 MiB |
| `download.max_gallery_mb` | `512` | 单画廊图片总大小上限，单位 MiB |
| `download.max_send_mb` | `100` | `/eh发送` 手动发送 ZIP 的大小上限，单位 MiB |
| `download.concurrent_tasks` | `1` | 同时下载的画廊数，最大 3 |
| `download.max_pending_tasks` | `5` | 运行中和排队中的任务总数上限 |

下载参数在插件加载时读取，修改后请重新加载插件。站点返回 401/403、画廊删除、图片额度耗尽或临时封禁时会停止，避免无限重试。

## 使用

```text
/eh下载 https://e-hentai.org/g/画廊编号/画廊令牌/
/ehdl https://exhentai.org/g/画廊编号/画廊令牌/
/eh任务
/eh任务 任务ID
/eh取消 任务ID
/eh发送 任务ID
/eh帮助
```

命令前缀取决于 AstrBot 的唤醒设置；默认示例使用 `/`。一次提交一个链接，支持带 `?p=...` 等查询参数的画廊链接，下载始终从整本第一页开始。图片详情页 `/s/...` 链接不支持。

提交后立即返回任务 ID，用 `/eh任务` 查看阶段和已完成页数；完成后在提交任务的会话发送文本提示，包含页数、ZIP 大小和文件名，管理员还可看到保存路径。普通用户只能查看、取消和手动发送自己在当前会话提交的任务，管理员可管理当前会话的所有任务。进度只在查询时发送，避免逐页刷屏。

## 模型工具调用

按 [AstrBot AI 文档](https://docs.astrbot.app/dev/star/guides/ai.html#通过装饰器定义-tool-和注册-tool) 的 `@filter.llm_tool` 方式注册三个工具，重新加载插件后由 AstrBot 自动加载：

| 工具名 | 参数 | 功能 |
| --- | --- | --- |
| `ehentai_download` | `url`：完整 HTTPS 画廊链接 | 提交后台下载，立即返回提交结果和任务 ID |
| `ehentai_download_status` | `job_id`：任务 ID，可省略或留空 | 查询指定任务，或当前会话最近最多十个可见任务 |
| `ehentai_download_cancel` | `job_id`：任务 ID | 取消指定任务，保留已完整下载的图片 |

在 AstrBot 中配置支持工具调用（Function Calling）的聊天模型，并在当前人格的工具配置中允许使用上述工具。之后可直接对机器人说：

```text
帮我下载这个画廊：https://e-hentai.org/g/画廊编号/画廊令牌/
查看我的下载任务进度
取消下载任务 任务ID
```

工具与命令共用权限、队列、活动任务去重和任务记录。默认仍仅管理员或白名单用户能下载，普通用户只能查询和取消自己在当前会话提交的任务。工具提交后由模型回复任务 ID；下载继续在后台运行，完成后保存 ZIP、清理临时目录，并在原会话发送文本提示。提交成功不表示下载已完成。模型工具不发送文件。

工具参数按文档要求通过 docstring 的 `Args:` 段声明。工具直接返回文本供模型继续回复，只有命令处理器调用 `event.stop_event()`。不需要给本插件单独配置模型 API Key。

## 文件保存和恢复

未配置 `save_path` 时，数据保存在 AstrBot 的：

```text
data/plugin_data/astrbot_plugin_ehentai_download/downloads/
  画廊编号_标题.zip
```

下载过程中，图片缓存在 `e-hentai.org_画廊编号_画廊令牌_display/` 子目录中；`original` 模式使用单独临时目录，ZIP 文件名增加 `_original` 后缀。ZIP 只有在全部页面下载完成后才生成，临时文件以 `.part` 结尾；失败或取消不会生成缺页 ZIP。打包成功后，将 ZIP 移到保存根目录，再删除该画廊的临时目录。图片已压缩，ZIP 使用存储模式，避免重复压缩占用 CPU。

配置 `save_path` 后，ZIP 直接保存在自定义路径下。

任务列表仅保存在内存，最多保留最近 100 条，插件重载或 AstrBot 重启后清空。失败或取消的任务保留完整图片，重新提交链接即可续下；成功后只保留 ZIP，再次提交会重新下载图片。同一画廊、标题和模式的 ZIP 会在新下载成功后替换。打包过程中图片加 ZIP 通常需要接近两份图片的磁盘空间。

下载完成后仅发送文本提示。如需手动获取文件，可用 `/eh发送` 通过 AstrBot 的 `File` 组件发送，实际上传能力和大小限制取决于适配器。NapCat / OneBot 等独立部署时，适配器需要能读取 AstrBot 文件路径，必要时共享数据卷。也可由管理员直接从服务器取回文件；本插件不会公开文件下载地址。

## 验证和故障排查

本项目提供离线测试，不需要 Cookie，也不会访问真实画廊：

```bash
python -m unittest discover -s tests -v
```

44 项离线测试覆盖下载器和使用 AstrBot API 替身的任务管理，包括模型工具的后台提交、权限与会话隔离、队列去重、取消，以及 ZIP 移动和临时目录清理、仅文本完成通知、自定义保存目录、重载图片服务器和错误原因诊断。生成插件安装包：

```bash
python scripts/build_package.py
```

- 找不到完整图片页：检查是否登录、Cookie 是否过期，以及是否启用了 MPV。建议在站点设置中使用普通查看器。
- 额度或临时限制：等待站点恢复；缩小并发、增加间隔。插件不能绕过这些限制。
- 原图请求失败：确认账号能在浏览器打开该原图链接，或关闭优先原图下载展示图。
- 自定义 SOCKS 代理报依赖错误：确认在 AstrBot 所用环境安装了 `httpx[socks]`。
- 第一张图片下载失败：画廊页面和图片可能来自不同服务器。v1.0.2 起错误提示包含页码、图片服务器、重试次数和最后原因。`ProxyError` 表示代理连接或隧道失败；`ReadTimeout` 表示响应读取超时；HTTP 403/404 表示图片服务器拒绝或地址失效。先在浏览器打开提交的画廊链接，点击第一张缩略图，确认同一代理能否访问图片。
- 图片服务器失效：插件会读取重载按钮自身或子元素中的 `nl` 令牌，刷新图片页后重试。不会绕过账号权限或站点额度限制。
- 任务发生异常：检查磁盘空间、配置类型和依赖版本。日志只记录异常类型和任务 ID，避免输出凭证或带令牌的图片地址。

实现参照 [E-Hentai API 文档](https://ehwiki.org/wiki/API) 和 [AstrBot 插件事件文档](https://docs.astrbot.app/dev/star/guides/listen-message-event.html)。测试使用合成图片和页面，实际站点访问及聊天平台上传仍需部署后验证。

## 许可证

MIT。参考项目为 yasdiox 编写的 MIT 许可元数据插件；本插件独立实现下载器，不依赖其源文件。

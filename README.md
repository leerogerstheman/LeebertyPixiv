# LeebertyPixiv — pixiv 关键词爬虫（原图下载 + 可检索图库）

> ## 🧪 测试版（BETA）
>
> 当前为**测试版**：核心功能已可用，仍在持续迭代，**后续会有大量修改**。
> pixiv 随时可能改接口（程序已做结构校验，改版时会明确报错）；界面/文档也会调整。
> 遇到问题请提 [Issue](https://github.com/leerogerstheman/LeebertyPixiv/issues)，附上日志最好。

**纯 Python 标准库实现。** 输入关键词 → 抓取 pixiv 搜索结果 → 下载**原图** → 按标签/画师/关键词
建立**硬链接分类**（不占额外空间）→ 生成 CSV / Markdown / SQLite 索引 → 图库多维检索 + 图形界面。

## 快速上手

**Windows 用户（推荐，无需装 Python）**：到 [Releases](https://github.com/leerogerstheman/LeebertyPixiv/releases)
下载 `PixivCrawler-windows.zip` → 解压 → 双击 `PixivCrawler.exe`。

**已装 Python**：`git clone` 本仓库后双击 `gui.bat`，或命令行：

```powershell
python pixiv_crawler.py auth login              # ① 登录 pixiv（推荐；不登也能用，但每种排序只有约 600 个作品）
python pixiv_crawler.py crawl 初音ミク --pages 5 # ② 关键词爬取并下载原图
python pixiv_crawler.py follow sync             # ③ 或追更你订阅的画师（增量）
python pixiv_crawler.py search 初音 --tag 雪ミク  # ④ 检索已下载的图库
python pixiv_crawler.py gui                     # 图形界面
```

## 功能一览

| 能力 | 说明 |
| --- | --- |
| **三种检索** | 关键词 / 画师ID（走作品全集接口，不受翻页上限）/ 全量分段（按时间切分，突破约 6000 条上限） |
| **筛选** | 时间范围（年月日三级联动）、点赞/收藏门槛、**R-18 / R-18G 独立开关**、**AIGC 排除/只要** |
| **动图** | ugoira 转 webp / gif（用 ffmpeg），可自动删原始 zip |
| **画师追更** | 订阅清单（`artists.txt`）+ 增量下载 + 上次追更时间；GUI「追更」页一键同步、进度条、安全停止后续跑 |
| **图库检索** | 原TAG（多选+自动补全）/ **画师ID（独立列表区：按作品数排序、可多选、自动补全）** / 衍生TAG（你自己的标签层）/ 时间 / 人气 / 分级 / AIGC；单击缩略图预览、双击打开 |
| **衍生标签** | 每张图可加自定义标签；每次爬取自动附「第 N 次爬取」溯源标签；原TAG只读 |
| **爬取历史** | 每次爬取的序号/时间/检索项/结果明细，「第N次」全局计数 |
| **可靠性** | 接口结构校验（改版响亮报错）、限流自适应、断点续爬、缺页自动修复（`repair`）、自检（`selftest`） |

**图形界面 6 个标签页**：爬取 / 追更 / 图库检索 / 图库位置 / 读取能力（自动体检+三种登录方式）/ 爬取历史。

## 登录（三种方式）

| 方式 | 用法 | 说明 |
| --- | --- | --- |
| **① OAuth（推荐）** | `auth login --method token` | 浏览器打开 pixiv 授权页登录（账号密码只输入在 pixiv 页面），程序拿长期有效的 refresh_token；GUI「读取能力」页也有同款按钮 |
| ② 自动读浏览器 | `auth login --method auto-cookie` | 从本机浏览器读 cookie；需先完全退出浏览器，新版 Edge/Chrome 可能受系统保护读不出 |
| ③ 手动粘贴 | `auth login --method cookie` | F12 → Application → Cookies 里复制 `PHPSESSID` 粘贴；任何环境可用 |

不登录也能用：可搜索、可下原图，但每种排序约 600 个上限、无 R-18。
凭据保存在 `~/.pixiv_crawler/credentials.json`（用户目录，不写进项目）；refresh_token 轮换会自动保存。
**凭据等价于登录态**：config.json / credentials.json 请勿外传或提交（已 gitignore）。

## 画师追更

```powershell
python pixiv_crawler.py follow add 73260619 四宮いずな   # 订阅画师（ID 或主页链接）
python pixiv_crawler.py follow list                      # 看清单
python pixiv_crawler.py follow sync                      # 增量下载新作品（可 --dry-run 先看）
```
清单纯文本 `library/artists.txt`，每行 `画师ID|画师名|上次追更时间|备注`，可直接编辑。

## 常用命令速查

```powershell
python pixiv_crawler.py crawl 初音ミク                # 默认收最新 1 页
python pixiv_crawler.py crawl 初音ミク --pages 5      # 翻 5 页
python pixiv_crawler.py crawl 初音ミク --deep         # 深度：最新+最早两段
python pixiv_crawler.py crawl 初音ミク --full         # 全量：按时间段切分尽量取全（先 --dry-run 看量级）
python pixiv_crawler.py crawl 初音ミク --dry-run      # 干跑：只报会下多少，不下载
python pixiv_crawler.py crawl 初音ミク --date-from 2024-01-01 --date-to 2024-12-31
python pixiv_crawler.py crawl 初音ミク --min-likes 1000
python pixiv_crawler.py crawl 初音ミク --keep-r18     # 收 R-18（默认跳过）
python pixiv_crawler.py crawl 初音ミク --ai-exclude   # 排除 AI 生成
python pixiv_crawler.py crawl --artist-id 73260619    # 按画师ID爬全部作品
python pixiv_crawler.py search 初音 --tag 雪ミク       # 检索图库
python pixiv_crawler.py repair                        # 补齐缺失页/文件
python pixiv_crawler.py repair --clean-tags           # 清洗拼接标签（如"初音ミク,"）
python pixiv_crawler.py reindex --relink              # 重建索引/分类链接
python pixiv_crawler.py stats                         # 图库统计
python pixiv_crawler.py selftest                      # 自检（--offline 跳过网络）
```

完整参数用 `python pixiv_crawler.py crawl --help` 查看。

## 已知限制

1. **不登录只能搜约 600 个/排序**；要更多、要 R-18 请登录（见上）。
2. **pixiv 改接口时程序会明确报错**（不会静默假成功），修复需更新版本；接口地址可在 `config.json` 的 `endpoints` 覆盖。
3. 全量模式爬热门关键词可能占大量磁盘——先 `--dry-run` 或 GUI「预估」按钮看量级。
4. R-18G（猎奇）默认跳过，需 `--keep-r18g` 单独放开。
5. Windows 完整支持；Linux/macOS 可用命令行（无动图转码需自装 ffmpeg）。

## 从源码运行

```
Python 3.9+，无需 pip install 任何第三方包（动图转码需要单独的 ffmpeg，见 docs/）
```

配置文件 `config.json`：改 proxy、输出目录、并发、限速门槛等，详见文件内注释。

---

测试版说明见 [RELEASE_NOTES.md](./RELEASE_NOTES.md)。发现 bug 或有想法 → [提 Issue](https://github.com/leerogerstheman/LeebertyPixiv/issues)。
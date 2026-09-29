# pixiv 关键词爬虫（原图下载 + 可检索分类库）

> ## 🧪 测试版（BETA）
>
> 当前版本为**测试版**：核心功能已可用，但仍在持续迭代中，**后续会有大量修改**。
> 已知边界：pixiv 随时可能改接口（程序已做接口结构校验，改版时会明确报错）、
> 界面文案可能调整、GitHub Release 可能追加新构建。
> 建议：**定期 git pull 获取更新**；遇到问题请提 issue，最好附上日志与报错信息。

**纯 Python 标准库实现，零第三方依赖、零 pip 安装**：
输入关键词 → 抓取 pixiv 搜索结果 → 下载**原图** → 按标签/画师/关键词建立**硬链接分类**（不占额外空间）
→ 生成 CSV / Markdown / SQLite(FTS5) 索引 → 可命令行检索，也可用图形界面。

**五种使用方式**（Windows 用户推荐 exe 或 gui.bat）：

| 方式 | 适合谁 | 怎么用 |
| --- | --- | --- |
| 🖥 **图形界面 exe**（测试版发布形态） | 不想碰命令行的用户 | 下载 [GitHub Release](https://github.com/leerogerstheman/pixiv-crawler/releases) 里的 `PixivCrawler-windows.zip`，解压后双击 `PixivCrawler.exe` |
| 🪟 ** gui.bat** | 已装 Python 的用户 | 双击 `gui.bat`（自动找 Python） |
| 💻 命令行 | 喜欢命令行的用户 | 见下方命令 |
| 🐍 源码运行 | 开发者 / 想改代码 | `python pixiv_crawler.py gui` |

```powershell
python pixiv_crawler.py auth login                 # ① 登录 pixiv（见第二节）
python pixiv_crawler.py crawl 初音ミク --pages 5    # ② 关键词爬取并下载原图
python pixiv_crawler.py follow sync                # ③ 或者追更你订阅的画师
python pixiv_crawler.py search 初音 --tag 雪ミク     # ④ 检索已下载的图库
python pixiv_crawler.py gui                        # 或者用图形界面（Windows 双击 gui.bat）
```

> 亮点：**实测**登录后单个关键词可获取量从匿名的 598 个提升到 **6000 以上**；
> 支持**画师追更**（订阅制增量下载）、**动图转 webp/gif**、时间范围（年月日三级下拉）、
> 点赞/收藏门槛、R-18 与 R-18G 独立开关、干跑侦察、断点续爬、缺页自动修复、失败熔断。

---

## 一、环境要求与安装

| 项目 | 要求 |
| --- | --- |
| Python | **3.9+**（开发与实测环境为 3.12；无需 pip install 任何包） |
| 操作系统 | Windows 完整支持（含图形界面与一键脚本）；Linux / macOS 可用命令行模式 |
| 网络 | 能访问 `pixiv.net` / `i.pximg.net`；不通时可在配置里填 `proxy` |

```bash
git clone <this-repo> pixiv-crawler && cd pixiv-crawler
python pixiv_crawler.py selftest          # 先自检：环境/网络/凭据/索引逐项检查，不下载图片
```

Windows 用户也可直接双击 `crawl.bat` / `search.bat` / `gui.bat` / `selftest.bat`。

---

## 二、登录 pixiv（推荐，可获取量提升 10 倍）

pixiv 的搜索接口对**未登录**用户只提供每种排序大约 600 个作品（实测第 11 页起全部重复）。
登录后这个限制消失（实测单一排序翻到第 100 页仍是新作品）。

```powershell
python pixiv_crawler.py auth login      # 交互式，会列出三种方式让你选
python pixiv_crawler.py auth status     # 查看当前登录状态与凭据来源
python pixiv_crawler.py auth doctor     # 体检：实际测出你能读多少（登录 vs 匿名对比）
python pixiv_crawler.py auth logout     # 退出登录（删除凭据库）
```

> 不想碰命令行？图形界面里有同级的「**读取能力**」标签页，打开就自动体检，
> 检测不通过会直接给你按钮和图文步骤（从浏览器导入 / 手动填 PHPSESSID）。

### 三种登录方式

| 方式 | 命令 | 说明 |
| --- | --- | --- |
| **① refresh_token（推荐）** | `auth login --method token` | 走 pixiv 官方 **OAuth + PKCE**，程序引导你打开授权页、粘贴回调地址。凭证长期有效，用 App API，可获取量最大 |
| ② 自动读取浏览器 | `auth login --method auto-cookie` | 从本机浏览器 Cookie 库直接读取，**需先完全退出浏览器**；新版 Edge/Chrome 可能读不出来（见下） |
| ③ 手动粘贴 | `auth login --method cookie` | 从 DevTools 复制 `PHPSESSID` 粘贴进来，任何环境都可用 |

### 方式①的完整流程（`auth login`）

```
第 1 步：在浏览器里打开下面的地址
  https://app-api.pixiv.net/web/v1/login?code_challenge=...&client=pixiv-android
第 2 步：用你的 pixiv 账号登录
第 3 步：登录后浏览器会跳到 .../auth/pixiv/callback?code=XXXX（页面显示 404 或空白是正常的）
第 4 步：把浏览器地址栏里的完整地址复制下来，粘贴到终端
```

程序用 `code` + PKCE 换取 refresh_token 并保存。**账号密码只输入在 pixiv 页面上，程序不接触你的密码。**

### 方式③的取值位置

浏览器登录 pixiv → `F12` → **Application / 应用程序** → 左侧 **Storage → Cookies → https://www.pixiv.net**
→ 在筛选框输入 `PHPSESSID` → 双击「值」列 → 复制 → 粘进终端。

> 为什么不能全自动？Edge 127+ / Chrome 127+ 对 cookie 启用了 **v20「应用绑定加密」**，
> 密钥受 SYSTEM 级 DPAPI 保护，**非管理员权限无法解密**（本仓库实测确认）。
> 所以程序提供 OAuth 与手动粘贴两条路：前者完全不需要碰 cookie，后者只读取你主动粘贴的内容。
> 内置的 AES-256-GCM 解密实现（`browser_cookie.py`，用于方式②）用官方测试向量验证过：
> `python browser_cookie.py` 会跑 FIPS-197 与 GCM 官方向量自检。

### 仓库文件一览

| 文件 | 作用 |
| --- | --- |
| `pixiv_crawler.py` | 主程序：`crawl` / `follow` / `search` / `auth` / `stats` / `repair` / `selftest` / `reindex` / `gui` |
| `ugoira.py` | 动图下载与转码（帧率换算 + ffmpeg 调用；自带自检 `python ugoira.py`） |
| `artists.py` | 画师订阅清单的读写（纯文本 `artists.txt`，自带自检 `python artists.py`） |
| `browser_cookie.py` | 从本机浏览器读取 cookie 的实现（AES-GCM + DPAPI，零依赖，自带官方向量自检） |
| `test_dates.py` | 时间筛选回归测试（解析、边界、闰年、整年/整月语义） |
| `test_libsearch.py` | 图库检索回归测试（标签多选、点赞/收藏门槛、时间范围、R-18 分级、排序） |
| `test_libloc.py` | 图库位置切换回归测试（写配置、重载、可写性校验、索引读取） |
| `test_daterange_gui.py` | 两页时间选择器回归测试（下拉布局、年月联动、闰年、与检索联动） |
| `test_capability_gui.py` | 读取能力页回归测试（自动体检、四项渲染、按钮可用性、凭据路径） |
| `test_overlap.py` | 组合重叠自检回归测试（中文列宽计算、去重汇总、deep 组合统计） |
| `probe_depth.py` | 实测脚本：登录/匿名下各排序能翻多深、deep 组合的实际增益 |
| `config.json` | 默认配置（**请勿把填了凭据的版本提交到 git**，已在 `.gitignore` 中排除） |
| `crawl.bat` / `search.bat` / `gui.bat` / `selftest.bat` | Windows 一键入口 |

### 凭据存放在哪里

登录成功后凭据保存在**用户目录**（不写进项目里的 `config.json`，避免被误提交）：

```
~/.pixiv_crawler/credentials.json      # Windows: C:\Users\<你>\.pixiv_crawler\credentials.json
```

类 Unix 系统会设为 `0600` 权限。凭据来源优先级（后者覆盖前者）：

```
config.json  <  凭据库 ~/.pixiv_crawler/credentials.json  <  环境变量  <  命令行参数
```

CI / 脚本场景可用环境变量注入：`PIXIV_REFRESH_TOKEN` / `PIXIV_PHPSESSID`。

`refresh_token` 每次刷新后 pixiv 都会轮换，程序**会自动把新 token 存回凭据库**，无需手动更新。

> ⚠️ 凭据等价于登录态：`credentials.json` 与 `config.json` 请勿外传、勿提交到 git
> （本仓库的 `.gitignore` 已排除它们）。

---

## 三、30 秒上手

### 图形界面（推荐给不熟悉命令行的用户）

双击 `gui.bat`，界面分五个标签页：

| 标签页 | 能做什么 |
| --- | --- |
| **爬取** | 关键词、页数、排序、深度模式、干跑侦察；**内容分级三个独立勾选框（全年龄 / R-18 / R-18G，可自由组合）**；**AIGC 选择（不限 / 排除 AI / 只要 AI）**；**「预估」按钮**（先算量级再决定要不要爬）；发布时间的**年/月/日三级下拉**（互相联动、自动适配闰年）；点赞/收藏门槛；动图格式与是否保留 zip；**进度条 + 当前任务指示 + 停止按钮** |
| **追更** | 订阅的**画师清单**（名字/ID/上次追更时间/备注），图形化管理：添加（支持链接或逗号分隔多个）、移除选中、**同步选中**或**同步全部**（增量下载：只下载上次以后的新作品，已下载的自动跳过）；干跑预览；**进度条 + 当前任务指示 + 停止按钮**（停止=安全保存后停手，下次同步自动续跑）；清单是纯文本 `artists.txt`，可直接编辑 |
| **图库检索** | 在已下载的图库里组合筛选：**原TAG列表可勾选一个或多个**（显示每个标签的图片数）＋**衍生TAG列表**（用户加的那一层，含自动「第N次爬取」）、点赞数/收藏数下限、**画师ID（精确匹配）**、**发布时间（与爬取页相同的年/月/日三级下拉）**、**分级（与爬取页一致的 7 档，状态栏会提示"另有 N 条被分级隐藏"）**、**AIGC（不限/排除/只要）**、画师名、检索词、排序方式；**「编辑衍生标签…」对话框**（原TAG只读展示提醒不修改，衍生TAG可增删、可一键导入原TAG）；**多页作品在 ID 列标注「×N页」**（避免一图多页看着像重复）；结果表格可**单击预览**（PNG/GIF 缩略图+元信息）、**双击打开图片**、一键打开所在文件夹、导出 CSV；**追更同步完成后自动跳转到这里并按该批画师筛选**（先清空旧筛选，不会叠加出空结果） |
| **图库位置** | 查看图库在电脑中的**具体位置**（含目录是否存在、记录数、作品数、占用大小、磁盘剩余空间、索引文件路径；并说明"记录按图计、作品按 pixiv 作品计"，多页作品一目了然）；**浏览/输入新位置后一键切换**（写回 `config.json` 并立即生效）；快捷打开 `by_tag` / `by_author` / `by_keyword` / `_originals` / `_meta` 各子目录；重建索引 |
| **读取能力** | **打开即自动体检**：实际发请求测出你当前能读多少内容，四项探测 —— ① 登录凭据（来源/长度/位置）② 登录态（是谁、是否开了成人内容）③ **可读取量（登录 vs 匿名实翻对比）** ④ 原图下载权。任何一项不通过都会给出**具体修复步骤**，并直接提供三种修复按钮：从浏览器自动导入 Cookie、手动填 PHPSESSID、**用 pixiv 官网登录（OAuth，账号密码不落盘、凭据可吊销）**；页面底部列出完整的凭据优先级与真实文件路径 |
| **爬取历史** | 每次爬取的明细：**第N次（全局累计，自动标签用）**、开始时间、模式（关键词/画师/全量/追更）、检索项、下载/失败数、耗时、自动标签；可刷新、可清空 |

底部状态栏常驻显示当前图库位置与记录数。

> 两个页面的时间选择器是**同一份实现**（`build_date_range`）：年月日三级下拉、互相联动、
> 自动适配闰年（2024-02 有 29 天、2023-02 只有 28 天）、可只选到年或月（=整年/整月）、
> 起止填反会红字提示。改任意一级都会立即重新筛选/重新计算范围回显。

#### 内容分级：三个勾选框自由组合（6 种）

「全年龄 / R-18 / R-18G（猎奇）」三个框可任意勾选，共 **6 种有意义组合**，
界面下方实时回显它会用什么服务器模式：

| 勾选 | 服务器 mode | 实际爬取 |
| --- | --- | --- |
| 全年龄 + R-18 + R-18G | `all` | 全都要（默认） |
| 全年龄 + R-18 | `all` | 排除猎奇 |
| R-18 + R-18G | `r18` | 只要 R18 系 |
| 全年龄 + R-18G | `all` | 跳过 R-18 |
| 只勾全年龄 | `safe` | 只要全年龄 |
| 只勾 R-18 | `r18` | 只要 R-18 |
| 只勾 R-18G | `r18` | **只要猎奇**（以前做不到） |
| 一个都不勾 | — | 界面会警告并拒绝开始 |

pixiv 的搜索 `mode` 只有三档（all/safe/r18），无法在服务器上区分 R-18 与 R-18G，
所以策略是**选一个能覆盖所选、又尽量小的模式，剩下的在本地精筛**（见 `r18_plan()`）。

#### AIGC（AI 生成）筛选

三档：`不限` / `排除 AI` / `只要 AI`。数据来自搜索列表自带的 `aiType` 字段，
**不额外消耗请求**。

> ⚠️ **实测提醒**：「初音ミク」在默认排序下**前 8 页（480 条）全是 AI 作品**，
> 排除 AI 后整个前 300 页采样都找不到非 AI 内容。这是 pixiv 的排序结果，不是程序问题。
> 所以开启「排除 AI」时，程序会**跳跃采样**先定位到有可用内容的深度再顺序翻
> （实测能定位到第 25 / 100 页），并在完全找不到时明确提示，而不是静默返回 0。

#### 按画师 ID 检索

两处都支持，填纯数字 ID 或主页链接（`https://www.pixiv.net/users/12345`）都认，
可填多个（逗号分隔）：

* **爬取页**：`画师ID` 输入框／命令行 `--artist 12345`
  —— 在关键词搜索结果里**只保留这些画师**的作品
* **图库检索页**：`画师ID` 输入框／命令行 `search --artist-id 12345`
  —— 精确匹配库里的 `author_id`，常用于"这张图的作者还有哪些图"

```powershell
python pixiv_crawler.py crawl 初音ミク --artist 5961223
python pixiv_crawler.py crawl 初音ミク --artist 5961223 --artist 54550225
python pixiv_crawler.py search --artist-id 5961223
```

> **实测**：pixiv 搜索接口**不支持**按画师过滤 —— `uid`/`user_id`/`userId`/`mid`/
> `artist_id`/`user` 这些参数名都会被静默忽略，返回结果与不传**完全一样**。
> 所以爬取侧的画师筛选是**在搜索结果里筛**（列表阶段就筛掉，不发多余的详情请求），
> 因此受关键词与翻页上限约束（单一排序约 6180 条）。
>
> **想拿某位画师的全部作品**（不受关键词与翻页上限限制），用「画师追更」更合适：
>
> ```powershell
> python pixiv_crawler.py follow add https://www.pixiv.net/users/5961223
> python pixiv_crawler.py follow sync
> ```
>
> 它走的是 `/ajax/user/{id}/profile/all`，实测能把某画师的 632 个作品全部拿到。

### 命令行

方式 A：直接双击 **`crawl.bat`**，按提示键入关键词（如 `初音ミク`），回车。

方式 B：在本目录打开 PowerShell（或 CMD）：

```powershell
python pixiv_crawler.py crawl 初音ミク              # 爬 1 页（约 60 个作品）
python pixiv_crawler.py crawl 初音ミク --pages 3    # 爬 3 页
python pixiv_crawler.py crawl 初音ミク 風景 猫       # 一次爬多个关键词
python pixiv_crawler.py                             # 不带参数 = 交互式键入关键词
python pixiv_crawler.py gui                         # 图形界面（或双击 gui.bat）
```

> 入口一览：`crawl.bat`（爬取）、`search.bat`（检索）、`gui.bat`（图形界面）、`selftest.bat`（自检）。

**第一次使用建议先双击 `selftest.bat`**：它会逐项检查 Python 环境、配置文件、图库目录是否可写、pixiv 网络连通性、登录凭据是否有效、已下载图库的索引完整性、分类硬链接是否正常，**不会下载任何图片**。哪一步有问题会直接提示怎么处理，例如网络不通时会告诉你加代理：

```powershell
python pixiv_crawler.py selftest              # 完整自检（含联网测试）
python pixiv_crawler.py selftest --offline    # 只做本地检查，不发任何网络请求
```

**爬取规模怎么定（先侦察再下载）**：

```powershell
python pixiv_crawler.py crawl 初音ミク --dry-run            # 干跑：只统计能拿到多少，不下载
python pixiv_crawler.py crawl 初音ミク --deep --pages 60     # 深度模式：组合多种排序，扩大覆盖面
python pixiv_crawler.py crawl 初音ミク --limit 200           # 只收 200 个作品
python pixiv_crawler.py crawl 初音ミク --restart             # 忘掉"已爬完"的记忆，重新翻页
```

`--dry-run` 会如实告诉你三件事：pixiv 报告多少结果、接口实际能给多少、本次会下载多少。看到数字满意再去掉 `--dry-run` 真跑。

## 四、画师追更（长期使用的主力模式）

关键词搜索是"一次性"的：今天搜「初音ミク」拿到一批，明天再搜还是那些的更新版本。
**追更模式是"订阅制"的**——把你喜欢的画师加进清单，之后反复跑同一个命令，
它只下载索引里还没有的新作品。

```powershell
python pixiv_crawler.py follow add https://www.pixiv.net/users/73260619 四宮いずな
python pixiv_crawler.py follow add 21391270 --name らいおん --note 动图作者
python pixiv_crawler.py follow list                # 看看订了哪些
python pixiv_crawler.py follow sync --dry-run      # 先看看会下多少（不下载）
python pixiv_crawler.py follow sync                # 真正追更
python pixiv_crawler.py follow sync --limit 20     # 每个画师这次最多下 20 个新作品
python pixiv_crawler.py follow sync 73260619       # 只追更指定画师
python pixiv_crawler.py follow remove 73260619     # 取消订阅
```

**实测的增量效果**（两位画师，跑两次）：

| 画师 | 作品总数 | 第一次发现新增 | 第二次发现新增 |
| --- | ---: | ---: | ---: |
| 四宮いずな | 50 | 49 | **46**（50 − 1 已有 − 3 刚下） |
| らいおん | 632 | 631 | **628** |

订阅清单是**纯文本**，可以直接编辑、也能纳入 git：

```
library/artists.txt
# 画师ID|画师名|上次追更时间|备注
73260619|四宮いずな|2026-09-28T23:41:43+08:00|
21391270|らいおん|2026-09-28T23:42:20+08:00|动图作者
```

配合系统定时任务（Windows 任务计划 / cron）就能做到"画师一更新就自动收"：

```powershell
# 每天 20:00 追更一次（Windows 任务计划里填这个命令即可）
python pixiv_crawler.py follow sync
```

> 追更能拿到画师的**完整作品列表**（实测某画师 632 个作品全部拿到），
> 不受关键词搜索"每种排序只给一段"的限制，这是它比关键词模式更适合长期使用的原因。

---

## 五、动图（ugoira）与转码

pixiv 的动图**不是视频也不是 GIF**，而是一个 **ZIP 包**：里面是 N 张静态帧，
每帧停留多久由接口单独给出。所以解压出来只是"一堆连续画面"，**本身不会动**。

```powershell
# 默认只收原始 zip（不动）
python pixiv_crawler.py crawl 初音ミク --keep-ugoira

# 转成 webp（推荐：1600 万色 + 支持透明，体积通常只有 GIF 的 1/3~1/5）
python pixiv_crawler.py crawl 初音ミク --keep-ugoira --ugoira-format webp

# 转 GIF（最通用，但只有 256 色）
python pixiv_crawler.py crawl 初音ミク --keep-ugoira --ugoira-format gif

# 转完删掉原始 zip（省空间；一个 1080p 动图 zip 常有几十 MB）
python pixiv_crawler.py crawl 初音ミク --keep-ugoira --ugoira-format webp --drop-ugoira-zip
```

也可在 `config.json` 里长期开启：

```json
"skip_ugoira": false,
"ugoira_format": "webp",
"ugoira_keep_zip": true,
"ffmpeg_path": ""
```

**转码依赖 ffmpeg（可选）**：程序会依次在 `ffmpeg_path` 配置、系统 PATH、程序目录里找。
找不到时**不会失败**，而是保留原始 zip 并打印安装方式：

```
[未找到 ffmpeg，无法转码（已保留原始 zip）。安装方式：
    Windows:  winget install Gyan.FFmpeg  …]
```

安装方式：

| 系统 | 命令 |
| --- | --- |
| Windows | 下载 [gyan.dev essentials 版](https://www.gyan.dev/ffmpeg/builds/) 解压到 `D:\ffmpeg`，把 `ffmpeg_path` 指向 `D:\ffmpeg\bin\ffmpeg.exe`；或 `winget install Gyan.FFmpeg` |
| macOS | `brew install ffmpeg` |
| Ubuntu | `sudo apt install ffmpeg` |

> 本机实测环境：**ffmpeg 9.0.2-essentials 装在 `D:\ffmpeg\bin\ffmpeg.exe`**，
> 所需编码器 `libwebp_anim` / `gif` / `libx264` / `libvpx-vp9` 均已确认可用。

**实测转码结果**（同一动图：24 帧 / 3000 ms / 原始 zip 4.82 MB）：

| 格式 | 产物大小 | 压缩到 | 帧数 | 时长 | 说明 |
| --- | ---: | ---: | ---: | ---: | --- |
| **webp** | **0.49 MB** | 10% | **24**（原始帧数） | **3.000s**（精确） | 推荐；保留逐帧时长 |
| gif | 1.79 MB | 37% | 49 | 3.060s | 最通用 |
| mp4 | 0.31 MB | 6% | 49 | 3.062s | 体积小，无透明 |
| webm | 0.21 MB | 4% | 49 | 3.063s | 体积最小 |

**帧率是怎么算的（会影响播放速度，这点很多工具做错）**：pixiv 给的是每帧的毫秒数，
程序取所有 delay 的**最大公约数** g，用 `1000/g` 作为帧率以精确还原每帧时长
（例：150 帧 × 33ms → 30.303 fps，而不是粗暴按 30fps 播）。
但纯 gcd 会有个坑：实测某动图 delay 都是 10 的倍数（g=10），算出 **100fps**，
于是 24 帧被展开成 300 帧、webp 产物从 4.8 MB 膨胀到 **81 MB**、播放还不准。
因此现在加了上限：**输出帧率不超过"平均帧率 × 2"**。修正后同一动图变成 16fps / 48 帧 /
0.49 MB，时长误差 2% 以内。

另外，动图会**自动抽出第一帧作为封面**，这样 `by_tag/` 等分类目录里能直接预览，
而不是只看到一个打不开内容的 zip。

---

## 六、筛选作品（时间范围 + 人气门槛）
```powershell
# 只收 2024 年发布的作品
python pixiv_crawler.py crawl 初音ミク --date-from 2024-01-01 --date-to 2024-12-31 --pages 60

# 只收点赞 1000 以上、且是 2023 年之后的作品
python pixiv_crawler.py crawl 初音ミク --date-from 2023-01-01 --min-likes 1000 --pages 60

# 先干跑看看这些条件筛完还剩多少
python pixiv_crawler.py crawl 初音ミク --date-from 2007-01-01 --date-to 2010-12-31 --dry-run
```

图形界面（`gui.bat`）第二行是**三级下拉**，不用手打日期：

```
发布时间：从 [年▾] 年 [月▾] 月 [日▾] 日   到 [年▾] 年 [月▾] 月 [日▾] 日
          （结束日含当天；月/日留「不限」代表整年或整月）
```

三级下拉互相适配，都实测过：

| 你的操作 | 界面行为 |
| --- | --- |
| 年份选「不限」 | 月、日自动清空并**置灰**（禁用） |
| 选了年份 | 月变为可选，日仍禁用 |
| 选了月份 | 日变为可选；**日的候选天数按年月自动调整**（2024-02 只有 29 天，2023-02 只有 28 天，闰年正确） |
| 先选 1 月 31 日，再把月份改成 4 月 | 31 号越界，自动收回为「不限」，不会留下无效日期 |
| 取消年份 | 月、日一起清空并禁用 |
| 只选到年 / 只选到年月 | **= 整年 / 整月**（结束侧的"整月"包含该月最后一天） |
| 起始晚于结束 | 下方**红字**警告「⚠ 起始时间不早于结束时间，不会做时间筛选」 |

第四行会实时回显实际生效范围，例如：

```
从 2015-07　到 2015　—　生效范围 2015-07-01 00:00 ~ 2016-01-01 00:00
```

年月日三级的完整语义（实测验证）：

- 只选 **2015** ~ **2015** = 覆盖整个 2015 年（2015-01-01 00:00 ~ 2016-01-01 00:00）
- 只选 **2015-07** ~ **2015-07** = 覆盖整个 7 月（2015-07-01 00:00 ~ 2015-08-01 00:00）
- 只选 **2015-07-15** ~ **2015-07-15** = 只收那一天

**「点赞数」和「收藏数」是两个不同的指标**（实测数据）：

| 指标 | pixiv 官方名 | 网页上在哪 | API 字段 |
| --- | --- | --- | ---: |
| 点赞数 | いいね / Like | 爱心图标旁边 | `likeCount` |
| 收藏数 | ブックマーク / Bookmark | 书签按钮旁边 | `bookmarkCount` |

采样 12 个作品：收藏合计 14,925、点赞合计 17,445（比值 0.86），**没有"谁一定更大"**——
2007 年的老作品是点赞更多（8,856 收藏 vs 10,938 点赞，因为"点赞"功能比"收藏"晚推出），
刚发布几小时的新作则常常是收藏略多。所以两个门槛都提供，按你的偏好选一个或都用。

检索已下载的图库：

```powershell
python pixiv_crawler.py search 初音                  # 检索词同时匹配 标题/画师/标签/关键词/作品ID
python pixiv_crawler.py search --tag 雪ミク           # 按标签精确筛选（可写多次，取交集）
python pixiv_crawler.py search --author 四宮          # 按画师模糊筛选
python pixiv_crawler.py search --query 初音ミク       # 只看当初用这个关键词爬到的
python pixiv_crawler.py search --list-tags 40        # 列出图库高频标签
python pixiv_crawler.py search ミク --open-first     # 检索并直接打开第一条
python pixiv_crawler.py search -i                    # 交互式输入检索词
python pixiv_crawler.py stats                        # 图库统计
```

也可以直接双击 **`search.bat`**，键入关键词即可。

---

## 七、分类法（怎么组织、怎么检索）

一张图会被放进**多个**分类目录，方便从任意角度找到它。原件只存一份，分类目录里放的是**硬链接**（同一磁盘上不额外占用空间，实测 `nlink=6` 就是六个入口共享同一份数据）。

```
D:\PixivCrawler\library\
├─ _originals\                      ← 原图原件，唯一物理副本
│      画师名_画师ID\{作品ID}_p{页码}.{ext}
│      例：ドーピングドラゴン_101709021\150192138_p0.jpg
│
├─ by_tag\初音ミク\                  ← ① 按标签（最常用的检索入口）
├─ by_tag\雪ミク\                        一个作品可同时出现在多个标签目录里
├─ by_tag\VOCALOID\
│
├─ by_author\四宮いずな_73260619\    ← ② 按画师（名字带 ID，重名也不会混）
├─ by_keyword\初音ミク\              ← ③ 按你当初键入的爬取关键词
├─ by_keyword\雪ミク\                   跨关键词重复出现时同样会登记
│
├─ _meta\{作品ID}_p{页码}.json       ← 每张图的档案：标题/画师/标签/原图直链/作品页地址
└─ _index\                          ← 检索索引
        records.jsonl                    主索引（每行一条记录，可增量追加）
        index.csv                        表格，可直接用 Excel 打开
        index.md                         带目录链接的总览，浏览器/Markdown 阅读器可点
        catalog.sqlite                   SQLite 库（含 FTS5 全文索引）
        queries.csv                      爬取历史（关键词/数量/时间）
```

分类目录里的文件名排序规则：

```
0100_画师_标题_作品ID_p0.jpg
└┬─┘ └────── 一眼可读的信息 ──────┘
 └─ 序号 = 该关键词下的搜索排名 × 100 + 页码
    0100 = 第 1 名第 1 张，0200 = 第 2 名第 1 张，0201 = 第 2 名第 2 张
```

所以在资源管理器里按名称排序，顺序就等于 pixiv 上的搜索排名顺序；而 `_originals` 里按作品 ID 归档，永不重名。

**用 SQLite 查（可选，更快更灵活）**

```python
import sqlite3
c = sqlite3.connect(r"D:\PixivCrawler\library\_index\catalog.sqlite")
c.execute("select id,title,author from works_fts where works_fts match ?", ("ミク",)).fetchall()   # 全文检索
c.execute("select id,title from works where tags_flat like ? and author like ?", ("%雪ミク%", "%四宮%")).fetchall()   # 组合条件
```

---

## 八、配置文件 `config.json`

默认输出目录就是 `D:\PixivCrawler\library`，开箱即用。常改的几项：

| 配置项 | 作用 |
| --- | --- |
| `output_dir` | 图库放在哪（默认 D 盘） |
| `cookie_phpsessid` | 填了可搜到更多内容、可下 R-18（取法见文件内注释） |
| `refresh_token` | 与 cookie 二选一；填了改用官方 App API 搜索 |
| `proxy` | 直连不通时填，如 `http://127.0.0.1:7890`（填完可以跑 `selftest.bat` 确认通了） |
| `pages` / `order` | 默认翻页数与排序（date=最新 / popular=热门 / old=最早） |
| `skip_r18` / `skip_r18g` | 内容分级过滤（命令行用 `--r18` 更直观，见上方速查）；两个都默认 `true` |
| `ai_mode` | AIGC 过滤：`all` / `exclude` / `only` |
| `date_from` / `date_to` | 只收这个时间段内发布的作品。可写 `2024`（整年）、`2024-07`（整月）、`2024-07-15`（当天）、`"2024-07-15 18:30"`（精确到分钟）；留空=不限。结束侧包含当天 |
| `min_likes` / `min_bookmarks` | 点赞数（いいね）/ 收藏数（ブックマーク）门槛，`0`=不限 |
| `max_pages_per_work` | 每个作品最多下几页，`0`=不限；填 `1` 可大幅省空间 |
| `max_tags_per_work` | 每个作品最多归入几个标签目录（默认 8） |
| `tag_stopwords` | 这些标签不用来建目录（默认剔除 `オリジナル`、`AIイラスト`、`R-18` 等噪声标签） |
| `concurrency` / `requests_per_second` | 并发数与限速，建议保持默认，调高容易被封 IP |

命令行参数会覆盖配置文件，例如 `--proxy http://127.0.0.1:7890`、`--keep-r18`、`--out E:\图库`。

---

## 九、常用参数速查

```powershell
python pixiv_crawler.py auth login / status / doctor / logout
  login                登录 pixiv（交互式选择 OAuth / 自动读取 / 手动粘贴）
  status               查看登录状态、凭据类型与来源
  doctor               读取能力体检：实翻对比登录/匿名的可读量、原图下载权，并给修复建议
  logout               删除凭据库里的凭据
  --method token|auto-cookie|cookie   指定登录方式
  --code <授权码或回调地址>            非交互式完成 OAuth 登录
  --probe-pages N      doctor 的探测深度（默认 24 页；太小会两边同时触顶测不出差别）

python pixiv_crawler.py follow list / add / remove / sync
  add <ID或链接> [名字]    订阅画师（支持 https://www.pixiv.net/users/12345 或纯数字 ID）
  sync [--dry-run]         增量下载所有订阅画师的新作品；--limit N 限制每位画师本次数量
  --note 备注              给画师写备注，方便日后辨认

python pixiv_crawler.py estimate 关键词 [选项]        # 先算量级，再决定要不要爬
  --probe-requests N     最多发多少次探测请求（默认 40，越大越准也越慢）
  --work-mb N            每个作品平均体积 MB（默认按本机图库实测）
  --throughput N         单流下载速度 MB/s（默认 1.5，保守估计）
  --r18 LEVELS           内容分级（同 crawl 的 --r18）
  输出示例：
    预计作品数：约 不到四十万个（358,611）
    预计磁盘占用：一 TB
    预计总耗时：三天（搜索 一小时 + 下载 三天）
    需要分成约 112 个时间段来爬，共约 6,088 次请求
    ⚠ 磁盘空间不足：需要 一 TB，当前磁盘剩余 二百 GB

python pixiv_crawler.py crawl 关键词 [选项]
  --pages N              每种搜索组合翻多少页（每页约 60 个作品）
  --limit N              本次最多处理多少个作品（--full 模式下也生效）
  --r18 LEVELS           内容分级，自由组合（逗号分隔）：
                           0 / all-ages = 全年龄      1 / r18 = R-18      2 / r18g = R-18G
                         例：--r18 0,1 = 全年龄+R-18；--r18 2 = 只要 R-18G；--r18 all / none
                         三个分级任意组合共 6 种，与图形界面的三个勾选框完全对应
  --ai-mode MODE         AIGC：all=不限（默认）／exclude=排除 AI 生成／only=只要 AI 生成
  --artist ID或链接      只收这些画师的作品，可重复（纯数字 ID 或主页链接都认）
                         注意：这是在搜索结果里筛（pixiv 搜索不支持按画师过滤），
                         想要某画师的全部作品请用 follow 命令
  --full                 全量模式：按时间段递归切分，突破单一查询约 6180 条的翻页上限，
                         把该关键词的结果尽量全取下来。**很慢、占空间大**，
                         运行前会自动先做一次预估，磁盘不够会直接停止
  --full-from / --full-to   全量模式的时间范围（默认 2007-09-01 ~ 今天）
  --segment-target N     全量模式每段目标条数（默认 4800，越小分得越细）
  --deep                 深度模式：组合「最新 + 最早」两个不重叠区段。
                         实测匿名 598→1198，登录后 6300→10920（+73%），值得勾
                         运行时会自动报告每个组合的"新增率"，重叠多的组合会明确警告
  --deep-all             再加热门/男性向/女性向/标签完全一致。实测新增 0（热门排序登录后返回空），一般不必用
  --dry-run              干跑侦察：只统计各组合能拿到多少作品，不下载任何文件
  --order date|popular|old
  --s-mode tag|tag_full|text   tag=标签部分一致(默认) / tag_full=标签完全一致 / text=标题说明
  --max-pages-per-work N 每个作品最多下几页，0=全部（默认 0）
  --keep-r18             本次不跳过 R-18（但 R-18G 仍然跳过）
  --keep-r18g            连 R-18G（猎奇）也一起收
  --date-from 2024       只收此时间之后发布的作品（可写 2024 / 2024-07 / 2024-07-15 / "2024-07-15 18:30"）
  --date-to   2024-12-31 只收此时间之前发布的作品（含当天；写 2024 表示整个 2024 年）
  --min-likes 500        只收点赞数（いいね，爱心）≥ 500 的作品（0=不限）
  --min-bookmarks 100    只收收藏数（ブックマーク，书签）≥ 100 的作品（0=不限）
  --keep-ugoira          下载动图 ugoira（默认跳过）
  --ugoira-format FMT    none|webp|gif|mp4|webm 动图转码格式（需要 ffmpeg）
  --drop-ugoira-zip      动图转码成功后删除原始 zip（默认保留）
  --force                已下载过的也重新下载
  --restart              清空爬取进度记忆，所有组合从头重翻
  --concurrency N        并发下载数（默认 4）
  --proxy URL            临时指定代理
  -v                     显示详细配置信息

python pixiv_crawler.py reindex [--relink]
  --relink               补齐/刷新分类硬链接，并清理失效的旧链接（换过分类规则或 --force 重爬之后用）

python pixiv_crawler.py repair [--dry-run]
  按索引检查并补齐缺失的页/文件（不依赖搜索结果，任何作品都不会漏）
  什么时候需要：早期用 --max-pages-per-work 1 只下过一页、或磁盘文件被误删

python pixiv_crawler.py selftest [--offline]
  逐项自检环境/配置/网络/登录凭据/索引/分类目录，不下载图片；--offline 跳过联网测试
```

---

## 十、登录后能多爬多少（实测数据）

同一关键词「初音ミク」，我实测了各种情况下的可获取量：

| 情况 | 单次可获取作品数 | 说明 |
| --- | ---: | --- |
| 匿名 + 单一排序 | **598** | 第 11 页起全部重复，接口不再给新内容 |
| 匿名 + `--deep`（最新+最早两个区段） | **1198** | 两个区段互不重叠 |
| **已登录 + 单一排序（最新）** | **6300** | 翻 105 页仍未见底（探测脚本主动停在 105 页） |
| **已登录 + `--deep`** | **10920** | 最新 6300 ＋ 最早 4620，**两者交集 0** |

实测细节（2026-09，两种状态各跑一遍完整翻页）：

* 「最新」与「最早」是**完全不重叠**的两个区段 —— 匿名下交集 0，登录下交集也是 **0**。
  所以 `--deep` 带来的增益是实打实的：匿名 598 → 1198（+100%），登录 6300 → 10920（**+73%**）。
* **登录后 `--deep` 更值得勾**：能翻得更深，两个区段各自都很长且不重叠。
* `popular`（热门）排序**不可靠**：匿名下与"最新"完全重合（新增 0），
  登录下更是**直接返回空**。`--deep-all` 里的 `tag_full` / `popular_male` / `popular_female`
  实测新增也是 0，所以默认不勾选（代码里保留，但会提示"免得白发请求"）。

#### deep 会不会重复下载？—— 不会，而且程序会自己报告重叠率

去重分三层，逐层串起来：

| 层次 | 作用 |
| --- | --- |
| ① 单个组合内翻页 | 页内已出现过的作品 ID 直接跳过；整页 0 新增就判定"该组合已到接口尽头" |
| ② **组合之间（最新 vs 最早）** | 所有组合共用同一个"已见 ID 集合"，所以从头读和从尾读重合的部分只会算一次 |
| ③ 跨次运行 | 已在图库索引里的作品直接跳过，不重复下载 |

**程序会主动报告重叠情况**，不用你自己算。`--dry-run` 的输出里就有：

```
搜索组合                        接口返回    新增  新增率   状态
date_d + s_tag                       180     180    100%
最早发布（与最新不重叠的区段）       180     180    100%
热门（与最新重合时自动跳过）          60       0      0%   已到接口尽头　与已爬组合大量重叠
组合间去重：各组合共返回 420 条，去重后唯一 360 条（重复 60 条，重复率 14%）
→ 重复率高说明这些排序大量重合，加更多组合收益有限；重复率低说明组合确实互补
```

上例中去重后唯一 360 = 180 + 180，说明**「最新」与「最早」零重复**，
那 60 条重复全部来自 `popular` —— 这也正是它被标记为"大量重叠"的原因。
非干跑时若某个组合新增率低于 10%，也会当场打出 `⚠ 本组合新增率仅 X%` 的警告。

登录后 pixiv 报告的「初音ミク」结果数也从匿名的 62 万涨到 **74.5 万**（登录能看到更多内容）。

> 不确定自己现在能读多少？跑 `python pixiv_crawler.py auth doctor`，
> 或在图形界面点开「**读取能力**」标签页 —— 它会实际发请求测出登录 vs 匿名的可读量对比。

所以"没爬全"通常是这几层限制叠加：

1. **默认只翻 1 页**（约 60 个作品）——用 `--pages 60` 放开；
2. **未登录时接口每种排序只提供约 600 个**——登录后这个墙消失（实测 5997+ 无重复）；
3. **`max_works_per_run` 曾默认 500 会静默截断**——已改为默认 `0`（不限制），命令行 `--limit` 仍可显式设限；
4. **多图作品只下了第一张**——`max_pages_per_work` 默认就是 `0`（全部页）。若你曾用 `--max-pages-per-work 1` 跑过，
   跑一次 **`python pixiv_crawler.py repair`** 就会按索引把所有缺的页补齐（实测有效）。
5. **标签目录只取前 8 个标签**——`max_tags_per_work` 控制，想更全就调大。

`_state/crawl_state.json` 会记住哪些「关键词+组合」已经翻到接口尽头，下次运行自动跳过、不重复发请求；中断后重跑会接着爬，已下载的图自动跳过。想从头再来用 `--restart`。

---

## 十一、已知限制（重要，先看这里）

> 登录方式见 **第二节**；登录前后的可获取量对比见 **第八节**。

0. **接口改版会"响亮地失败"，不会静默丢失。** pixiv 随时可能改接口（网址或返回结构）。
   本程序对每个接口做**结构校验**：一旦发现返回结构不符合预期（找不到 `illustManga` /
   `userData` / `body` 等关键字段），会立即报错并列出**实际返回的键名**，提示"几乎一定是
   pixiv 接口改版了"，而不会假装"这关键词没结果"地爬完一整批。
   应急手段：接口地址变了可以不用改代码——在 `config.json` 的 `endpoints` 里覆盖
   （`{"search": "https://新地址/…/{kw}", ...}`，占位符 `{kw}`/`{iid}`/`{uid}`）。
   另外填了 `refresh_token` 时会直接走官方 App API（比网页接口稳定得多）。

1. **不登录也能用**，但免登录的搜索接口每种排序只提供约 600 个作品；要突破这个量、要看 R-18，请先登录（见第二节）。
2. **同一个关键词的可获取量有硬上限**（实测数据见上一节）：匿名单一排序约 600，加 `--deep` 约 1200，登录后更多但仍拿不到"全部 61 万条"。想覆盖更多，用多个近义关键词分别爬，它们都会汇总进同一个图库、自动去重。
3. **重复的作品会自动跳过**，不会重复占用空间。第一次用 `--max-pages-per-work 1`、之后想补齐多图时再跑一次同一关键词即可，程序会自动把缺的页补上。
4. **R-18 与 R-18G 是分开控制的**（默认都跳过）。pixiv 用 `xRestrict` 表示分级：`0`=全年龄、`1`=R-18、`2`=R-18G（猎奇/グロ），实测已确认 R-18G 一律是 `2`。

   | 你的目的 | 用法 | `skip_r18` / `skip_r18g` |
   | --- | --- | --- |
   | 全部不收（默认） | 不加参数 | `true` / `true` |
   | 收 R-18，**但不要猎奇** | `--keep-r18` | `false` / `true` |
   | R-18 和 R-18G 都要 | `--keep-r18g` | `false` / `false` |

  注意 **`--keep-r18` 不会纳入 R-18G**（早期版本会，已修正）。
   另外：搜索 `mode=all` 时 pixiv 服务端自己也会漏出 R-18/R-18G 结果（实测 120 条里有 28 条），
   所以这些过滤是程序在客户端逐条判断的，不依赖服务端筛选。匿名状态下拿不到 R-18 结果，需先登录。

   **图形界面（`gui.bat`）顶栏有三个对应控件**：`含 R-18` 复选框、紧跟其后的
   `含 R-18G（猎奇）` 独立复选框，以及 `含 R-18G` 右侧的浅色提示文字。
   不勾 `含 R-18` 时，R-18G 复选框会自动取消并置灰（R-18G 是 R-18 的子集，
   没有独立存在的意义），勾上 R-18 后才可单独决定要不要 R-18G。
   每次开始爬取前，日志区第一行会打印本次实际生效的分级，例如：

   ```
   [i] 内容分级：R-18 收，R-18G（猎奇）不收，动图 ugoira 不收
   ```
5. **换关键词重爬同一作品时**，分类目录里的链接序号会随新排名变化，旧链接可能留下；跑一次 `reindex --relink` 即可补齐并清理干净（原件始终只有一份，不会重复下载）。
6. **动图（ugoira）默认跳过**，它是 ZIP 帧序列，需要额外用 ffmpeg 合成，本程序只做收集不做合成。想收就把 `skip_ugoira` 改成 `false`（会存成 `.zip`）。
7. **请求不要太快**。程序默认限速 1.2 次/秒并自带指数退避重试；被限流（HTTP 429）时会自动等待。如果频繁报错，把 `requests_per_second` 调到 `0.5`，或换个时间段再跑。
8. 网络不通时（例如需要代理），在 `config.json` 填 `proxy`，或加 `--proxy`，必要时再加 `--insecure`。
9. `--force` 会删除并重新下载已存在的文件；另外注意它会让分类目录里的链接序号发生变化，必要时跑一次 `reindex --relink` 清理。
10. **人气门槛有个接口层面的代价**：搜索列表接口**不返回**点赞数与收藏数（实测字段为 `null`），
    只能逐个取作品详情才能判断，因此不达标作品的详情请求仍会发出（每作品一次）。
    想少花这些请求，就配合时间范围把范围收窄——**时间范围在列表阶段就能过滤**，
    还会在整页都早于起始时间时自动停止翻页（从新到旧排序时）。
11. **筛选发生在数量上限之前**：`--limit 200` 指的是"通过筛选的作品里取 200 个"，
    不会因为筛掉了作品而少下（早期版本会，已修正）。

---

## 十二、请留意

- 本程序仅按你的关键词抓取 pixiv 公开页面上可访问的图片，**图片版权属于各位画师**，请用于个人离线浏览与检索，不要二次发布或商用。
- 请勿把限速调得过高、不要长时间大并发抓取；尊重 pixiv 的使用条款与画师的意愿（画师在简介中注明禁止转载/保存的，请遵守）。
- 建议只收自己真正想看的：`--limit`、`max_pages_per_work` 都能帮你控制规模。

# AI 一键换发 · 复刻版

和发知发那个小程序同一套东西：**上传一张正面照 → 选发型 → 出「上脸」效果图 → 每日免费 2 次**。
区别是这套跑在你自己的服务器上，出图后端可换（云端大模型 / 本地 4060 的 ComfyUI）。

## 效果是怎么来的（三条命门，权重从高到低）

1. **构图（30%）**：必须只截头肩区。整张人像丢进去，头发像素占比太低，模型一定糊。见 `pipeline/preprocess.py`，以人脸框为基准左扩 1.15w、上扩 1.05h、下扩 1.35h 取正方形，缩到 768。
2. **mask（40%）**：只允许重绘头发。发际线一定要保留，否则额头和头发糊成一片，一眼假。见 `pipeline/mask.py`：BiSeNet 人脸解析取 hair 类（`models/face_parsing.onnx`），再外扩 8~12px 补发根；没模型时用肤色排除 + 头区约束的启发式兜底。
3. **局部重绘 + 身份锁定（30%）**：inpainting 只动 mask 内，IP-Adapter FaceID 锁人脸，避免"换个发型顺便换了个人"。见 `providers/comfy.py`。
4. **回贴调色（决定"像不像照片"）**：LAB 里把生成图的 L 通道对齐原图光照，a/b 只拉 35%（保留你新染的发色），羽化 2~6px 贴回，最后只锐化头区。见 `pipeline/compose.py`。

## 长发剪短（门店最常用的一档，单独说）

**问题**：SD1.5 的 inpainting 看到肩膀、衣领的上下文，会把 mask 里的长发继续画成长发——只是重画质感，长度一点没变。用 prompt 写 "pixie cut / 15cm"，8 张里只有 1~2 张真的变短，业务上没法用。

**现在的做法**（`pipeline/compose.cut_hair` + `composite_patch`）：

1. **几何剪**：算出目标发尾线 `y_line = 人脸框顶 + 框高 × LEN_END[长度]`，把线以下的头发用 Telea 填成背景/脖子/衣领——长度**由几何决定，模型说了不算**。
2. **抹平**：填充区沿水平方向反复 blur，把 Telea 留的暗斑和横纹抹匀（衣领上那点灰就是这么消掉的）。
3. **只修发尾**：交给模型重画的只有 `y_line ± 0.12~0.22×框高` 那一条（`tail_band`），denoise 0.7。给多了它一定把长度补回去（试过整块，全长回肩膀）。
4. **合成**：`composite_patch` 只在发尾那条里贴模型结果，其余全部用几何剪好的底图。

长度档位 `LEN_END = {10: 0.90, 12: 0.94, 15: 0.98, 20: 1.12, 30: 1.30}`（发尾 = 框顶 + 框高×倍数）。
`length_cm()` 从款式文案认长度：pixie/精灵/寸头→12，bob/短发/jaw→15，中长→20，长发/锁骨→30，认不出按 20。

判定：`y_line < 现有头发最低点 - 2%图高` = 要剪短，走上面这条；否则走原来的"加长/换卷"链路（`composite` + `hair_only_alpha`）。

实测（512 原图，长发抖到锁骨 → 齐下颌 Bob）：改动率 **18%（剪不动）→ 55~67%**，衣领/脖子干净，脸不动。验收图：`storage/debug/try_sheet.jpg`、`storage/debug/cut_v.jpg`。

**这档的边界**：极短（耳朵以上）时衣领处偶尔还会留一点灰；发尾近看比真发整齐；发丝质感是 SD1.5 的天花板。要更像真人发丝只能换云端 image-edit 大模型（`PROVIDER=gemini`）。

## 四条出图后端

| provider | 速度 | 成本 | 保真度 | 适合 |
|---|---|---|---|---|
| `mock` | 0.2s | 0 | —（占位渲染，画面上打 MOCK） | 只用来验证链路，别上线 |
| `gemini` | 8~15s | 按张计费 | 最好，几乎不用调 | 直接上线卖服务 |
| `openai` | 15~40s | 按张计费 | 好，支持 mask 精确局部 | 有中转额度时 |
| `comfy` | 6~10s（4060） | 0 | 中上，要调参 | 量大、要控成本、要私有化 |

切后端只改一个环境变量：`PROVIDER=gemini|openai|comfy|mock`。

## 跑起来

双击 `start.bat`（会自动把 `.env.example` 复制成 `.env`），然后浏览器打开：

```
http://127.0.0.1:8899
```

页面「④ 出图后端」里切换云端 / 本地，不可用的会灰掉并显示原因（缺 key / ComfyUI 没起）。

命令行等价写法：

```bash
python -m pip install -r requirements.txt
cd backend && python -m uvicorn app:app --host 0.0.0.0 --port 8899
```

`PROVIDER` 只决定默认选中项，实际每次请求都由前端带上 `provider` 字段覆盖；`app.py` 会自己读根目录 `.env`。

### 常见问题：上传失败 Failed to fetch

这不是上传逻辑的问题，是**页面和 API 不在同一个地址**，浏览器把网络层失败统一报成 `Failed to fetch`。三种触发场景：

| 场景 | 原因 | 解决 |
|---|---|---|
| 双击 `frontend/index.html` 打开 | `file://` 协议，Origin 是 null，fetch 直接被拦 | 用 `start.bat` 起服务后访问 `http://127.0.0.1:8899` |
| 在 IDE 预览面板里打开（端口类似 31555） | 预览服务器上没有 `/api/*` | 已修复：前端会自动嗅探 `''`→`8899`→`8897`，找到哪个用哪个 |
| 后端没启动 / 端口被占 | 连接被拒 | 看 `start.bat` 窗口有没有报错；`netstat -ano | findstr :8899` 查占用 |

现在页面顶部会显示「后端 http://127.0.0.1:8899」或「未连上后端」，一眼能区分是页面问题还是服务问题。

接口（和小程序那套同构）：

```
POST /api/upload      multipart file            → {photo_id, preview, face_found, rect}
GET  /api/styles      → {total, items:[{id,name,tags,prompt_zh}]}
POST /api/generate    {photo_id, style_id}      → {job_id, quota} | 402 额度用完
GET  /api/job/{id}    → {status:0|1|2|3, url, err}
GET  /api/quota       → {day, free, used, bonus, remain}
POST /api/unlock      {channel:"share"}         → 加 1 次（分享/客服解锁）
GET  /api/history     → 最近 20 条
```
`openid` 走 header（小程序里换成 code2session 得到的 openid）。

## 本地 ComfyUI（RTX 4060 8G）配法

模型丢进 ComfyUI 对应目录：

```
checkpoints/  sd-v1-5-inpainting.ckpt                       (~4G)
clip_vision/  CLIP-ViT-H-14-laion2B-s32B-b79K.safetensors   (~2.5G)
ipadapter/    ip-adapter-faceid-plusv2_sd15.bin             (~100M)
insightface/  models/antelopev2/*                           (~350M)
```
插件：`ComfyUI_IPAdapter_plus` + `insightface` + `onnxruntime-gpu`。
启动 `python main.py --listen 127.0.0.1 --port 8188`，然后 `PROVIDER=comfy`。

参数默认值（`providers/comfy.py::build_workflow`）：

| 参数 | 值 | 说明 |
|---|---|---|
| denoise | 0.92 | <0.8 换不出新发型，>0.97 开始破坏周围皮肤 |
| cfg | 4.5 | FaceID 下 7 会僵，4~5 最自然 |
| steps / sampler | 26 / dpmpp_2m + karras | 8G 上 768×768 约 6~10s |
| IPAdapter weight | 0.8 | 脸变了就调到 0.85；脸僵、不像新发型就 0.7 |
| grow_mask_by | 8 | 发根过渡 |

## 效果翻车对照表

| 现象 | 原因 | 改哪里 |
|---|---|---|
| 像戴了假发套 | mask 只盖了头顶，两侧真发没盖住 | `dilate()` px 加到 14~18；mask 用 BiSeNet 别用启发式 |
| 脸变了 / 变年轻 | 没有身份约束或 denoise 太高 | 加 IPAdapter FaceID 0.8；denoise 降到 0.88 |
| 边缘一圈硬线 | mask 没羽化 / 回贴没做 LAB 匹配 | `compose.composite()` 的 blur 与 `match_color` 别跳过 |
| 发量不够、贴头皮 | 裁剪框太小 | `head_crop_rect` 把 bottom 扩到 1.5h |
| 发色偏、发灰 | a/b 通道被过度对齐 | `match_color(ab_weight=0.15)` |
| 额头有头发影子 | mask 吃进了额头 | 肤色排除阈值收紧，或改用解析模型 |

## 上小程序

```js
wx.chooseMedia({count:1, mediaType:['image'], success: async r => {
  const up = await wxUpload('/api/upload', r.tempFiles[0].tempFilePath);   // wx.uploadFile，header 带 openid
  const job = await wxPost('/api/generate', {photo_id: up.photo_id, style_id: styleId});
  poll(job.job_id);   // 2s 一次 /api/job/{id}，status=2 出图
}});
```
额度用完接口返回 **402**，前端弹「分享给好友 +1 次」→ `POST /api/unlock`。

合规三条（真上线必须做，否则过不了审核/被举报）：
1. 生成图带 **AI 生成** 标识（微信小程序 AI 类目要求），后端加角标或元数据；
2. 隐私协议明确「照片仅用于本次生成，24 小时后删除」，并真的做一个清理定时任务；
3. 拒绝未成年人照片 + 明星/公众人物人脸，出图前做人脸比对拦截。

## 已验证 / 未验证

- 已验证：风格库 100 款加载；上传→裁剪→mask→生成→回贴→出图整链（mock）；额度每日 2 次、第 3 次返回 402、unlock 后 +1；前端页面 200。
- 未执行：`gemini` / `openai`（缺 key）、`comfy`（本机未装 ComfyUI 与 torch，需按上面清单装）。

---

# 发型库：抓取、入库、上新

## 1. 公开侧实际抓到了什么（已跑，可复现）

```bash
python tools/crawl_site.py --max-pages 40      # sitemap → 40 页 → 99 张图落盘
python tools/import_styles.py --catalog        # 质量门槛筛完只剩 9 张可用
```

```
[+] sitemap 115 个 URL
[=] 页面 40 张，图片落盘 99 张，去重后唯一 URL 111
[=] 词频 top10 -> 假发40 洗护10 补发8 头顶8 发片7 护理7 头皮7 打理6 染6 男士5
[catalog] 99 张 → 通过 9 张
[=] 拒绝 90 张：多人合影 71 / 没检出人脸 16 / 人脸太小 2 / 对比度太低 1
```

**结论（重要）**：他们官网只有门店、团队、会议、产品包装照，**没有一张款式图**。100+ 发型库在「AI 换发」小程序的后端里——`robots.txt` 里 `/api/` 是 Disallow，且接口域名独立（不在 bbkjfwg.com 的证书 SAN 里）。所以 `crawl_site.py` 抓到的是**运营素材**，不是款式库；款式库要么走抓包那条线（见 `recon/`），要么用你自己的产品图建。

目前 `data/styles.json` 里那 9 条是"过审的门店人像"，`tags` 统一是 `假发`，只用于证明链路。要清掉：`rm data/styles.json` 或调 `DELETE /api/admin/styles/{id}`。

## 2. 四条入库通道

| 通道 | 命令 | 适用 |
|---|---|---|
| 抓取自动入库 | `python tools/import_styles.py --catalog` | 已有整站素材，自动过门槛 + 自动打标 |
| 文件夹批量 | `python tools/import_styles.py --dir D:/款式图 --tags 短发,卷发 --color 深棕` | 一个文件 = 一款，文件名即款名 |
| CSV 清单 | `python tools/import_styles.py --csv data/styles_template.csv` | 有款名/标签/文案，批量上 50 款 |
| 后台单条 | `POST /api/admin/styles` | 运营在小程序后台点"新增款式" |

后台单条（实测已通）：

```bash
curl -X POST http://127.0.0.1:8899/api/admin/styles \
  -H "X-Admin-Token: dev-token" -H "Content-Type: application/json" \
  -d '{"name":"法式碎短发·深棕","tags":["短发"],"color":"深棕",
       "prompt_zh":"干净利落的法式碎短发，长度到下颌，深棕发色，发尾自然外翻",
       "cover_b64":"<图片base64>"}'
# → {"ok":true,"style":{"id":"ab2a11347ed",...},"total":110}
```

其余：`GET /api/admin/styles` 看全部外部款、`DELETE /api/admin/styles/{id}` 下架、`GET /api/styles?tag=短发` 按标签筛。

## 3. 加一款新发型，实际动了三层

**第一层 · 数据**（`data/styles.json` 一条记录）
```json
{"id":"ab2a11347ed","name":"法式碎短发·深棕","tags":["短发"],"color":"深棕",
 "cover":"ab2a11347ed.jpg","prompt_zh":"...","prompt_en":"...","hot":true}
```
`cover` 存 `data/covers/`，对外是 `/covers/xxx.jpg`；`tags` 直接变成前端筛选条和推荐依据。

**第二层 · 提示词**（决定"出来长什么样"的那半）
- 只给 `name`：走模板 `{color}{name}，发丝质感真实` —— 能出，但款式含糊；
- 给 `prompt_zh`：写清 **长度 + 层次位置 + 卷度 + 刘海 + 发色 + 质感**，这是唯一影响出图的控制点（`prompts.py` 会在后面拼上"五官/光照/背景不变"的硬约束）；
- 英文 `prompt_en` 只有走本地 ComfyUI 时才用得上。

**第三层 · 模型**（前端不用改，换后端即可）
同一款想更准：把该款的产品图挂到 `IPAdapterFaceID` 的参考图槽（改 `providers/comfy.py` 的 `7.image`），或对该款训一个 LoRA 挂到 checkpoint 后；再或者直接换 `gemini`，用整段中文 prompt 描述。上新款**不需要重训模型**，除非要做"极其特定的卷型/发色"。

**上新五步**
1. 拍/选图：正面大头、光线均匀、纯色背景，512×512 起步；
2. 过门槛：`import_styles.py` 会自动卡（单人、脸占比 ≥12%、置信度 ≥0.6、对比度 ≥260、非白底文字图）；
3. 写 `prompt_zh`：长度/层次/卷度/刘海/发色/质感六要素；
4. 入库：CSV 批量或后台 API；
5. 抽检：拿 3 张不同脸型各跑一次，看发际线和两侧发量。

## 4. 他们的发型库大概率是这么加的（推断，抓包可验）

后台录款（名称 / 封面 / 标签 / 是否热门 / 排序）→ 生成时按 `styleId` 取「提示词或参考图」→ 前端 `/api/styles` 拉列表 → 运营配置"热门/本命推荐"。抓包拿到 `styles` 接口后，看响应里有没有 `prompt`/`cover`/`sort` 字段就能确认是"一句 prompt 一款"还是"一张参考图一款"——这决定了你要抄的是文案还是素材。

## 5. 一句版权提醒

抓到的门店/团队照版权属于对方，当内部测试集没问题，**上线展示请换成自家产品图**，否则一投诉一个准。

---

# 真实产品库：官网产品中心 142 款已抓完入库（2026-10-20 实测）

## 突破：产品数据不在页面上，在 API 里

`cp.php` 页面写的是"正在加载产品信息..."，但分类表**内联**在 HTML 里（`const categories = {...}`），
产品列表走 AJAX。从 `assets/js/cp.js` 里挖出了两个接口：

```
GET api/products/get_products.php?category_id=N      # 主接口
GET api/products/fallback_products.php?category_id=N # 兜底
```

**11 个分类 / 142 款产品 / 142 张 640×960 产品图，声明数与实收数完全一致：**

| 分类 | 数量 | 分类 | 数量 |
|---|---|---|---|
| 女士直发 15cm | 38 | 女士卷发 15cm | 19 |
| 女士纹理 15cm | 23 | 女士发片 15-30cm | 14 |
| 男士发片 15cm | 14 | 女士长发 30,35cm | 8 |
| 女士卷发 20cm | 6 | 男士全头套 15cm | 5 |
| 女士直发 20cm | 5 | 女士锡纸烫 15cm | 5 |
| 混发专区 15cm | 5 | | |

产品字段：`name / product_code / style_code / colors(自然色·深棕·浅棕·酒红) / category_name / thumbnail_image / image_count / description`。

## 三步流水线

```bash
python tools/crawl_products.py            # 1) 抓分类+产品+封面图（断点续抓，已下载跳过）
python tools/analyze_products.py          # 2) 逐张量卷度/发色/发量 -> data/products_metrics.json
python tools/products_to_styles.py --reset # 3) 转成款式条目入库（142 款全部带真实封面）
```

跑完 `/api/styles` 总量 **242**（内置 100 + 产品 142），每款封面 `/covers/p*.jpg`。

## 为什么不直接拿品类名当 prompt

`女士直发（15cm）` 有 38 款，品类名只能生成 38 条一模一样的 prompt。所以第 2 步把
每张图量出三个量，让每款有自己的画像：

- **卷度**：头发区域结构张量方向一致性（直发发丝朝向一致，卷发杂乱）。
  用品类真值校准：直发 coh 0.36 [0.28,0.43] > 纹理 0.21 > 卷发 0.13 [0.10,0.19] > 锡纸烫 0.07，
  阈值 0.26/0.19/0.10，直发 vs 卷发二分类 **86%**。
- **发尾落点**：头发最低行相对脸框底端的偏移，量化为 耳上/及颌/及肩/过肩。
- **发量**：头发面积/人脸面积，四档（贴头/适中/蓬松/极蓬松）。

品类标注是权威（直发类最多容许"微卷"，卷发类不许判成"直发"），CV 指标只做细分，
两边冲突时夹回品类容许范围。结果：142 款 → 50 种不同 prompt，同组重复的
（16 款齐耳自然黑直发）在照片上本来就长得一样，这是目录的真实粒度。

每款还带了 `colors`（自然色·深棕·浅棕·酒红 4 色变体）和 `curl_coh`（原始指标，
调阈值时不用重算图）。

## 品牌动态里还有一套「荷风十二轻风系列」（12 款，无图）

系列文案在 `newsdetails.php?id=125`：晴丝/松絮/蝉尾/芒刺/榴卷/疏影/凝丝/圆云/敛云/平荷/分菱/垂檐，
每款有完整卖点文案（已存 `data/series.json`）。它们没有对应产品图，适合后续
按文案生成参考图后再入库——这也是他们自己"AI 生成效果图"那套打法的入口。

## 版权提醒（同上，升级版）

这次抓的是**产品级**数据（图 + 编码 + 色号），拿来自用测试、跑通链路没问题；
对外上线前把图换成自家产品图，编码体系（S01 发片/S02 直发/S03 卷发…）可以参考，别照抄整套。

---

# "很精准"是怎么来的：多候选择优层（已实现，无需 key）

## 他们小程序的换发链路（证据 + 推断）

官网 `ai.php` 的文案「精准模拟发丝质感、光线折射与头脸贴合度」和市面上假发试戴
服务商（犀跃创意等）的方案页**逐字一致**——这是采购成熟方案，不是自研。这类方案的
两条主流路线：

| 路线 | 速度 | 精准来源 |
|---|---|---|
| 2D 贴图合成（魔发科技类） | <1s，可"一秒试戴全店款式" | 人脸关键点 + 手动缩放平移 |
| 生成式大模型出图（即梦/万相/玩美 API） | 5~40s，有 taskId | mask + prompt + 参考图 |

发知发额度按天给 2 次、15~40s 出图、宣传"发丝质感/光线折射"——更像**生成式路线**。

但真正决定"看起来精准"的不是模型，是**择优**：一次生成 4 张，用客观指标打分，
只递最好的一张给用户。这一层已经在你这套里实现了。

## 实现：`backend/pipeline/select.py` + 多候选生成

三个指标（都能客观测出）：

- **keep 脸部保护度**（权重 0.44）：除头发 mask 外，人脸框内像素的平均改动量。
  换发只该动头发——这条最能拦住"换头感"，也最能拦住整图色偏/崩图。
- **sim 身份相似度**（0.40）：SFace（opencv_zoo，37MB 已下到 models/）对齐裁剪后
  的人脸 128 维特征余弦相似度，拦"生成出另一个人"。
- **hair 发际线连续性**（0.16）：mask 上边界逐列二阶差分，锯齿大 = 发际线糊了。

`total = 0.44*keep + 0.40*sim + 0.40*sim...`，keep<0.55 或 sim<0.34 直接判废。
计分阈值与权重的校准实测：

```
同人图      keep=1.00  sim=1.00  ✅
不同人      keep=0.00  sim=0.61  ❌ 脸部被改动
整图加噪    keep=0.54  sim=0.89  ❌ 脸部被改动
```

## 接口与前端

```bash
curl -X POST /api/generate -d '{"photo_id":"...","style_id":"ps0301","provider":"mock","n":4}'
GET /api/job/{id}
# → {"status":2,"url":"/files/results/xxx.jpg",
#    "candidates":{"n":4,"selected":0,"candidates":[
#      {"url":"/files/candidates/xxx_0.jpg","selected":true,
#       "score":{"keep":0.881,"sim":0.831,"hair":0.821,"total":0.854,"ok":true}}, ...]}}
```

前端加了「⑤ 一次出几张候选」1/2/4 档；完成后展示候选条（缩略图 + keep/sim/hair
分数），点任意一张可切换对比图。N 张候选全部落盘 `storage/candidates/`，方便复盘。

实测（mock provider，n=4，4 个变体里第 4 个故意带整图色偏模拟翻车）：

```
#0 ★ keep=0.881 sim=0.831  ← 选中
#1   keep=0.879 sim=0.829
#2   keep=0.878 sim=0.822
#3   keep=0.726 sim=0.831  ← 脸被染色的翻车样本，分数最低，被择优排除
```

`n>1` 只对 `supports_n=True` 的 provider 生效（mock 已实现变体；comfy 天然支持
换 seed 出多张；gemini/openai 单张接口，走默认退化）。mock 变体的差异在
发色 ±8% 与纹理强度，不影响链路验证逻辑。

## 还差什么到他们的"精准"

1. **真出图模型**：mock 只染色不换发型。gemini（一张图一句 prompt）或 comfy
   （SDXL inpaint + IP-Adapter FaceID）——这是"精准"的主体，缺它择优没有意义。
2. **3D 姿态对齐**（可选）：侧脸/仰头照片，先 3DDFA_V2 估姿态再变形 mask，能救
   10% 左右的难例。
3. **发际线 alpha matting**（可选）：把 hair mask 边缘细化到发丝级，回贴边缘无硬线。

---

# 本地出图（ComfyUI，零边际成本）

云端 Gemini 一条路已经通了（填 `GEMINI_API_KEY` 即可），本地这条是**不花钱、离线、随便调**的
那条：SD1.5 inpainting + IP-Adapter Plus，8G 显存的 4060 刚好吃得下。

所有下载项全部落 **D 盘**：`D:/AI/ComfyUI`（源码 + venv + 模型），项目目录不涨体积。

## 一、装（跑一次，4.7G 流量，断点续传）

```bat
setup_comfy.bat
:: 等价于 python tools\setup_comfy.py
```

脚本做四件事，每步都能单独重跑：

| 步骤 | 内容 | 位置 |
|---|---|---|
| 1 | `git clone --depth 1` ComfyUI | `D:/AI/ComfyUI` |
| 2 | 建独立 venv（Python 3.12，别用项目的 3.13，省一堆轮子问题） | `D:/AI/ComfyUI/.venv` |
| 3 | torch cu126 + `requirements.txt` + `ComfyUI_IPAdapter_plus` | 同上 |
| 4 | 模型从 **hf-mirror**（HF 直连被墙，镜像 206 正常）断点续传 | `D:/AI/ComfyUI/models` |

模型清单：

```
models/checkpoints/Realistic_Vision_V5.1-inpainting.safetensors   4.1G  写实 inpainting 主模型（fp32）
models/clip_vision/CLIP-ViT-H-14-laion2B-s32B-b79K.safetensors    2.4G  IP-Adapter 的图像编码器
models/ipadapter/ip-adapter-plus_sd15.safetensors                  94M  把参考发型"样子"带进模型

合计约 6.6G / 下载约 50 分钟（1MB/s 宽带）。装完 `D://AI//ComfyUI` 占 12G。
```

只补模型（比如断在第二个文件）：`python tools\setup_comfy.py --models-only`。
想上 FaceID（锁人脸，要 insightface 能编译）：`python tools\setup_comfy.py --with-faceid`。

## 二、起

```bat
comfy_up.bat          :: 127.0.0.1:8188，关窗口即停
python tools\check_comfy.py            :: 自检：连通/节点/模型/真出一张
python tools\check_comfy.py 照片.jpg 4  :: 出 4 张候选，带择优分数
```

自检全绿后，页面「④ 出图后端」点 **本地 ComfyUI**，`⑤ 一次出几张候选` 选 4。

## 三、链路（和云端同一条，只是 provider 换了）

```
上传 → YuNet 人脸 + 头肩裁剪 → 头发 mask（肤色排除 + 膨胀 12px）
     → ComfyUI 局部重绘（只在 mask 内生成，denoise 0.95）
       ├ 负向：blurry hair / plastic hair / wig / cartoon ...
       └ IP-Adapter Plus：参考图 = 发型库封面（有封面才带，没有就纯 prompt）
     → RepeatLatentBatch(amount=N) 一次采样出 N 张
     → LAB 调色回贴（L 全对齐、a/b 拉 35%、羽化 8px）
     → select 择优：keep 0.44 / sim 0.40 / hair 0.16
```

- 图**只重绘 mask 内**，脸根本没被碰过，所以身份不会变——这是比云端 image-edit 更稳的地方。
- 参考图来自 `/covers/ps*.jpg`（抓来的 142 款产品封面）。带参考图时 IP-Adapter 权重
  `COMFY_IPA=0.72`，`weight_type=ease out`、`end_at=0.85`，让它在早期步定调、后期让模型自由发挥。
- 节点入参按 `/object_info` 实时裁剪，ComfyUI 版本升级换了参数名也不会 400。

## 四、可调参数（环境变量，都有默认值）

```bat
set COMFY_HOST=http://127.0.0.1:8188
set COMFY_CKPT=Realistic_Vision_V5.1-inpainting.safetensors
set COMFY_IPA=0.72        :: 0 = 关掉 IP-Adapter，纯 prompt 重绘
set COMFY_STEPS=32
set COMFY_CFG=4.5
set COMFY_DENOISE=0.86    :: 越小越像原发型，越大换得越彻底
set COMFY_TIMEOUT=300
```

调参顺序：先 `COMFY_DENOISE`（发型改不动就加到 0.95，糊/花就降到 0.8），
再 `COMFY_IPA`（发型不像就加到 0.85，脸被带跑就降到 0.55），最后 `CFG`。

> 16:00-19:20 实测调参记录（4060 8G，512 输入）：
> - `cfg 6.0 + denoise 0.95`：发丝出现蓝紫/绿红彩虹边 + 假发感 → 弃
> - `cfg 4.5 + denoise 0.86 + steps 32`：干净的黑发，10.6s/张（n=2 共 21s）→ 现用默认
> - 负向词补 `blue hair sheen / purple hair / colored highlights / glossy plastic sheen`
> - 产品库文案里的 `wig / lace topper base` 是电商词，喂模型会真画假发 → `prompts._clean` 洗掉

## 五、两种后端怎么分工

| | 本地 comfy | 云端 gemini |
|---|---|---|
| 成本 | 0（电费） | 按张付费 |
| 速度 | 4~8s/张（4060） | 8~15s/张 |
| 效果 | 发型结构准、发丝感一般 | 整体最像"一键换发" |
| 适合 | 批量试库、调参、内测 | 对外收费、难例兜底 |

两条都留着：日常/批量走 comfy，用户投诉的难例（卷度复杂、极限发色）切 gemini。

---

# 常见报错（都对得上就按表改）

## 1. `'st' 不是内部或外部命令` + `ERROR: Could not import module "app"`

一起出现时是**同一个原因**：`.bat` 存成了 LF 行尾 / UTF-8 编码。

cmd.exe 在 `chcp 65001` 之后按字节偏移读文件，UTF-8 中文 + LF 会让它把一行拆成两半，
于是 `cd /d "%~dp0backend"` 没生效（→ uvicorn 在错误目录启动 → `Could not import module "app"`），
残片又当命令执行（→ `'st' 不是内部或外部命令`）。

修法（已改好，改 bat 时别再犯）：

| 项 | 必须是 |
|---|---|
| 行尾 | **CRLF**（`\r\n`） |
| 编码 | **GBK / cp936**（不是 UTF-8） |
| 换行后 | 不要加 `chcp 65001` |

自己改乱了就用 python 重新生成：

```python
data = open('start.bat', encoding='utf-8').read().replace('\n', '\r\n').encode('cp936')
open('start.bat', 'wb').write(data)
```

现在的 `start.bat` 还会先检查 `backend\app.py` 在不在，目录不对就直接告诉你，不再抛 ASGI 错。

## 2. `今日免费次数已用完`

额度是 `openid + 日期` 计数，默认**每天 30 次**（`FREE_PER_DAY`，`.env` 里可改）。
本地自用嫌少，三种解法：

```bat
:: ① 页面右上角「重置额度 ↺」——用完才出现，点一下今天所有人清零
:: ② 命令行全清
curl -H "X-Admin-Token: dev-token" -H "Content-Type: application/json" ^
     -d "{\"all\":true,\"reset\":true}" http://127.0.0.1:8899/api/admin/quota
:: ③ 只给某个人加次数（不重置已用量）
curl -H "X-Admin-Token: dev-token" -H "Content-Type: application/json" ^
     -d "{\"openid\":\"USER\",\"bonus\":100}" http://127.0.0.1:8899/api/admin/quota
```

查谁用得多：

```bat
curl -H "X-Admin-Token: dev-token" http://127.0.0.1:8899/api/admin/quota
```

## 3. 页面显示后端是 mock / 出图是占位图

`.env` 里 `PROVIDER=` 留空时，后端每次请求自动挑**当场能用**的后端：`comfy > gemini > openai > mock`。
想钉死某一条就填 `PROVIDER=comfy` / `gemini` / `mock`。

## 4. `.env` 里的行尾注释

`FREE_PER_DAY=30      # 本地自用` 这种写法现在是安全的（加载时会把 ` #` 之后切掉），
但**值本身别带 `#`**，`GEMINI_API_KEY` 之类不会被误切。

---

# 「选了发型但图没变化」——真正的原因和修法

这一版重写了 mask 和回贴，根因有三个，每个都能单独让结果看着像"没反应"：

## 1. mask 只覆盖**现有头发**（最致命）

原来 `hair_mask` 的头区下沿卡在 `fy + 1.05*fh`（下巴），且只取"现有头发像素"。
于是：短发的人选 30cm 长发，模型只能在原来那几厘米里换质感，长度根本长不出来 →
改动量只有 7%，肉眼就是没变。

现在：

- `mask.style_mask()` 把 mask 扩到**目标发型可能占的整块区域**（椭圆，按 15/20/30cm 定发尾位置）
- 长度从款式文案里认（`name_en` 的 `15cm/20cm/30cm`、`collarbone`、`ear-to-jaw`…）
- 五官从眉线到下巴整张脸挖掉，腰线以下截断
- 头区下沿放到 `fy + 2.10*fh`，长发不再被截断；再用连通域只留"跟头顶连着"的那块，避免把黑衬衫当头发

实测改动量：短发→30cm 从 **7% 提到 22%**。

## 2. 整块贴 vs 只贴头发，要分两条路

- **加长/改卷/改色**（目标比现有头发长）：只把"确实是头发"的像素贴回去
  （`hair_only_alpha` 按亮度判定），否则模型重画的背景会在头周围留一圈亮光晕
- **剪短**（目标比现有头发短）：必须整块替换（`full=True`），不然旧长发原地不动；
  同时 denoise 提到 0.95 让它重画轮廓

判定用 `hair_bottom()`（按行像素数算最低点），别用 `getbbox()`——几粒零散暗像素就能带偏。

## 3. 上传文件名撞车（测试时把我坑了两小时）

```python
# 原来：同一秒连传两张 → 同一个文件名 → 后一张覆盖前一张
path = f"{int(time.time())}_{u[:8]}.jpg"
# 现在：
path = f"{uuid.uuid4().hex[:12]}_{u[:8]}.jpg"
```

症状：A 的照片 id 指向了 B 的图，测 A 却出 B 的成品，看起来像"管线串了"。

## 已知边界（实话）

| 情况 | 现在能做到 |
|---|---|
| 短发 → 长发 / 中长 | ✅ 轮廓真的变长（22%） |
| 同长度换卷度、发量、发色 | ✅ |
| 长发 → 短发 | ⚠️ 会变短、变蓬，但很少能剪到耳下；整块贴时背景可能留一点灰圈 |
| 扎发 / 盘发（丸子头、高马尾、低盘发） | ✅ 会把长发收到下颌 + 头顶长出发髻（`_do_updo`：几何剪 + 清带 + 头顶加髻）；不是原版那种"贴着头皮全梳上去"，侧脸还有一点短发包边 |
| 发丝质感 | ⚠️ 近看仍有"假发感"，这是 SD1.5 inpainting + 电商假发参考图的限制 |

要真正"剪短到位"和"发丝级真实"，得上云端 image-edit（gemini），本地这条负责批量和调参。

### 扎发为什么单独一条路
"扎起来"不是改长度，是改头发走向。SD1.5 的 inpainting 只会在现有头发范围里重画，
prompt 写 `high bun` 也只会画出 bob —— 这就是以前点「气质扎发」没反应的原因。现在的做法：
1. `compose.cut_hair()` 几何剪到下颌（长度锁死）
2. `mask.updo_band()` → 模型补脖子/衣领（`prompts.UPDO_CLEAN_POS`，出 3 张挑最短的）
3. 高扎：`mask.bun_mask()` 只在头顶一小块里长髻，按 `_bun_score` 挑真出了髻的那张
4. 只把「带 ∪ 髻」贴回原图，脸和衣服其它部分不动

单张耗时约 3 次生成（≈40 秒）。想更快可把 clean/bun 的 n 降到 1~2，代价是稳定性下降。


---

## 云端出图：APIMart（换发型走这条，本地 SD1.5 只能微调）

一个 key 打通 gpt-image-1 / Nano Banana（gemini-2.5-flash-image）/ Seedream 等图像模型，
协议 OpenAI 兼容但**异步**（提交拿 task_id → 轮询 → 下载图）。

### 配置（`.env`）

```ini
PROVIDER=apimart
APIMART_API_KEY=sk-xxxxxxxx                     # apimart.ai 后台 → API Keys
APIMART_IMAGE_MODEL=gemini-2.5-flash-image-preview
APIMART_RESOLUTION=1K
# 国内直连不通，走本机梯子；不填会自动猜（10808 socks5 / 10809 / 7890 / 7897）
APIMART_PROXY=socks5://127.0.0.1:10808
```

### 原理（和本地这条的区别）

| | 本地 comfy | APIMart |
|---|---|---|
| 输入 | 只给 mask 内区域 | 整张人像 + 发型参考图 |
| 模型做的事 | 在原发丝范围里"描一遍" | 整头重画（长度/卷度/刘海都能换） |
| 回贴 | 只贴头发 alpha | `compose.composite_block()` 整块贴回 + 四边羽化 |
| 结果 | 原发型 + AI 味 | 真换发型，脸保持不变 |

代码位置：`backend/providers/apimart.py`、`app._run_job` 里 `EDIT_PROVIDERS` 分支、
`prompts.build_edit()`、`compose.composite_block()`。

### 接口

```bash
# 看能用的模型（image 那一组就是出图模型）
curl -s http://127.0.0.1:8899/api/apimart/models

# 运行中换模型，不用重启
curl -s -XPOST http://127.0.0.1:8899/api/apimart/model \
  -H 'Content-Type: application/json' -d '{"model":"gpt-image-1"}'

# 单款直跑（不经 HTTP，用于调 prompt / 比模型）
python tools/try_apimart.py storage/uploads/7bb80a11a1404885_crop.jpg ps0101 2
```

成本：nano banana 约 $0.0125~0.03/张，gpt-image-1 medium 约 $0.027/张，失败退款
（响应里的 `cost` 字段就是这一张实际扣的钱）。

---

## 后台页面：填 Key / 加发型（`http://127.0.0.1:8899/admin`）

首页右上角有「⚙ 填 Key / 加发型」按钮，点进去就是这个页面。**本机访问不用口令**，
从别的机器（手机 / 局域网）访问才需要 `X-Admin-Token`（默认 `dev-token`，改 `.env` 的 `ADMIN_TOKEN`）。

### ① 填 Key（不用手改 .env）

页面上粘贴 APIMart key → 选模型 → 点「保存并立即生效」。
后端 `POST /api/config` 会把 key 写进 `.env`（覆盖或取消 `#` 注释）并 `reload_providers()`，
**当场生效、不用重启**。页面上只回显掩码（`sk-te...cdef`）。

```bash
curl -s http://127.0.0.1:8899/api/config
curl -s -XPOST http://127.0.0.1:8899/api/config -H 'Content-Type: application/json' \
  -d '{"apimart_key":"sk-xxx","apimart_model":"gemini-2.5-flash-image-preview","provider":"apimart"}'
# provider 传 "" = 自动（谁可用用谁）
```

### ② 加发型

页面填：名称 / 类目（多选）/ 长度 / 发色 / **封面图** / 补充描述 → 保存。
后端 `POST /api/admin/styles` 收 `cover_b64`，裁成 512×512 存 `data/covers/<id>.jpg`，
写 `data/styles.json` 并 `styles.reload()`，首页立刻多一款（不用重启）。

**长度字段会直接决定几何**：`app._run_job` 里 `L = int(style_row["length"]) or length_cm(...)`，
10 超短 / 12 到下颌 / 15 下巴下方 / 20 锁骨 / 30 胸下。不填才退回从文案里猜。

### ③ 删发型 / 查额度

`DELETE /api/admin/styles/{sid}`、`GET|POST /api/admin/quota`，页面上都有对应按钮。

---

## 实拍图（主图 / 正面 / 侧面 / 背面）+ 产品信息

官网每款产品的详情页里其实有一组相册：`S0101.jpg`、`S0101_front.jpg`、`S0101_side.jpg`、
`S0101_back.jpg`。以前只下了封面，所以库里一款只有一张图。

```bash
python tools/crawl_gallery.py            # 全量，已下过的跳过
python tools/crawl_gallery.py --limit 5  # 先看 5 款
python tools/crawl_gallery.py --force    # 连页面缓存一起重来
```

- 抓取结果：`data/gallery/<款式id>/{main,front,side,back}.jpg`（142 款 / 533 张，约 26MB）
- 清单回写 `data/styles.json` 的 `images: [{label, file, src}]`，页面缓存留在 `data/gallery_html/`
- `styles.reload()` 只把**磁盘上真存在**的图放进 `images`（`{label, file, url}`），不会给前端 404
- 静态目录：`/gallery/<id>/<file>`（`app.mount("/gallery", ...)`）

产品信息字段（都来自 `products.json`）：`product_code` S0101、`style_code` FP01、`category`、
`colors`、`curl`、`volume`、`color`、长度（`name_en` 里的 `15cm`）。

前端：卡片上显示 `FP01 · 15cm · 直发 · 实拍4`；点卡片弹窗里是实拍图 + 完整信息条，
`←/→` 翻图，右下「用这款发型生成」直接跑。

## 后台：拉模型 / 改编已有款

- `POST /api/apimart/models {key?}` → 不保存也能拉模型清单（`{total, image:[...], all:[...]}`），
  后台点「拉取模型」把出图模型灌进下拉框
- `POST /api/admin/styles/{sid}` → 改名字/类目/长度/发色/提示词/换封面（内置 100 款不允许改）
- `POST /api/admin/styles/{sid}/images {file_b64,label}` / `DELETE .../images/{file}` → 加删实拍图
- `DELETE /api/admin/styles/{sid}` → 连 `data/gallery/<sid>/` 和 `data/covers/<sid>.jpg` 一起清掉

# 网站化：账号 / 积分 / 会员（2026-10-21 加）

## 一句话
- 品牌：**微粒 · WEILI**（前台已换牌，页脚有隐私说明）
- 注册送 3 次免费；用完按积分扣，1 元 = 10 积分，单次 `GEN_CREDITS`（默认 8）积分
- 普通用户：不能选模型（选了也按默认跑，贵模型直接 403）、最多出 2 张、只看自己的记录
- 管理员：能选模型 / 后端 / 4 张，不扣积分，后台看经营数据
- 失败自动退积分 / 免费次数（`_run_job` 的 except 里退）

## 计费口径（backend/billing.py，改这里就是改价格）
| 模型 | $/张 | ¥/张 | 积分 | 毛利 |
|---|---|---|---|---|
| gpt-image-2 @1K（当前默认） | 0.0085 | 0.061 | 8 | ¥0.73（91%） |
| gemini-2.5-flash-image（nano banana） | 0.0125 | 0.089 | 8 | ¥0.69（87%） |
| seedream-5-0-flash | 0.0137 | 0.098 | 8 | ¥0.69（86%） |
| dall-e-3 | 0.032 | 0.229 | 16（高级档 ×2） | |
| seedream-4-5 / flux-2-pro | 0.24~0.26 | ~1.7-1.9 | 40（旗舰 ×5，仅管理员） | |

真实消耗会按 apimart 回包里的 `cost` 写进 `jobs.cost_usd`，后台「⑤ 成本与毛利」看的是实账不是估算。

## 接口
- `POST /api/auth/register|login`、`GET /api/auth/me`（Bearer token，HMAC 签名，重启不掉登录）
- `GET /api/billing`（套餐+余额）、`POST /api/billing/order`、`POST /api/billing/order/{id}/pay`（现在是模拟支付，接微信/支付宝时替换这里）
- `GET /api/admin/users`、`POST /api/admin/users/{id}`（加积分/送次数/设管理员/停用）
- `GET /api/admin/overview`（用户/单量/充值/API 实付/单张毛利 + 每模型毛利表）

## 后台管理员
- 第一次启动自动建：`admin / weili8888`（.env 里 `ADMIN_USER/ADMIN_PASS` 改）
- `/admin` 页右上角登录后可见「④ 用户与积分」「⑤ 成本与毛利」

## 前台新增
- 登录/注册弹窗、积分 pill、充值弹窗（套餐四档）、06 会员与积分区
- 预览区（02）里也有一条同步进度条 + 转圈蒙层
- 结果可「分享」（手机调起系统分享，PC 弹微博/QQ空间/X/复制链接）和「存相册」（Web Share 文件分享，PC 走下载）
- 手机端：底部常驻「开始换发」条、单列布局、按钮加大、safe-area 适配

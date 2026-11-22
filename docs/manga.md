# 漫画翻译工作区

侧栏新增 **漫画翻译**。这是 AiNieePlus 内的独立本地工作区，不依赖 Java 或外部程序；原有“图片翻译”的整图生图模式仍可使用。

首页复用 AiNiee 的导入卡片和最近工程列表；编辑区采用相同的 Fluent 控件、主题色、卡片和明暗主题。

![漫画编辑器](images/manga-editor.png)

![深色主题](images/manga-editor-dark.png)

## Windows x86-64 安装包

在 Fork 的 GitHub Releases 中选择 `AiNieePlus-Windows-x64-Setup.exe`。这是 64 位版本，包含前后端、核心模型与字体；按当前用户安装，无需管理员权限。安装版使用标准用户数据目录，便携 ZIP 使用程序目录。发布页同时提供 SHA256 清单。

推送 main 会触发 Windows 构建、漫画测试、离线资源核对、安装包编译和安装后运行检查。手动运行工作流时可额外选择 macOS。每次成功构建创建独立预发布版本，保留先前发布记录。

## 使用与界面

- **文件 → 导入图片**：一个入口选择图片、PDF 或文件夹，也可拖入。文件夹会递归识别受支持文件；PDF 逐页转成编辑用图片，保留外部原件。文件菜单保留原文 JSON、图片与工程包导出，以及译文导入，不提供译文导出。
- **识别**：只有“识别当前页”和“OCR 当前页”。识别会询问是否同时 OCR；尚无框时进行 OCR，会询问是否先识别。
- **一键翻译**：主按钮翻译整部；若缺检测框或 OCR，先询问是否补齐再翻译。下拉另有“一键识别”和“一键 OCR”，也带相应询问。已有框和原文默认保留。
- **翻译**：翻译当前页，并直接打开 AiNiee 公共术语表、禁翻表、译后文本替换表。单框翻译和 OCR 在框的右键菜单中。
- **编辑**：撤销、重做，以及清除译文、OCR 原文、文字框。清除可选择选中框、当前页或整部，并可撤销；取消选框也在此菜单中。
- **排版**：批量应用样式、公共标点替换规则、显示 / 隐藏阅读顺序。阅读顺序默认隐藏，展开后位于右栏下方。应用到整部的样式也成为后续新建、识别框的默认样式。

画布上方的加框与删框图标相邻。右键可 OCR 此框、翻译此框、去字或删除。B 建框、V 移动，Delete 删除；Ctrl+Z 撤销，Ctrl+Y / Ctrl+Shift+Z 重做。左侧页面栏可以缩到只显示序号，悬停仍显示文件名。

## 实时编辑与成品预览

**实时编辑**使用独立文字图层：文字、样式、位置、框大小和撤销会直接更新；背景与解码图片复用缓存。编辑不会反复生成成品。选择 **预览成品** 或导出时才合成完整图片；修改内容会回到实时编辑。仅去字范围或蒙版改变时，精准去字会单独重建背景。

**自动字号**同时放大和缩小文字以适应框，避免大框中仍是很小的字；右栏显示实际字号。手动改字号会退出自动模式，再勾选即可恢复。换行、字形尺寸、描边与竖排在编辑器和导出中使用同一套计算。适应窗口、缩放滑条与百分比位于画布右下角。

识别过程中自动显示正在处理的页面。手动切页后暂停跳转，并出现“恢复跟随”提示；任务结束后提示隐藏。底部进度条只在任务进行时显示，任务明细入口固定在最底部日志右侧，包含具体失败页面 / 框、原因、定位与重试。

## 公共标点替换规则

“自动替换标点”启用时会创建或启用公共 **译后替换表 → 漫画标点替换**。重复启用不重复添加表格，也不会恢复用户删掉的规则，不改其他公共表格。通过“编辑标点替换规则”可新增、修改、删除，或手动恢复默认规则；也可在公共文本替换页面直接编辑同一张表。

默认含 `．．． → ...`、`… → ...`、`︙ → ...`、`⋮ → ...` 等规则。竖排中的点组与横线会按竖排方向显示。替换在排版中不改原始译文；启用的公共规则也会通过原版翻译链路处理模型结果。开关悬停显示说明。

整个工程统一勾选“精准去字”时使用 LaMa 修复；关闭立即回到色块。原图始终保留。框右键去字可以在还没有译文时处理原文。

## 更新与上游同步

更新检测和下载只使用 [Xitaoyou/AiNieePlus](https://github.com/Xitaoyou/AiNieePlus/releases)，包括当前 CI 预发布包。每个安装包写入自己的构建号，避免重复提示更新；保留上游版本号作为来源记录。

每日自动检查 `NEKOparapa/AiNiee` 的 main。有更新时普通合并，先运行后端与界面回归，成功后推送并构建 Windows 包；冲突或测试失败会停止并留下同步报告，不强制覆盖定制代码。配置见 `.github/workflows/sync-upstream.yml`。

## 核心与扩展模型

| 模型 | 用途 | 安装方式 |
| --- | --- | --- |
| manga-panel YOLO26 | 文字框与分镜；类别 1 为文字、0 为分镜 | 完整发布包内置，使用作者的同一 ONNX 导出 |
| mangaOCR ONNX | 日文单框识别，支持横竖排 | 完整发布包内置，同一 encoder / decoder / vocab |
| RapidOCR PP-OCRv6 | 通用中英文识别 | 完整发布包内置，固定 RapidOCR 3.10.0 的三组权重 |
| LaMa Manga ONNX | 复杂背景修复 | 完整发布包内置；采用公开漫画修复权重 |
| Koharu RFDETR 版面分割 | 作者提供的扩展模型 | 模型中心提供来源与用途；本版尚未适配其分割输出，不能用于检测 |

核心模型共约 390 MB（含字体；不含大语言模型）。模型中心提供来源、用途、目录导入、下载、断点续传与 SHA256 校验。下载结果必须与 `source-lock.json` 中的已验证版本一致；不会静默替换模型或回退到在线 OCR。当前推理使用 CPU；GPU 和其余扩展检测架构不在本版适配范围。

ImageTrans Legacy 气泡（Scaled YOLOv4）不随安装包附带。在「漫画模型中心」可从 AiNieePlus 一键下载，也可选择「导入目录」，选中本机 ImageTrans 根目录或 `models/legacy-balloon-yolov4`（需包含原版 `model.cfg`、`model.weights`、`model.json`）。然后在项目设置中选择该检测器。默认 512×512、置信度 0.20、NMS 0.50，不自动混入其他检测器的结果。它与 Japanese manga 旧版权重是不同模型，只检测气泡，不用于分镜。

下载或导入的可选模型可以在模型中心卸载。卸载只删除 AiNiee 自己保存的副本；不删除 ImageTrans 原文件或随包模型。如果当前工程使用该模型，先切换工程设置。

## 开发与打包

安装原项目的前后端依赖后，准备公开资源：

```powershell
python -m Tools.prepare_manga_models --download
# 也可以优先复用同一公开权重：
python -m Tools.prepare_manga_models --source 'D:\path\to\models' --download
python -m Tools.prepare_manga_models --check
```

正常通过 `python AiNiee.py` 启动。仅预览漫画页面时，在两个终端分别运行：

```powershell
python -m Backend --port 33899
python -m Tools.manga_preview --url http://127.0.0.1:33899
```

后端复用原有 image 队列、作业记录和取消接口；漫画 REST 路由位于 `/api/v2/manga`，沿用后端访问令牌。检测、OCR、修复和排版在独立进程执行，界面通过后台线程通信，不直接加载推理库。

核心权重与字体不提交到 Git，由资源准备步骤获取，打包时整体收入 `Backend/resources/manga`。Windows 和 macOS 工作流都已加入该步骤；发布前检查缺失文件、固定指纹、ONNX 输入与字体可加载性。Windows 安装包还会在干净目录静默安装，检查 EXE 架构和无系统 PATH 时的运行库加载。

```powershell
python -m pytest tests -q
python -m Tools.smoke_manga --output build/manga-smoke
```

真实模型冒烟验证包含 YOLO26、mangaOCR、RapidOCR、LaMa、独立进程和 PNG 导出；自动测试中的大语言模型回复是受控数据，不会调用付费接口。

## 资源来源

- 检测器与导出配置：[xulihang/balloon-dataset](https://github.com/xulihang/balloon-dataset)
- YOLO26 原始权重与 AGPL 声明：[leoxs22/manga-panel-detector-yolo26n](https://huggingface.co/leoxs22/manga-panel-detector-yolo26n)
- mangaOCR ONNX：[xulihang/mangaocr-onnx](https://github.com/xulihang/mangaocr-onnx)，原始模型：[kha-white/manga-ocr](https://github.com/kha-white/manga-ocr)
- RapidOCR：[RapidAI/RapidOCR](https://github.com/RapidAI/RapidOCR)
- LaMa 漫画模型与 Apache 声明：[mayocream/lama-manga-onnx](https://huggingface.co/mayocream/lama-manga-onnx)
- 字体：[Noto CJK](https://github.com/notofonts/noto-cjk)，OFL 文本随资源提供。

模型转换来源与原始权重来源分开记录；本项目不包含任何第三方程序代码。

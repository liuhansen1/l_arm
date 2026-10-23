# 机械臂与中文指令抓取

使用 PyBullet 的 Panda 七轴机械臂和双指夹爪，提供网页点选示教、动态物体库、自训四层 Transformer 动作理解和用户自定义组合任务。仿真中有红、绿、蓝方块，目标名称和执行坐标由用户采集、登记。

项目的设计目的、总体结构和技术亮点见 [项目设计说明](PROJECT_DESIGN.md)。

可从 [GitHub Releases](https://github.com/liuhansen1/l_arm/releases) 下载带版本号的源码与模型发布包；版本说明见 [RELEASE_NOTES.md](RELEASE_NOTES.md)。发布包已经包含训练权重和机械臂模型，运行依赖按下文安装。

项目原创代码采用 [MIT License](LICENSE)。随仓库提供的 Panda 模型资源沿用上游 [Apache License 2.0](robot/models/franka_panda/LICENSE.txt)；用户登记数据和本机环境不随仓库分发。

新版小模型已真实训练并接入网页，支持 GRASP、PLACE、MOVE、STOP、RUN_TASK、CLARIFY、UNSUPPORTED，不识别方向。新增目标、别名和基于已有技能的任务不需要训练。模型、数据、权重和实验记录见 [llm/README.md](llm/README.md)。
机械臂代码集中在 `robot/`，启动入口集中在 `launch/`。

## 开源内容

仓库包含从网页示教、语言理解到机械臂实际仿真的完整工程，安装依赖后即可启动。

| 路径 | 内容 |
| --- | --- |
| `frontend/` | 网页、点选示教、物体与放置点管理、任务编排和执行反馈 |
| `llm/` | 自训 Transformer、Tokenizer、训练数据、权重和实验记录 |
| `robot/models/franka_panda/` | Panda URDF、外观与碰撞网格、上游许可证 |
| `robot/` | 机械臂模型加载、场景、逆运动学、抓取、移动及放置控制 |
| `backend/simulation.py` | 仿真调度、相机与深度点选、动作执行及物理结果验证 |
| `backend/` | SQLite 登记与任务管理、名称匹配、意图解析和 HTTP 接口 |
| `launch/`、`tests/` | 统一启动入口和回归测试 |

`.gitignore` 仅排除本地虚拟环境、缓存、环境配置和 `backend/data/` 中的用户数据库及备份。机械臂模型文件、控制程序和语言模型资料均纳入版本管理。首次启动会建立空的登记库，不预填用户物体或任务。

## 基础配置

推荐按以下基线准备环境。软件与功能验证基于 **Ubuntu 24.04、x86_64、Python 3.12**；硬件建议便于运行网页与软件渲染，不代表经过测试的最低要求。

| 项目 | 配置或要求 |
| --- | --- |
| 操作系统 | Ubuntu 24.04 LTS，x86_64 |
| Python | 3.12，需安装 `venv` 和开发头文件 |
| CPU | 建议 4 个及以上逻辑核；模型默认使用 CPU，网页后端默认设置 4 个计算线程 |
| 内存与磁盘 | 建议 8 GB 内存，预留至少 4 GB 磁盘用于依赖、缓存和编译 |
| GPU / 实体机械臂 | 网页默认使用 PyBullet DIRECT 模式和 CPU 渲染，均不要求 |
| 浏览器 | 支持 Canvas 和 Fetch 流式读取的现代桌面浏览器 |
| 运行依赖 | NumPy `1.26.4`、PyBullet `3.2.7`、PyTorch `2.11.0+cpu` |
| 数据库 | Python 自带 SQLite，程序自动建库，无需单独安装数据库服务 |
| 网络 | 首次拉取和安装依赖需要联网；运行本地模型与仿真无需外部模型服务 |

前端为原生 HTML / CSS / JavaScript，运行时无需 Node.js、npm 安装或前端打包。Node.js 仅用于可选的前端测试。Windows、macOS 和树莓派目前不在已验证基线内。

## 构建方式

本项目的构建过程是创建 Python 环境、安装依赖并执行启动自检。训练好的权重、机械臂 URDF 和网格已包含在仓库内，首次使用无需训练、下载额外模型或手工配置资源路径。

### 1. 安装系统依赖

Ubuntu 24.04 上执行以下命令，安装 Git、Python 虚拟环境、开发头文件和 C/C++ 编译工具：

```bash
sudo apt update
sudo apt install -y git python3.12 python3.12-venv python3.12-dev build-essential
```

PyBullet `3.2.7` 在 Linux / Python 3.12 上首次安装可能需要源码编译，因此上述编译工具不可省略；编译通常比下载其他 Python 包耗时更长。其发布文件见 [PyPI](https://pypi.org/project/pybullet/3.2.7/#files)。

### 2. 拉取工程并创建虚拟环境

```bash
git clone https://github.com/liuhansen1/l_arm.git
cd l_arm
python3.12 -m venv .venv
```

以下命令均在仓库根目录执行，显式指定 `.venv/bin/python`，无需额外执行 `activate`。

使用 Release 压缩包时，解压后进入 `l_arm-v版本号/`，跳过 `git clone` 和 `cd l_arm` 两行，从创建 `.venv` 开始执行即可。

### 3. 安装固定版本的 CPU 依赖

```bash
.venv/bin/python -m pip install --upgrade pip setuptools wheel
.venv/bin/python -m pip install -r requirements-cpu.txt
```

[requirements-cpu.txt](requirements-cpu.txt) 同时安装基础依赖和固定版本的 CPU PyTorch，避免遗漏语言模型依赖。CPU 安装包来自 [PyTorch 官方索引](https://download.pytorch.org/whl/cpu/torch/)。[llm/requirements.txt](llm/requirements.txt) 保留原训练环境记录，首次运行使用前述 CPU 依赖文件即可。

### 4. 启动前自检

```bash
.venv/bin/python -B launch/check.py
```

自检会实际加载当前权重、仓库内的 Panda 模型、三色方块和仿真相机，并检查 SQLite 建库与初始空库。最后应输出：

```text
启动自检全部通过。可执行：.venv/bin/python -B launch/web.py
```

该检查使用临时数据库，退出时自动清理，不读取或覆盖用户已有登记，也不打开浏览器或监听网络端口。自检失败时返回非零退出码，应先处理终端报告的依赖或资源问题。

## 使用方式

### 1. 启动网页与仿真

```bash
.venv/bin/python -B launch/web.py
```

命令会先加载模型，再初始化机械臂和相机，最后启动网页服务并自动打开 `http://127.0.0.1:8080`。首次物体库、放置点库和任务库为空，这是正常状态。以后启动仍使用同一命令；按 `Ctrl+C` 停止服务和仿真。

浏览器没有自动打开时，手动访问终端打印的地址。没有桌面浏览器的环境可以使用 `--no-browser`；默认仍会运行真实物理仿真。

### 2. 完成第一个抓放任务

1. 等待页面显示后端已连接、画面中出现机械臂和三色方块。
2. 点选红色方块顶部中央，填写名称“零件A”，保存物体。名称、别名和坐标均由用户登记。
3. 点击“按坐标试抓”观察抓取结果；试抓结束后先点击“重置场景”，保存的登记会保留。
4. 切换到放置点登记模式，在可达的空白地面点选位置，命名为“装配区”并保存。放置点使用所点支撑面的坐标。
5. 在任务编排中建立“零件归位”，添加“抓取零件A → 放置零件A到装配区”，可选别名“收拾零件”，保存任务。
6. 点击任务的运行按钮，或输入“执行零件归位”，检查解析结果后确认执行。已有别名时也可以输入“我在上课，我想收拾零件”。

单独使用语言抓放时，可先说“抓取零件A”，成功后再说“把零件A放到装配区”。没有持物、目的地未登记或目标不明确时，后端会要求补充，不自动选择放置位置。

物体移动后，原抓取坐标不会自动更新。重新演示同一个任务前可先重置场景；操作新的实际位置时，应重新点选并更新物体坐标。

### 3. 常用启动选项

| 需求 | 命令 |
| --- | --- |
| 手动打开浏览器 | `.venv/bin/python -B launch/web.py --no-browser` |
| 更换端口 | `.venv/bin/python -B launch/web.py --port 8090`，访问 `http://127.0.0.1:8090` |
| 同时显示原生窗口 | `.venv/bin/python -B launch/web.py --gui`，需要桌面显示环境 |
| 使用独立登记库 | `.venv/bin/python -B launch/web.py --database backend/data/demo.sqlite3` |

默认数据库是 `backend/data/registry.sqlite3`，首次启动自动创建，重启后保留。重置场景不会清空登记；“清除所有记忆”会删除物体、放置点和任务，点击前页面会要求确认。

### 4. 常见启动问题

| 现象 | 处理方式 |
| --- | --- |
| 缺少 `torch`、`numpy` 或 `pybullet` | 用 `.venv/bin/python -m pip install -r requirements-cpu.txt` 安装；确认启动和安装使用同一解释器 |
| 创建虚拟环境时提示缺少 `ensurepip` | 安装 `python3.12-venv` 后重新创建 `.venv` |
| PyBullet 编译失败 | 检查 `build-essential`、`python3.12-dev` 是否已安装，保留完整编译日志排查 |
| 包下载失败 | 检查能否访问 PyPI 和 PyTorch 官方 CPU 索引，检查本机代理设置后重试 |
| 提示端口被占用 | 使用 `--port 8090`，浏览器访问相同端口 |
| 缺少权重、URDF 或网格 | 确认已完整拉取仓库，运行 `launch/check.py`；不要只复制 Python 源文件 |
| 页面提示后端未连接 | 使用 `launch/web.py` 启动，确认服务终端没有退出，浏览器地址与终端输出一致 |
| 抓取后无法继续执行 | 坐标试抓结束、失败或停止后先重置；普通任务成功抓取后可继续执行放置 |

已有 `.venv_train` 环境可以继续使用，将上述命令中的 `.venv/bin/python` 替换为 `.venv_train/bin/python` 即可。

## 网页工作台与坐标试抓

打开 `http://127.0.0.1:8080`，可点选任意场景位置，编辑坐标，再点击“按坐标试抓”。背景按地面 Z=0 投影，其他表面使用深度坐标；点位可以自行命名保存。坐标显示、保存和执行均保留三位小数。网页用持续相机流显示原生画面、实际 FPS、抓取进度、成功或失败和误差。成功要求实际夹起方块、抬升至少 15 cm 并保持双指接触 3 秒。每次测试后点击“重置场景”再进行下一次。

物体库初始为空：点选只采集坐标，名称和别名由用户填写，点击保存后才登记到 `backend/data/registry.sqlite3`。自行登记的内容在重启后保留。旧版 `backend/data/studio.sqlite3` 保留原样，不会被清空。网页仿真默认无独立窗口，可追加 `--gui` 同时显示原生 PyBullet 窗口，或追加 `--no-browser` 手动打开网页。Ctrl+C 同时停止服务与仿真。启动器会自动使用项目内已安装 PyTorch 的 `.venv_train` 环境。详见 [前端说明](frontend/README.md)。

登记目标后，可以输入“帮我抓取你填写的名称”，解析并执行。在“放置点”页面点击登记，切换到放置点模式，在画面点选地面或水平方块顶部并命名保存。点位保存的是支撑面坐标，精度为三位小数；支持地面和方块堆叠，放置后检查实际支撑与稳定状态。

任务编辑器支持 GRASP / MOVE / PLACE / WAIT，PLACE 必须选择物体和放置点，后端会自动移动到该点、下降、释放并验证实际支撑。可直接配置“抓取 → 放下”，无需手填移动坐标。已经抓起物体后，也可以说“把零件A放到放置区A”或“放到放置区A”。MOVE 仍使用夹爪参考点的世界绝对坐标。任务正常完成后可继续操作；失败或停止后按页面提示重置。物体被移动后需要重新采集登记坐标，不会自动用真实位置覆盖用户示教数据。

数据库新增 `placement_points` 表，启动时自动补齐，原有物体与任务保留。旧任务中的放下步骤需要编辑补选放置点后才能运行。添加放置点只更新数据库，不更改模型权重和词表。

## 文件结构

```text
robot_grasping/
├── robot/
│   ├── models/franka_panda/  # URDF、网格与模型资源许可证
│   ├── demo.py                # 场景与关节演示实现
│   ├── grasp.py               # 逆运动学、夹爪控制与物理抓取
│   └── skills.py              # 安全移动、定点放置与方块堆叠
├── backend/                  # 数据库、语言解析、HTTP 服务与仿真调度
├── frontend/                 # 网页、相机流、坐标编辑与任务编排
├── launch/
│   ├── web.py                 # 网页与后端统一入口
│   ├── check.py               # 安装依赖、模型、仿真与数据库自检
│   ├── package_release.py     # 源码发布包、资源清单和校验文件
│   ├── demo.py                # 启动场景演示
│   ├── grasp.py               # 启动指定颜色抓取
│   └── trained_language_grasp.py  # 自训小模型 + 用户登记库的命令行入口
├── llm/
│   ├── model.py               # 合并后的网络、文本编码和推理代码
│   ├── checkpoints/intent_v2.pt  # 当前七分类权重、固定词表和阈值，v1 保留
│   ├── data/                  # 训练、验证、测试数据
│   ├── experiments/intent_v2/ # 当前训练曲线、实验配置和真实评估，v1 保留
│   ├── train.py               # 真实训练入口
│   ├── evaluate.py            # 独立评估入口
│   ├── schema.py              # 固定动作意图协议与输入标准化
│   └── requirements.txt       # 自训模型的 PyTorch 依赖
├── tests/                     # 无需训练数据的运行回归检查
├── requirements.txt           # PyBullet、NumPy 依赖
├── requirements-cpu.txt       # 新用户默认使用的完整 CPU 依赖
├── .venv/                     # 安装时创建，Git 忽略
└── .venv_train/               # 可选的原训练环境，Git 忽略
```

## 机械臂演示

在项目根目录运行：

```bash
.venv/bin/python launch/demo.py
.venv/bin/python launch/grasp.py --color red
```

机械臂 URDF 和网格位于 [robot/models/franka_panda](robot/models/franka_panda/README.md)，场景程序从仓库内加载模型。逆运动学与碰撞仿真由 PyBullet 提供，`robot/grasp.py` 封装抓取与夹爪控制，`robot/skills.py` 实现移动和放置。也可使用 `python -m launch.demo` 或 `python -m launch.grasp` 启动。

## 自训小模型

小模型可以独立解析指令，在已安装依赖的环境中运行：

```bash
.venv/bin/python -B -m llm.model '帮我抓取<OBJ>'
```

可使用已经通过网页登记的目标名称执行；首次空库时需先在网页添加：

```bash
.venv/bin/python -B launch/trained_language_grasp.py \
  "帮我抓取零件A" --headless
```

网络约 184 万参数，默认使用 CPU 推理。词表、标签和网络配置包含在 `llm/checkpoints/intent_v2.pt` 中；v1 保留用于比较。后端替换数据库名称后，按目标和现有动作词提取唯一命令，只把有效片段交给真实模型，不审核闲话词语或枚举开场白。例如“我在打游戏，我想抓取零件A”只识别“抓取<OBJ>”。命令行入口支持 `--parse-only`、`--database`、`--headless`、`--device` 和 `--checkpoint`。它启动独立仿真，不与正在运行的网页共享物理状态。

各类意图的含义、自然语言示例及执行条件见技术文档中的[模型识别能力表](PROJECT_DESIGN.md#模型识别能力)。

## 接口说明

### HTTP 接口约定

前端通过 [frontend/api.js](frontend/api.js) 调用后端，默认 API 根地址为 `http://127.0.0.1:8080/api/v1`。可以在网页“接口设置”中指定兼容服务的地址。现有服务还通过 API 根路径之外的 `GET /studio-config.json` 返回 `{"mode":"backend","apiBaseUrl":"/api/v1"}`，该运行配置会覆盖浏览器保存的设置。

JSON 使用 UTF-8；成功响应直接返回对象或数组，错误使用非 2xx 状态码及 `{"message":"错误说明"}`。物体、放置点和任务 ID 为整数，`frame_id`、`parse_id`、运行作业 ID 为字符串。坐标使用 `world` 世界坐标系，单位为米，记录精度为三位小数。放置点表示支撑表面位置，不是夹爪高度。

| 方法 | 路径（相对 API 根地址） | 用途 |
| --- | --- | --- |
| GET | `/health` | 检查执行端是否就绪及可用能力 |
| GET | `/simulation/scene` | 当前画面元数据、`frame_id`、图像地址及状态版本 |
| GET | `/simulation/frame.png?frame_id=...` | 读取指定帧的 PNG 图像 |
| GET | `/simulation/stream` | 连续推送画面及同帧元数据 |
| POST | `/simulation/pick` | 将当前显示帧上的归一化点击坐标转换为三维位置 |
| POST | `/simulation/stop`、`/simulation/reset` | 停止执行、重置场景，请求体为 `{}` |
| GET / POST | `/objects` | 列出或新增物体 |
| PUT / DELETE | `/objects/{id}` | 修改或删除物体 |
| GET / POST | `/placement-points` | 列出或新增放置点 |
| PUT / DELETE | `/placement-points/{id}` | 修改或删除放置点 |
| GET / POST | `/tasks` | 列出或新增组合任务 |
| PUT / DELETE | `/tasks/{id}` | 修改或删除组合任务 |
| POST | `/instructions/parse` | 匹配登记名称并调用 Transformer 解析动作 |
| POST | `/jobs` | 提交坐标试抓、组合任务或已确认的语言指令 |
| GET | `/jobs/{id}` | 查询作业进度、结果或失败原因 |
| POST | `/memory/clear` | 重置并清除登记数据、解析凭据和运行记录，具有删除作用 |

### 从点选到执行

先读取 `/simulation/scene` 或相机流，取得当前显示画面的 `frame_id`。点选请求示例（`x/y` 范围为 `[0,1)`，原点在画面左上角）：

```http
POST /api/v1/simulation/pick
Content-Type: application/json
```

```json
{"frame_id":"frame-42","x":0.61,"y":0.69}
```

响应包含 `hit_position`（命中表面坐标）、`grasp_position`（抓取参考坐标）、`scene_object_id`、`surface_normal` 和 `coordinate_frame`。登记物体使用 `grasp_position`，登记放置点使用 `hit_position`。不应把数据库物体 ID 与场景标识或物理引擎临时 ID 混用。

通过 `POST /objects` 保存用户填写的名称、别名、坐标和技能，例如：

```json
{
  "name": "零件A",
  "aliases": ["红色积木"],
  "scene_object_id": "cube-red",
  "color": "red",
  "position": [0.550, -0.180, 0.025],
  "coordinate_frame": "world",
  "skill_ids": ["cube_grasp"]
}
```

新增接口返回包含整数 `id` 的完整记录。名称匹配由数据库完成，Transformer 接收替换后的 `<OBJ>` / `<TASK>`，只识别动作。当前技能标识为 `cube_grasp`、`cube_place`；扩展执行端时需要保持技能协议一致。

语言解析使用 `POST /instructions/parse`：

```json
{"instruction":"帮我抓取零件A","model":"transformer"}
```

可执行响应的关键字段如下，ID 与分数仅作结构示例：

```json
{
  "status": "ready",
  "parse_id": "parse-123",
  "action": "GRASP",
  "object_id": 1,
  "object_name": "零件A",
  "normalized_instruction": "帮我抓取<OBJ>",
  "model_input": "抓取<OBJ>",
  "confidence": 0.96
}
```

只有 `status="ready"` 才能执行。`unmatched`、`clarify`、`unsupported`、`low_confidence` 均不应触发动作。`parse_id` 有效期 120 秒，只能成功提交一次；数据库、场景或状态改变后需重新解析。

用户确认解析结果后，向 `POST /jobs` 提交凭据：

```json
{"source":"language","parse_id":"parse-123"}
```

服务端按保存的解析结果执行，不由客户端重新指定动作。普通动作异步返回 HTTP 202 和作业 `id`，随后查询 `/jobs/{id}`。作业状态为 `queued`、`running`、`succeeded`、`failed`、`cancelled`，包含步骤进度、结果或错误；停止指令完成后直接返回 HTTP 200。

“按坐标试抓”直接提交当前编辑坐标，无需先登记物体：

```json
{
  "source": "coordinate_test",
  "action": "GRASP",
  "frame_id": "frame-42",
  "scene_object_id": "cube-red",
  "grasp_position": [0.550, -0.180, 0.025],
  "coordinate_frame": "world"
}
```

执行端必须使用提交的坐标，真实物体位置只能用于结果检查，不能替换用户示教位置。组合任务则提交 `{"source":"task_builder","action":"RUN_TASK","task_id":1}`。PLACE 必须有已登记的放置点及当前持物；WAIT 属于任务步骤，不是模型输出类别。

相机流的 Content-Type 为 `application/x-panda-frames`，每帧依次包含：**4 字节大端 JSON 字节长度 → UTF-8 JSON 元数据 → PNG 字节**。元数据的 `image_size` 指定 PNG 长度，`frame_id` 必须与显示图像一致；现有解码器见 [frontend/frame_stream.js](frontend/frame_stream.js)。

完整字段、放置点和任务请求示例、错误处理及执行结果见 [前后端接口约定](frontend/API.md)。

### 执行服务的 Python 接口

执行服务已实现在 [backend/simulation.py](backend/simulation.py)，由 HTTP 层、指令解析和数据库坐标校验调用；底层机械臂控制位于 `robot/`。以下调用约定便于理解现有实现和接入其他执行后端。

| 接口 | 调用约定与职责 |
| --- | --- |
| `SimulationService(gui=False)` | 创建执行服务，支持服务端传入 `gui` 关键字参数 |
| `start()` / `close()` / `check_ready()` | 启动、关闭和就绪检查；不可用时抛出 `backend.RequestError` |
| `snapshot()` | 返回画面及状态元数据，至少满足前端协议和解析使用的 `scene_id`、`state_version`、`held_object_id` |
| `frame(frame_id)` | 返回包含 `png` 字节的帧字典；旧帧应报告过期 |
| `stream_packets()` | 迭代返回按上述相机协议编码的二进制数据包 |
| `pick(body)` | 接收 `frame_id/x/y` 等点选参数，返回同帧三维采集结果 |
| `validate_program(steps)` | 检查步骤、技能、坐标和当前持物状态；不启动动作 |
| `submit_program(steps, *, action="RUN_TASK", expected_state=None)` | 校验状态版本并提交异步程序，返回作业字典 |
| `submit_grasp(scene_object_id, position, frame_id=None)` | 提交坐标试抓；场景目标可为空，位置必须使用传入坐标 |
| `get_job(job_id)` | 返回作业状态、进度及实际结果 |
| `control(kind)` | 接受 `"stop"` / `"reset"`，完成后返回控制结果 |
| `validate_position(value, *, workspace=True)` | 模块级坐标校验，返回三位小数的 XYZ；`workspace=False` 用于采集保存 |
| `validate_placement_position(value)` | 模块级放置表面坐标校验，返回 XYZ |

程序步骤由 `prepare_steps()` 从数据库记录生成，包含展开的物体、放置点及坐标，不能直接把客户端原始 JSON 当作执行步骤。作业提交、停止或重置等状态变化应更新 `state_version`，重置时更新 `scene_id`，使旧解析凭据失效。运行结果应报告实际完成情况，失败即停止后续步骤。

当前清除记忆逻辑还会使用执行服务的 `lock`（可重入锁）、`active_job`、`control_pending` 和 `jobs` 字典；兼容模块需要提供这些成员。实现错误通过 `backend.RequestError(message, status)` 交给 HTTP 层返回。若选择自行实现独立 HTTP 执行后端，则按上述 HTTP 协议连接前端，无需采用这个 Python 类。

## 运行选项与检查

- 新网页使用固定场景和用户登记的名称，不按颜色或方向预测目标。
- 未知目标、多目标、否定请求、过期解析凭据和无效技能不会发起执行；模型仍可能误解新表达。
- 成功判据为目标抬升至少 15 cm，并保持双指接触 3 秒；抓取依靠物理接触与摩擦。

```bash
.venv/bin/python -B -m unittest discover -s tests -v
```

## 构建 Release

维护者更新 `RELEASE_NOTES.md`，在干净的 Git 工作副本中提交修改、创建版本标签后，可以构建同一标签对应的发布包：

```bash
git tag -a v1.0.0 -m "l_arm v1.0.0"
python3 launch/package_release.py --tag v1.0.0
```

输出位于 `dist/v1.0.0/`，包含 ZIP、tar.gz、逐文件清单和 `SHA256SUMS`。打包程序只读取标签中的已提交文件，并拒绝将数据库、本地虚拟环境或本机配置打包。

推送版本标签会触发仓库的 `Publish Release` 工作流，自动上传附件并发布 GitHub Release：

```bash
git push --follow-tags origin main
```

也可在 GitHub Actions 页面手动运行该工作流并填写已存在的版本标签。该流程使用 GitHub 自带的工作流令牌，无需额外配置个人令牌；仓库需要允许 GitHub Actions 运行。

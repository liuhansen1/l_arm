# 机械臂与中文指令抓取

使用 PyBullet 的 Panda 七轴机械臂和双指夹爪，提供网页点选示教、动态物体库、自训四层 Transformer 动作理解和用户自定义组合任务。仿真中有红、绿、蓝方块，目标名称和执行坐标由用户采集、登记。

项目的设计目的、总体结构和技术亮点见 [项目设计说明](PROJECT_DESIGN.md)。

公开文件采用 [MIT License](LICENSE)，机械臂私有实现和本地用户数据不随仓库分发。

新版小模型已真实训练并接入网页，支持 GRASP、PLACE、MOVE、STOP、RUN_TASK、CLARIFY、UNSUPPORTED，不识别方向。新增目标、别名和基于已有技能的任务不需要训练。模型、数据、权重和实验记录见 [llm/README.md](llm/README.md)。
机械臂代码集中在 `robot/`，启动入口集中在 `launch/`。

## 公开范围与运行条件

公开部分包含网页前端、SQLite 登记与任务管理、自训 Transformer 的网络、Tokenizer、训练数据、权重及实验记录，以及语言解析和 HTTP 接口层。

**机械臂模型、场景和控制实现保留在本地，通过根目录 `.gitignore` 排除，不随公开仓库分发。**

| 本地私有路径 | 内容 |
| --- | --- |
| `robot/` | 机械臂模型加载、场景、逆运动学、抓取、移动及放置控制 |
| `backend/simulation.py` | 仿真调度、相机与深度点选、动作执行及物理结果验证 |
| `launch/demo.py`、`launch/grasp.py` | 私有机械臂演示入口 |
| `frontend/tools/render_scene.py` | 依赖私有场景与控制器的预览资源生成工具 |

本地虚拟环境、机械臂描述和几何资源、`backend/data/` 中的用户数据库也已忽略。`llm/checkpoints/`、`llm/data/` 和 `llm/experiments/` 保留在公开范围内；这里的语言模型权重与机械臂模型资源不同。

公开仓库可以独立使用小模型推理、训练和评估，也可以浏览前端静态预览。**完整网页仿真、SQLite 登记服务及语言执行目前都依赖私有的 `backend/simulation.py`；仅克隆公开文件无法直接启动 `launch/web.py`。** 本地完整工程仍按下文启动，接入其他执行端所需的接口见“接口说明”。本文文件结构中的私有路径用于说明完整工程，公开检出时不会包含。

`.gitignore` 只阻止未跟踪文件被常规添加，不会移除已经跟踪的文件或历史提交中的内容。

## 网页工作台与坐标试抓

以下命令用于保留上述私有文件、且已安装依赖的本地完整工程。在项目根目录运行，同时启动真实 PyBullet 仿真和网页服务，并自动打开浏览器：

```bash
.venv/bin/python -B launch/web.py
```

打开 `http://127.0.0.1:8080`，可点选任意场景位置，编辑坐标，再点击“按坐标试抓”。背景按地面 Z=0 投影，其他表面使用深度坐标；点位可以自行命名保存。坐标显示、保存和执行均保留三位小数。网页用持续相机流显示原生画面、实际 FPS、抓取进度、成功或失败和误差。成功要求实际夹起方块、抬升至少 15 cm 并保持双指接触 3 秒。每次测试后点击“重置场景”再进行下一次。

物体库初始为空：点选只采集坐标，名称和别名由用户填写，点击保存后才登记到 `backend/data/registry.sqlite3`。自行登记的内容在重启后保留。旧版 `backend/data/studio.sqlite3` 保留原样，不会被清空。网页仿真默认无独立窗口，可追加 `--gui` 同时显示原生 PyBullet 窗口，或追加 `--no-browser` 手动打开网页。Ctrl+C 同时停止服务与仿真。启动器会自动使用项目内已安装 PyTorch 的 `.venv_train` 环境。详见 [前端说明](frontend/README.md)。

登记目标后，可以输入“帮我抓取你填写的名称”，解析并执行。在“放置点”页面点击登记，切换到放置点模式，在画面点选地面或水平方块顶部并命名保存。点位保存的是支撑面坐标，精度为三位小数；支持地面和方块堆叠，放置后检查实际支撑与稳定状态。

任务编辑器支持 GRASP / MOVE / PLACE / WAIT，PLACE 必须选择物体和放置点，后端会自动移动到该点、下降、释放并验证实际支撑。可直接配置“抓取 → 放下”，无需手填移动坐标。已经抓起物体后，也可以说“把零件A放到放置区A”或“放到放置区A”。MOVE 仍使用夹爪参考点的世界绝对坐标。任务正常完成后可继续操作；失败或停止后按页面提示重置。物体被移动后需要重新采集登记坐标，不会自动用真实位置覆盖用户示教数据。

数据库新增 `placement_points` 表，启动时自动补齐，原有物体与任务保留。旧任务中的放下步骤需要编辑补选放置点后才能运行。添加放置点只更新数据库，不更改模型权重和词表。

## 文件结构

```text
robot_grasping/
├── robot/
│   ├── demo.py                # 场景与关节演示实现
│   ├── grasp.py               # 逆运动学、夹爪控制与物理抓取
│   ├── spatial_scene.py       # 布局生成和场景快照
│   └── scene_reasoning.py     # 颜色、空间关系与唯一目标筛选
├── launch/
│   ├── web.py                 # 网页与后端统一入口，依赖私有执行模块
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
├── .venv/                     # 本地机械臂仿真环境
└── .venv_train/               # 已安装 PyTorch 的小模型推理环境
```

## 本地私有机械臂演示

以下入口不随公开仓库分发。在本地完整项目目录运行：

```bash
.venv/bin/python launch/demo.py
.venv/bin/python launch/grasp.py --color red
```

机械臂的逆运动学、碰撞仿真和 URDF 资源由环境中的 PyBullet 提供；`robot/grasp.py` 调用底层逆运动学库。也可使用 `python -m launch.demo` 或 `python -m launch.grasp` 启动。

## 自训小模型

小模型不依赖私有机械臂实现。在已安装 PyTorch 的 Python 环境中可独立解析指令；下面使用本地已有的环境路径，公开检出时请替换为自己的 Python：

```bash
.venv_train/bin/python -B -m llm.model '帮我抓取<OBJ>'
```

在本地完整工程中，可使用已经通过网页登记的目标名称执行；首次空库时需先在网页添加：

```bash
.venv_train/bin/python -B launch/trained_language_grasp.py \
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

### 私有执行模块的 Python 接口

若复用现有 [backend/server.py](backend/server.py)、[backend/instructions.py](backend/instructions.py) 和 [backend/store.py](backend/store.py)，需要自行提供 `backend/simulation.py`。该模块当前还被数据库坐标校验引用，因此即使只启动登记接口也需要它。这里说明现有调用约定，不提供机械臂控制实现。

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
.venv_train/bin/python -B -m unittest discover -s tests -v
```

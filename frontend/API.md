# 前端与后端接口约定

使用 `.venv/bin/python -B launch/web.py` 同时启动前端与真实 PyBullet 仿真，启动器自动使用项目中安装了 PyTorch 的环境。接口根地址默认 `http://127.0.0.1:8080/api/v1`。已实现实时相机、深度点选、坐标抓取、停止/重置、SQLite 物体与任务管理、自训 Transformer 语言解析和组合任务执行。

所有 JSON 使用 UTF-8，长度单位固定为米，坐标系为 PyBullet 世界坐标。当前后端数据库物体/任务 ID 为整数，临时抓取 job ID 为字符串。`GET /studio-config.json` 位于 API 根路径之外，为同源前端返回 `{"mode":"backend","apiBaseUrl":"/api/v1"}`，覆盖旧预览设置。

成功响应直接返回本文所示对象或数组，不另包 `data`。错误响应使用非 2xx 状态码及 `{"message":"可读错误说明"}`。DELETE 成功可返回 204。前端不会自动重试 POST 请求，也不会在接口出错后伪造成功。

开发时允许前端来源 `http://127.0.0.1:8080` 的 CORS 请求，并处理 `OPTIONS`、`GET`、`POST`、`PUT`、`DELETE`。若静态资源和 API 同源，不需要额外跨域配置。

## 接口列表

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/health` | 检查实际仿真服务，返回状态与 capabilities |
| GET | `/simulation/scene` | 相机帧地址与同帧物体信息 |
| GET | `/simulation/stream` | 连续推送同帧元数据与 PNG 的二进制流 |
| POST | `/simulation/pick` | 根据画面归一化坐标拾取物体和抓取位置 |
| POST | `/simulation/stop` | 停止机械臂与当前任务 |
| POST | `/simulation/reset` | 中断任务，恢复机械臂和方块初始状态 |
| POST | `/memory/clear` | 重置场景并清空物体、放置点、任务、解析凭据和运行记录 |
| GET | `/objects` | 物体数组 |
| POST | `/objects` | 新增登记物体，返回完整对象 |
| PUT | `/objects/{id}` | 更新名称、别名、位置和技能，返回完整对象 |
| DELETE | `/objects/{id}` | 删除登记，有任务引用时返回 409 |
| GET | `/placement-points` | 查询独立放置点数组 |
| POST | `/placement-points` | 创建放置点，返回完整记录 |
| PUT | `/placement-points/{id}` | 修改放置点名称、别名和表面坐标 |
| DELETE | `/placement-points/{id}` | 删除放置点，有任务引用时返回 409 |
| GET | `/tasks` | 任务数组 |
| POST | `/tasks` | 新增组合任务，返回完整对象 |
| PUT | `/tasks/{id}` | 修改任务，返回完整对象 |
| DELETE | `/tasks/{id}` | 删除任务 |
| POST | `/instructions/parse` | 数据库名称匹配与自训七分类模型解析 |
| POST | `/jobs` | 提交坐标试抓、已保存任务或携带解析凭据的语言执行 |
| GET | `/jobs/{id}` | 获取进度、结果或错误 |

## 清除所有记忆

`POST /memory/clear`，请求体为 `{}`。网页在发送前要求用户确认删除范围。后端先中断当前动作、恢复机械臂和三色方块，再在一个 SQLite 事务中清空当前数据库的物体、放置点及任务，包含名称、别名、坐标和技能关联。解析凭据与运行记录同时清除，旧帧和旧指令不能继续使用。

响应示例：

```json
{
  "status": "cleared",
  "cleared": {"objects": 3, "placement_points": 2, "tasks": 1, "parses": 2, "jobs": 1},
  "message": "所有物体、放置点、任务和运行记录已清除，场景已重置。"
}
```

`cleared` 为各类实际删除数量。重复调用可成功返回零计数。模型权重、训练数据和接口设置保留，不删除其他数据库文件。若场景重置失败，不清空数据库；数据库删除失败时事务回滚。重新登记使用递增 ID，避免旧页面误认目标。

## 场景与拾取

`GET /simulation/scene` 示例：

```json
{
  "frame_id": "frame-42",
  "width": 640,
  "height": 400,
  "image_url": "/api/v1/simulation/frame.png?frame_id=frame-42",
  "coordinate_frame": "world",
  "unit": "m",
  "objects": [
    {
      "scene_object_id": "cube-red",
      "color": "red",
      "screen_position": [0.61, 0.69]
    }
  ]
}
```

`image_url` 可为绝对 HTTP(S) 地址、站点根路径或相对 API 根地址的路径。每个 frame_id 对应同一次渲染的图像、深度、分割图和物体姿态。当前服务通过单个 `/simulation/stream` 连接推送画面，目标上限 24 FPS，默认 640×400，实际取决于渲染性能。`screen_position` 是方块上表面中心在图像中的归一化位置，仅供颜色定位按钮使用；用户点击标记采用实际选点位置。

响应另含 `scene_id`、`state_version`、`held_object_id`、`is_running`、`requires_reset`、`target_fps`、`simulation_time` 和 `camera`（view_matrix / projection_matrix）。坐标试抓后需要重置；普通任务成功后可继续，失败或停止后需要重置。后端保留至少 24 帧、默认 72 帧供图像读取和点选，过期帧返回 409。

连续流类型为 `application/x-panda-frames`。每帧由 4 字节大端 JSON 字节长度、UTF-8 JSON 元数据和 PNG 字节依次组成；元数据 `image_size` 指定随后 PNG 的字节数。网络块边界与帧边界无关。流总是发送当前最新帧，不给慢客户端累积历史队列。前端用固定 canvas 绘制，并用实际绘制帧的 frame_id 点选。保留单帧 PNG 接口供启动首帧和兼容使用。

`scene_object_id` 是稳定场景标识；数据库物体 ID 与 PyBullet 每次启动分配的临时 `body_id` 不应混用。后端维护它们的映射。

场景接口只提供相机显示与点选定位信息，不返回物体名称或真实三维中心坐标，也不会将三个仿真方块自动登记。用户通过点选接口采集坐标，再自行命名保存。

`POST /simulation/pick` 请求：

```json
{"frame_id":"frame-42","x":0.61,"y":0.69,"scene_object_id":"cube-red"}
```

`x/y` 的范围为 `[0,1)`，原点在画面左上。直接点击时 `scene_object_id` 可以省略；颜色按钮会附带预期方块标识，后端验证该像素。画面任何位置都可以采集：方块、机械臂使用同帧深度；地面和无有效深度的背景使用 Z=0 平面投影。坐标不可反解或帧已过期时返回明确错误，不按距离匹配附近物体。

响应：

```json
{
  "scene_object_id":"cube-red",
  "color":"red",
  "frame_id":"frame-42",
  "hit_position":[0.55,-0.18,0.05],
  "grasp_position":[0.55,-0.18,0.025],
  "coordinate_frame":"world",
  "surface_normal":[0,0,1],
  "position_source":"depth_surface_offset"
}
```

命中方块时使用 `depth_surface_offset`，沿命中面法线减去 0.025 m，保留切向点选误差。命中其他表面时使用 `depth_surface`，直接记录表面位置；地面或背景使用 `ground_plane_projection`。非方块选点返回 `scene_object_id=null`、`color="point"`、`surface_normal=null`，并返回实际 `screen_position`。

`hit_position`、`grasp_position`、保存的 `position` 和执行 `requested_position` 统一四舍五入到三位小数；界面固定显示三位小数，JSON 数字不保证保留末尾零。静态预览仍独立标注中心吸附或工作平面投影，不能作为实际深度测量。

## 物体

默认数据库为 `backend/data/registry.sqlite3`，首次启动 `GET /objects` 返回 `[]`。点选、试抓、重置场景均不插入物体；只有显式的 `POST /objects` 才新增登记。名称必须由用户提供，不能由颜色自动补全。用户已保存的内容在重新启动时保留。

`GET /objects` 返回对象数组。新增请求省略 `id`；修改及响应包含 `id`：

```json
{
  "id":1,
  "name":"客户零件 A",
  "aliases":["红色积木","零件 A"],
  "color":"red",
  "scene_object_id":"cube-red",
  "position":[0.55,-0.18,0.025],
  "coordinate_frame":"world",
  "skill_ids":["cube_grasp"]
}
```

后端负责物体和任务之间的名称/别名冲突检查、标识有效性、坐标校验和持久化。新增名称不修改模型词表。支持三色方块登记和任意命名点位，不自动生成额外物理物体。任意点位请求使用 `color="point"`、`scene_object_id=null`，服务器分配稳定的 `point-...` 标识；更新时保留该标识。采集和保存允许工作区外坐标，执行时才校验范围。

目标支持技能 `cube_grasp` 和 `cube_place`，可关联多个技能。`cube_grasp` 表示当前方块操作策略，允许抓取及后续放置；仅关联 `cube_place` 的目标不能抓取。移动是通用夹爪技能，命名点位可提供绝对目标坐标。坐标属于用户示教位置，物体被移动后需要重新采集，不会自动被仿真真值覆盖。

## 放置点

放置点存于同一 SQLite 文件中的 `placement_points` 表，字段为 id、name、aliases 和 position。接口另返回 `coordinate_frame="world"`。创建表使用增量方式，不修改原有 objects/tasks 记录；首次放置点库为空，点选和试抓不自动插入记录。

```json
{
  "id": 1,
  "name": "放置区A",
  "aliases": ["归位点"],
  "position": [0.450, 0.150, 0.000],
  "coordinate_frame": "world"
}
```

前端登记放置点使用 `/simulation/pick` 的 `hit_position`，不使用减去方块半边长后的 `grasp_position`。XYZ 表示地面或水平方块顶部的支撑面位置，例如落地方块顶部 `Z=0.050`。夹爪高度由后端结合 5 cm 方块尺寸和实际持物偏移换算，不把用户提供的表面位置替换为场景真值。

采集和保存可保留任意表面坐标；执行前检查工作区和高处支撑面，放置时读取真实姿态再次检查。支撑方块无需登记；顶部倾斜不得超过 5°，选点距面边缘至少 5 mm。顶部高度容差为 5 mm，以容纳深度像素及坐标记录误差，原选点坐标保持不变。侧面、边缘、悬空、支撑物移走、以当前持物自身为支撑或上层已占用的位置会被拒绝。支持多层方块堆叠，释放后检查与选定支撑物的接触和通往地面的支撑链，不支持机械臂等其他表面。所有坐标保留三位小数，名称/别名与物体和任务共享冲突检查。被任务引用时删除返回 409。

## 任务

`GET /tasks` 返回任务数组。新增请求省略 `id`：

```json
{
  "id":1,
  "name":"零件归位",
  "aliases":["收拾零件"],
  "steps":[
    {"action":"GRASP","object_id":1},
    {"action":"MOVE","position":[0.55,0,0.25],"coordinate_frame":"world"},
    {"action":"WAIT","seconds":1},
    {"action":"PLACE","object_id":1,"placement_point_id":1}
  ]
}
```

`MOVE.position` 表示夹爪抓取参考点（现有 `GRASP_LINK=11`）的绝对世界坐标。移动时先抬高到过渡高度，再移动和下降。`PLACE` 必须提供 `placement_point_id`；后端读取该点的表面坐标，自动移动、下降并释放当前夹持方块，检查目标一致、位置占用、地面或方块支撑和稳定状态。配置 GRASP → PLACE 即可完成定点抓放或堆叠。

新任务保存时缺少放置点返回 422。旧任务数据原样保留，缺少目的地的旧步骤在执行前返回明确错误，需要编辑补选放置点，不再默认当前 XY。

后端在提交前检查步骤顺序、技能和参数，运行时再次检查实际持物状态。每步完成后反馈 `step_results`，任何一步失败就停止后续步骤。任务不支持递归、循环和条件分支。

## 语言解析

接口使用 `llm/checkpoints/intent_v2.pt` 的真实四层 Encoder 权重。没有方向分类或方向字段。数据库匹配并替换目标名称后，后端按目标占位符和现有动作词定位唯一命令；普通背景分句不参与模型分类。保留完整的 `normalized_instruction` 供查看，实际送入模型的片段为 `model_input`，网页显示为“有效命令”。例如“我在打游戏，我想抓取零件A”对应 `model_input="抓取<OBJ>"`。后端不使用整句词语白名单，也不枚举指定开场白。

“我在上课，我想收拾零件”在任务名或别名“收拾零件”已登记时，模型输入 `<TASK>`，预测 RUN_TASK。名称仍按登记信息匹配，“整理零件”需要先登记为别名，不能仅靠动作分类器猜任务。原句中的明确条件、否定、询问、预约时间、多目标、多动作和相对位置仍会被检查，不能通过裁剪片段绕过。“抓取零件A，放下”等没有连接词的多个动作也会被拒绝；“开始抓取零件A”属于一个命令。动作词规则只负责定位和计数，不返回动作类别或伪造模型分数。

停止同样提取有效分句，例如“想一想，别动”以 `model_input="别动"` 交给模型预测。只有模型预测 STOP 且无目标时，才将“别动”等短语视为停止，而非否定执行。没有目标或动作词的纯闲话不会生成可执行停止指令。

中文名称采用连续字符串匹配，不根据普通修饰词猜测名称边界；例如只登记“零件A”时，“大零件A”会转换为“大<OBJ>”。优先选择最长的登记名称，ASCII 字母数字名称仍检查边界。未收录的字符正常编码为 `<UNK>`，不按未知字符比例拒绝整句。实际模型仍可能因为意图或置信度拒绝输入，匹配到名称并不保证得到 ready。

`POST /instructions/parse` 请求：

```json
{"instruction":"帮我抓取红色方块","model":"transformer"}
```

`model` 可省略，提供时只能为 `transformer`。模型加载失败返回 503，不回退为规则预览。成功解析结构示例：

```json
{
  "parse_id":"parse-123",
  "status":"ready",
  "source":"model",
  "model":"transformer",
  "instruction":"帮我抓取红色方块",
  "normalized_instruction":"帮我抓取<OBJ>",
  "model_input":"抓取<OBJ>",
  "action":"GRASP",
  "object_id":1,
  "object_name":"红色方块",
  "confidence":0.96
}
```

任务指令使用 `RUN_TASK`、`task_id`、`task_name` 与 `<TASK>`；`STOP` 无需目标。MOVE 使用物体库中已登记的普通点位，响应另含 `position`；不能仅指定一个方块而省略移动目的地。

PLACE 支持“把零件A放到放置区A”和“放到放置区A”。两种名称都由数据库解析，物体和目的地均使用已有 `<OBJ>` token，模型仍只预测 PLACE，不新增词表或输出类别。前者验证指定物体与当前持物一致；后者使用当前持物。都必须先夹住物体，缺少目的地或没有持物时返回 clarify。

“把当前物体放到放置区A”等明确指代当前持物的表达可以省略物体名称；“把未知物体放到放置区A”不能仅凭匹配到放置点就使用当前持物，显式物体名称未登记时返回 unmatched。

PLACE 的 ready 响应另含 `placement_point_id`、`placement_point_name`、`placement_position`，以及可得的 object_id/object_name。客户端执行时可携带 placement_point_id，但服务器仅使用已保存的解析结果，并拒绝篡改 ID、坐标或更新点位后使用旧凭据。

`confidence` 来自模型 softmax，`inference_ms` 为实际模型调用耗时。示例分数仅说明结构，不是固定返回值，也不表示物理操作成功概率。七类输出为 GRASP、PLACE、MOVE、STOP、RUN_TASK、CLARIFY、UNSUPPORTED。

未匹配或拒绝执行时返回：

```json
{"status":"unmatched","message":"没有登记这个物体，请先添加名称或别名。"}
```

其他不可执行状态为 `clarify`、`unsupported`、`low_confidence`。只有 `ready` 显示执行按钮。解析与执行分开，模型结果必须经过后端协议、目标数量、特殊指令结构、否定表达、技能和参数校验。首次空库中的未知名称不会自动按颜色或距离识别。

`parse_id` 有效期为 120 秒，只能成功提交执行一次。后端保留原始解析结果、数据库快照摘要、scene_id 和状态版本，拒绝篡改动作/目标、重复执行和场景或配置变化后的旧凭据。解析过程中若状态发生变化，执行时同样拒绝。

## 执行与停止

中文指令执行：

```json
{"action":"GRASP","object_id":1,"source":"language","model":"transformer","instruction":"帮我抓取红色方块","parse_id":"parse-123"}
```

组合任务执行：

```json
{"action":"RUN_TASK","task_id":1,"source":"task_builder"}
```

语言定点放置（先解析“把零件A放到放置区A”）：

```json
{"action":"PLACE","object_id":1,"placement_point_id":1,"parse_id":"parse-place-123","source":"language","model":"transformer"}
```

放置步骤成功结果包含 `placement_point_id`、`requested_surface_position`、实际方块中心 `position`、`placement_error_m` 和 `supported`，另含 `support_type`（`ground` / `cube`）、`support_scene_object_id`（地面为 null，例如方块为 `cube-blue`）和 `stable_seconds=1`。夹爪退出后持续 1 秒验证实际支撑接触及稳定性，误差按方块底面中心与所选表面位置计算。实际接触和稳定验证失败时任务失败，不切换到附近空位。

点选试抓（物体未登记时可省略 object_id）：

```json
{"action":"GRASP","object_id":1,"scene_object_id":"cube-red","grasp_position":[0.55,-0.18,0.025],"coordinate_frame":"world","frame_id":"frame-42","source":"coordinate_test"}
```

坐标试抓必须使用请求提供的抓取位置，并明确换算为夹爪目标位姿；仿真真值可以用于误差评估，不能用于掩盖用户坐标偏差。

`POST /jobs` 接受三种来源：`coordinate_test` 使用提交坐标；`task_builder` 使用已保存 task_id；`language` 必须携带有效 parse_id，执行以服务器保存的解析为准。缺少合法来源和参数时拒绝，不接受任意伪造动作请求。

动作任务返回 202 和 `{"id":"job-1","status":"queued","message":"任务已接收"}`。状态为 `queued`、`running`、`succeeded`、`failed`、`cancelled`。前端每 500 ms 获取状态，终态停止轮询。后端拒绝并发控制同一机械臂。UI 断开不等于取消后端任务。语言 STOP 也可通过凭据执行，此时等待停止完成后返回 200 和停止响应；网页直接使用独立停止接口。

语言动作和组合任务带 `mode="program"`，终态包含 `requires_reset`、`step_results` 及 `result.steps`。各成功步骤包含 action、status 和实际结果，例如夹持抬升、夹爪到位误差或放置支撑。正常任务完成不要求重置，失败和取消需要重置。坐标试抓的结果字段见下表。

坐标试抓必须提供 `grasp_position`，其优先于数据库保存坐标。普通点位的 `scene_object_id` 可为 null；已保存点位也可以用 object_id 引用。后端不猜测目标，在夹爪闭合后通过实际双指接触确定夹持物，再验证抬升；无接触时失败，相应目标误差字段为 null。结果另含 `detected_scene_object_id`。

请求 frame_id 可以来自编辑前，只要场景未重置。重置前帧不能用于新场景试抓。当前执行工作区为 X[-0.1,0.9]、Y[-0.6,0.6]、Z[0,0.6] m，真正可达性与成功由物理执行检查；此范围不限制坐标采集和登记。

`GET /jobs/{id}` 返回 `current_step`、`total_steps`、`requested_position` 和说明。终态 `result` 包含：

| 字段 | 含义 |
|---|---|
| `success` | 指定目标是否通过抓取判据 |
| `requested_position` | 用户提交的实际执行坐标 |
| `object_initial_position` | 测试开始时真值位置，仅用于误差/抬升验证 |
| `coordinate_error_m` | 输入坐标与目标初始中心的距离 |
| `end_effector_error_m` | 夹爪实际位置与最后一个运动目标的位置误差 |
| `minimum_lift_m` | 完整稳定验证期间的最低抬升；未完成则为 null |
| `final_lift_m` | 测试结束时实际抬升 |
| `held_seconds` | 完整通过稳定夹持验证时为 3，否则为 null |
| `bilateral_contact` | 结果生成时是否保持双指接触 |

`POST /simulation/stop` 使用独立于语言解析的停止接口，当前约定在实际停止完成后返回 `{"status":"stopped","message":"机械臂已停止"}`，不能把请求受理等同于停止成功。后续若改为异步停止，需一起扩展前端状态处理。

`POST /simulation/reset` 等待当前动作取消并重建场景后返回 `{"status":"ready","message":"机械臂和三色方块已重置，可以重新点选测试。"}`。它清除旧相机帧并生成新的 scene_id，不修改 SQLite 登记信息。

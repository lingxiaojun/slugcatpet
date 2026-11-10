# 程序结构（开发者 / fork 用）

物理、绘制、平台这些底层是核心，只在固定的地方留了钩子。
能力、物品、行为、配件、角色各占一个目录，每样东西一个子目录。加新的一般只要新建子目录，再在注册表末尾加一行。

## 目录

```
slugcatpet/
  core/         物理：两节质点身体、碰撞、游泳 / 憋气、尾巴、水中上下错开。不认识任何猫种或物品
  items/        物品：果子、石头、矛、竖杆 / 横杆、暖灯、黏菌、蝙蝠、蛋虫、蛋虫蛋、珍珠。每种一个子目录
  world/        环境：雪、雨、气泡、爆炸特效（水在 core/water.py）
  abilities/    能力：舌头、飞升、爆破、空翻、水生、重压、骨针
  accessories/  配件：疤、鳃、尾巴斑点。纯外观、会动、能单独设颜色
  behaviors/    行为：所有猫会做的事，包括取果、爬架、发呆、游泳这些原生行为
  brain/        引擎：状态机 fsm、状态基类 state、心情仲裁 desire、多猫协调 social、
                核心状态 core_states（被拖 / 空中 / 晕 / 死）、共用动作 skills/（爬杆、横杆、走位、投掷）
  planning/     规划：目标、够取方式 Reach、执行器；通用够取（走、跳、爬竖杆、后空翻、杆网寻路）
  characters/   角色：CatDef、数值 stats、性格 personality（含 Traits）、JSON 读取
  appearance.py 一只猫的外观 `Look`（整套皮肤、分部位皮肤与颜色、尾巴形状、配件）和取色优先级
  skins/        DMS 皮肤读取与解析；resolver 按部位组查找图集
  rendering/    绘制：图集、身体各部件、尾巴贴图、绘制录制回放
  audio/        音乐播放器、场景配乐、雨声、音效混音器 sfx（可调音高）、从游戏资源包重建 ogg / wav
  platform/     系统相关：光标、置顶、单实例、全屏检测、挡雨窗口、开机自启、AppID、垂直同步
  control/      手动控制（键盘操控一只猫）
  meow/         按键喵（照 Push to Meow）：声音表、素材导入、按键判定和喵带的动作
  ui/           界面：theme/（配色与样式表）、panels/（各面板）、widgets/（通用小部件）
  resources/    characters/*.json（内置角色）、layouts/（部件摆位）、icons/
  tuning.py     几处共用的常量
  petunit.py    一只猫：把身体、图形、能力、配件、状态机组装起来，每 tick 推进
  window.py     桌面窗口与世界：tick 循环、环境、绘制顺序
tests/          行为检查（见文末）、多猫互动统计
tools/          params_doc.py（生成 ai-docs/PARAMS.md）、gen_arcs.py（生成 resources/arcs.json.gz）、gen_vorbis_setups.py
```

## 五种模块

| | 是什么 | 放在哪 | 例子 |
|---|---|---|---|
| 能力 | 身体能做什么：器官、物理特性、绘制、够取方式、手动控制 | `abilities/<key>/` | 舌头、爆破、水生 |
| 物品 | 世界里的东西：物理、绘制、放置 / 移除 / 拖拽、提供哪些用法 | `items/<key>/` | 果子能吃、石头能扔、杆能爬 |
| 行为 | 猫想做什么、怎么做：状态、强制规则、心情候选 | `behaviors/<key>/` | 取果、扔石头、舔光标 |
| 配件 | 只管外观的额外部件，任何猫都能戴，不带能力 | `accessories/<key>/` | 疤、鳃、尾巴斑点 |
| 角色 | 纯数据：有哪些能力、数值、性格 | `resources/characters/<key>.json` | 圣徒 = 舌头 + 飞升 |

它们之间的关系：

- 角色只写有哪些能力。能力决定身体标签，行为按 `requires` 对照身体标签自动挂上，角色里只写例外。
- 行为不认识具体的物品，只认用法。取果找「能吃的」，扔石头找「能扔的」。
- 配件和能力分开：白猫戴上鳃只是好看，水生体质是溪流的能力。
- 一件事归一个行为管全：用嘴吃的逻辑（取果、水里和失重时就近吃）都在 `behaviors/fetch/`。游泳、发呆这些基础状态只留扩展点，不写别的功能。
- 咬一口统一经 `fetch/chew.py`：身子停住、头没泡在水里（水生的不限）才咬。在杆上抓到就在杆上吃。
- 状态做完回待机用 `fsm.settle()`：还在杆上就交给爬架，不直接松手。

### 身体标签

`petunit.tags`，由 `abilities.body_tags()` 算出：默认标签（`BODY_TAGS`，只有 `mouth`）加上各能力 key，再加能力的 `provides`、去掉能力的 `removes`。

- 行为写进 `requires` 的标签不齐就不挂载。取果、留个果子要 `mouth`；矛大师的骨针能力 `removes = ("mouth",)`，于是用嘴吃的行为整套不挂，角色 JSON 里不用逐个关。
- 会不会吃一件东西统一用 `pet.can_eat(物)`：有 `mouth`、物品能吃、食性允许。手部管家、手动控制、鼠标喂蝙蝠都只看它。
- 吃下东西的结算统一走 `body.feed(1/4 格数)`（饱食、好感、体力）。骨针进食也调它。
- 吃完一样东西得几个 1/4 格由 `traits.nourishment(物)` 定：物品的 `food_points`（默认 1），肉食者只得 `food_points` 个 1/4，其余猫得 4 倍；`carnivore_full` 的（蛋虫蛋）肉食者也得 4 倍。
- 尸体（模型的 `carcass` 为真）每口得性格 `meat` 个 1/4 格，`meat` 为 0 的猫不啃。`body.consume` 啃尸体每口都结算、啃完不消失，由啃的一方扔开。

### 饱食

`body.food` 是整格，`body.food_q` 是攒着的 1/4（0–3，存档里也存）：

- 加 1/4 只在没吃满时生效，攒满 4 个进一格；吃满时清零。扣整格（死亡、冬眠）不动 1/4。
- HUD 的下一个空格按 1/4 画扇形（12 点起顺时针），透明度慢慢呼吸，多攒一个时亮一下。

### 需求与意图

闲待到点时由 `fsm.intents`（`brain/intent.py`）挑下一件事：各行为用 `fsm.intents.add(名, 效用, 开始)` 登记，按 `exp(效用 / INTENT_TEMP)` 的比例随机挑。

| 意图 | 谁登记 | 效用 |
|---|---|---|
| 闲待 | 发呆 | `HANG_UTIL` |
| 玩 | 发呆 | `PLAY_UTIL` × 精力换算的好动程度，累了、困了降；选中后由心情抽签决定玩什么 |
| 歇一会 | 休息 | 体力越低越想 |
| 睡 | 休息 | 困意越高越想，太冷时为 0 |
| 吃 / 打猎 | 取果 / 打猎 | 「睡前要吃够」和「顺手吃」取大（`brain/needs.py`） |
| 去爬架 | 爬架 | `PERCH_UTIL`，累了、困了降 |
| 取暖 | 取暖 | 越冷越想 |

需求：

- **耐力**（`body.energy`）：低于 0.1 强制趴下，回到 0.5 起来。趴下的原因（累倒、歇一会、打盹、冬眠）由趴下状态带着。
- **困意**（`body.sleepiness`，存档）：醒着涨，睡着降。食物够就冬眠（睡满扣食物、困意清零），不够就打盹；到 1 就地睡。
- **饿**：只在冬眠时扣食物。食物不够冬眠时「睡前要吃够」随困意变强、压过困意，到 `EAT_URGENT` 由强制规则去找吃的。
- **冷**：到 0.3 由强制规则去取暖。

**下雨**（`world/rain.py`、`behaviors/rain/`）：

- 每列记雨落到的高度：被窗口挡住的列只落到窗口顶边，窗口内和下方算室内。窗口由 `platform/shelters.py` 每 250ms 取一次。
- 窗口里默认下一层淡雨（雨丝和普通、暴雨两层雨幕按 `SHELTER_K` 打折）。设置「窗口里不下雨」（`rain_window_block`）打开后窗口里不画雨，整屏都被窗口挡住时不出雨帧。
- 雨量满格对应强度 0.7，过 0.35 露天开始被推（`push_force`）：猫和散落物品被推动，猫和活的小生物会被砸晕，被光标拎着的不算。被雨推后一段时间内摔落不致死、不记仇（`PetUnit.buffeted_until`）。
- 雨量将过危险线时露天的猫走到最近的遮挡下；危险期间闲逛、爬架、取果、打猎都不选露天目标（`rain.keeps_out`）。
- 在遮挡下的猫不主动出去：选目标、起跳前用 `motor.rain_blocks`（要去的点）和 `motor.arc_blocked`（跳的弧）查一遍；已经淋着雨就不管，交给躲雨。

**定身**（`behaviors/hold/`）：设置在 `pet.held`（姿势、视线），不存档。规则在不晕、不游泳时把猫拉回定身，所以被拖、被晕、游完泳后在落点接着定身；在杆上时先记下抓的杆，打断前一状态后再抓回去。定身时体力、困意、寒冷不变，不被雨推，别的猫不把它从杆尖顶下去。

**爬架**（`behaviors/perch/`）：挑一处走得到的杆上位置，经 `social` 预约，用 `SpotController` 走过去待着，待够了换一处或下来。上横杆站稳后也交给它。

### 扩展点

基础状态在固定位置跑别的行为挂进来的子任务：`fsm.add_task(扩展点, fn)`，`fn(*参数)` 返回 True 表示这一 tick 由它接管，状态自己的逻辑跳过；只想顺手做点事的返回 False。

| 扩展点 | 在哪跑 | 参数 | 已挂的 |
|---|---|---|---|
| `swim` | 游泳：换气、休息之后 | | 取果：游过去抓住、就地啃 |
| `zerog` | 失重待机：最前面 | | 取果：飘过去抓住、就地啃 |
| `zerog_aim` | 失重时朝目标漂之后（闲逛、追食物） | 目标 x, y | 失重吐舌 |
| `lie_down` | 主动要歇、要睡、懒着趴下之前（强制趴下、睡不走这里） | 趴下原因 | 挨着睡 |

### 技能槽

共用动作里有一步可以由能力换成自己的做法：能力在 `skills` 里登记 `槽名 → 类`，用到的地方 `pet.skill(槽名)` 取一个实例，没有就走默认做法。

| 槽 | 谁用 | 默认 | 已有实现 |
|---|---|---|---|
| `fetch_assist` | 取果：空中补救（`update / abort / reset / holding`） | 不补救 | 舌头：抢射粘住 |
| `hpole_mount` | 横杆：怎么上杆（`anchor_x(默认)`、`update(锚点)` → `running / grip / giveup`） | 跳起徒手抓 | 舌头：钩着荡一会再收绳 |
| `grow_weapon` | 生气、打猎：从身上长一件武器到空手里（`ready()`、`update()` → `running / got / fail`，拿到的在 `weapon`） | 只捡地上的 | 骨针：从尾巴拔骨针 |

器官的收尾统一走 `pet.release_organs()`（状态机里是 `fsm.release_organs()`）：依次调各能力的 `release(pet)`，再解除吊挂。状态被打断、被砸晕、上杆抓住时都调它，通用代码不直接碰某个器官。

### 物品用法

定义在 `items/base.py`：

| 用法 | 提供它的物品 | 用到它的行为 |
|---|---|---|
| `edible` 能吃 | 果子、黏菌、蝙蝠、蛋虫蛋、蛋虫尸体 | 取果（尸体只给会啃肉的猫，拿起来伏下身子啃） |
| `throwable` 能扔 | 石头、矛 | 生气扔石头 / 矛、打猎 |
| `climbable` 能爬 | 竖杆、横杆、扎在墙上的矛 | 爬架、上横杆（溪流、圣徒的上杆方式），以及取果时的杆网寻路 |
| `warm` 能取暖 | 暖灯 | 取暖 |
| `carryable` 能拿 | 果子、石头、矛、黏菌、蝙蝠、蛋虫蛋、蛋虫尸体、珍珠 | 手动控制时拾取 |
| `toy` 能玩 | 珍珠 | 玩珍珠 |

行为用 `fsm.items(用法)` 取场上的这类物品，顺序和放置顺序一致。

### 活物

蝙蝠、蛋虫这类会动会死的物品在模型上设 `creature = True`，带 `dead`、`stun`；场上的活物用 `items.base.creatures(win)` 取。

- 武器打中活物统一调 `struck(HIT_STONE / HIT_SPEAR / HIT_SLAM, rng)`，死还是晕由活物自己定。石头、矛、重压撞击都走它。
- `stops_weapons = True` 的大活物（蛋虫）挡住武器：矛扎进它身上那一节（模型给出 `chunks`），石头弹开。
- 打猎只打 `huntable` 的活物（蝙蝠要在飞），打下来能吃到多少由 `loot_quarters(traits)` 或它本身的营养定；大活物只用扎得进的武器打。
- 骨针扎中活物按 `needle_quarters` 喂矛大师，再调目标的 `drained()`（蛋虫被吸过就不爆蛋）。
- 雨推、舌头不粘、猫东张西望也按 `creature` 认。

### 珍珠

`items/pearl/`：物理沿用石头（`Pearl` 继承 `Stone`，不当武器扔），另带 `text`、`color`、`glyph`。`color` 为空是没写过的灰珍珠。

- 右键（物品类型设 `secondary = True`，实现 `on_secondary`）打开 `ui/panels/pearl_editor.py`：文字最多 `TEXT_MAX` 个字符，色板 `PALETTE`，勾选悬浮符文。确认后珍珠变色；符文编号在第一次勾选时随机定下并存档。
- 编辑期间 `editing` 为真：珍珠停在原处，不碰撞、不能被拖或被猫拿（`affords_now` 为假），猫手里的先松开。
- 光标停在写过字的静止珍珠上 `HOVER_DELAY` 个 tick 后，`draw_overlay` 在它旁边画文字框。
- 符文（`TinyGlyph0–13`）只在珍珠静止 `CALM_TICKS` 个 tick 后浮出，被拿起、拖着、飞行时收起。
- 清空（`clear_all_items`）经 `keeps()` 留下写过的珍珠；逐个移除前经 `removal_note()` 弹窗展示文字，确认才移除。
- 猫玩珍珠由 `behaviors/pearl_play/` 管：走过去拿起、拿着晃一会儿、放下或随手抛开。

### 场景存取

`items/scene.py` 把场上物品存成字典、再摆回去，重启保留和布局蓝图共用它：

- 物品类型实现 `dump(obj)` / `load(win, d, fit)` 才会被存，不实现的（蝙蝠、蛋虫）不存；`blueprint = True` 的（竖杆、横杆、灯笼）归布局蓝图管。
- 坐标经 `Fit` 换到当前屏幕：不放大，屏幕变小时等比缩小；纵向从地面量，横向从中线量，贴边的从那条边量。
- 布局蓝图预览用 `win.set_ghosts`：蓝图里的物品由 `draw_ghost` 画成淡蓝虚影，场上同类物品画淡。

### 杆网寻路

`planning/climbmap.py` 把杆和地面上的位置（地上、爬竖杆、站杆尖、吊横杆、站横杆）连成图：杆上移动按速度算耗时，起跳和松手查预先模拟的弧线（`jump_arc`）。落地会摔晕的起跳和松手不算。出边按需算，几何不变时复用；A\* 找够着目标的路线。

- 弧线按内置角色的身体数值离线算好，存在 `resources/arcs.json.gz`；改了身体数值或物理后用 `tools/gen_arcs.py` 重新生成，不在文件里的现算。
- 杆变了以后，各只猫的杆网由 `planning/budget.py` 每 tick 按固定额度轮流算，算完之前不走杆网。每 tick 现算的寻路次数有上限，满了先按上次的结果答。
- `climb_reach.py` 把它包成够取方式 `polepath`；`climbmoves.py` 逐条边驱动身体，失败时从当前位置重新找路，别处增删杆不打断。
- 杆上的姿势（爬竖杆、吊横杆、翻上去、站横杆、上杆尖、站杆尖）每种一个推一 tick 的函数，自动行为和手动操控（`control/moves_pole.py`）共用。
- 抓杆都经 `grip()` 记下抓的杆（失重时是 `body.zerog_pole`）。杆没了时由 `PetUnit.check_grip` 统一松手，`fsm.pole_lost` 让当前状态收尾。

控制器可以实现 `on_geometry()`：几何变了时执行器先问它，返回 True 就接着跑，否则中止重规划。只在地上走、跳的控制器都返回 True。

一件物体提供的用法可以随状态变：物品类型设 `varies = True` 并实现 `affords_now(物体, 用法)`。矛在手里、地上时能扔，扎进墙后不能扔、只能爬；扎进或拔出时物体自己调 `win.geometry_changed()`，规划器据此重算路线。

扎得进东西的武器在模型上设 `pierces = True`，按投掷态飞（四向出手、矛身只取水平朝向、可以带瞄准方向）的设 `aims_line = True`。行为和手部管家只看这两个属性，不认具体物品。
扔出的武器扎中或穿过东西时调扔它的身体上的 `body.pierce_hooks`：`fn(身体, 武器, 目标, 目标原本活着)`。骨针能力用它进食。

能从身上长出的武器由物品类型设 `grows = True` 并实现 `grow(win, x, y, 身体, 花纹)`，能力通过 `win.grow_weapon(身体, x, y, 花纹)` 要一件。

大件（矛）在模型上设 `bulky = True`：同时只能拿一件，数值 `dual_wield` 的猫（矛大师）不限。拿之前用 `body.can_hold(物)` 判断，手动控制选拾取目标、生气捡武器都先过它。

猫有两只手，手持物在 `body.grasps`（`l` 为主手，投掷、吃、丢下都先找它）。拿起放下走 `body.grab()` / `release()`，换手用 `start_switch_hands()`。数值 `hands` 为 1 的猫（猫崽）只用主手、不换手，找空手统一走 `body.free_hand()`。手持物的位置由 `rendering/graphics.py` 每帧贴到手上，爬杆、趴下时跟着手的动作走。物品模型用 `edible` / `throwable` 类属性声明能不能吃、能不能当武器扔。

手持物可以跨状态一直拿着，由 `brain/hands.py` 的手部管家 `fsm.hands` 管：

- 每样手持物记一个理由（`EAT_LATER` 留着吃 / `WEAPON` 当武器 / `None` 没用）。行为收尾时用 `fsm.hands.adopt(物, 理由)` 把没用完的东西交给它，不要自己松手。自己拿起的东西按 `can_eat`、能不能扔推断理由。
- 状态用 `hands` 声明进入时要几只空手（`any` / `one` / `free` 全空），要按情况变就覆盖 `hands_needed()`。不够时管家按理由先放下一样。
- 管家还负责：留着吃的东西揣多久再吃（真饿了不等）、放下没用的东西、消气后放下武器、站着发呆时摆弄手里的东西（挪个地方、换手、低头看）、被拎起时甩太狠脱手或拿武器打光标挣脱、被光标抢走时的反应。
- 肯不肯拿一件东西当武器统一用 `can_wield(身体, 物体, hunting=False)`：扎得进的对光标只在好感低于 `SPEAR_TEMPER` 时用、打猎时随时用，`spear_toss` 的猫（圣徒）不用。
- 饿了时，手里武器上串着的能吃的东西（`impaled`）会被另一只空手取下一个留着吃，由取果行为管。
- 被砸晕、死亡、被收起、重置时一律松手。

### requires

行为的 `requires` 可以写身体标签（含能力 key），也可以写物品用法：

- 写标签：这只猫有这些标签才挂载。
- 写用法：不影响挂载，只说明这个行为会用哪类物品（参数文档里会列出来）。没有这类物品时，行为自己的触发条件不成立，也就不会去做。
- 什么都不写：每只猫都有。`essential = True` 的行为角色里关不掉（发呆、休息、游泳）。

### 性格

性格数据在 `characters/personality.py` 的 `CatPersonality`，换算后的作用在同文件的 `Traits` 里。
行为和引擎只读 `fsm.traits`，不读原始数值。每个性格轴作用在哪，登记在同文件的 `EFFECTS`，参数文档会列出来。

- 精力（`energy`）、亲和（`sympathy`）每只猫一份（`pet.personality`，存档），新猫按猫种默认值和 `vary` 定；性格弹窗（`ui/panels/personality.py`）用 `pet.set_personality()` 改，`Traits.apply()` 原地重算，立即生效。
- 所以行为要在用的时候读 `fsm.traits`，不要在 `mount` 时存下来。
- 两轴在中性带（`PERS_NEUTRAL_LO`–`HI`）内时，后加的作用（闲待时长、趴着发呆、困意、好感涨跌、舔光标、打猎意愿）都不生效。
- 好感涨跌统一在 `body.temper_shift()` 里乘 `traits.temper_fac`，各处照常调用即可。
- 心情候选的权重在抽签时乘 `traits.mood_weight(心情名)`（精力和 `toy_pref`）。用 `mood_candidate()` 建候选就自动带上。
- 食性用 `traits.eats(物品)` 判断想不想吃，`traits.food_time()` 让肉食者偏好肉。身体能不能吃另算，见上面的 `can_eat`。

### 多猫协调

`brain/social/`，每个世界 tick 推进一次。行为只经这里了解别的猫，不直接读别的猫的内部状态。

- 占位（`space.py`）：每 tick 最先给每只猫建一份 `Occ`：姿势（地上 / 空中 / 踩在别的猫身上 / 杆上 / 被带着 / 水里 / 失重）、身体占的范围（正在趴下或起身的按变完的样子）、预约的落脚处（`target`）、能不能叫它让（`can`；操控中、定身、被带着、晕、睡、在忙更要紧的事都不能）、挡不挡地上走的猫（低处杆上的也挡）。
  - 停住（`parked`）只看猫自己：在地上没有要去的地方、趴着、叫不动，或原地等迎面的猫先过；被撞得还在滑的、跟在同向的猫后面停下的不算。
  - 预约：在走的猫按落脚处预约一段；`LieMark`（`claim("lie")`）是带朝向的趴下预约，趴下和挨着睡共用。
  - `extent(猫, d)`：停住的猫占的范围；地上站着、叫得动的连它朝 d 趴下的样子也算上，所以叫不叫它让，过它的猫看到的都一样。
  - `space.rows(我, t)`：我到 t 之间停住的猫（踩着的猫没走过它那头也算）按 `extent` 分排，中间落不稳脚（`LAND_WIDTH`）的合成一排，带着每排后面的落脚段（到下一排或走路范围的边为止）。
  - `space.blocks(我)`：臀不能待的地方：两边停住的猫按排整排算，加上别人的预约，按我的半宽外扩（挨着不算压到）。所以站得下的地方不会夹在一排里，预约两两不相交。
  - `space.settle_x(我, x, tol, keep)`：离 x 最近能站的地方，x 被占着时优先不用越过别人的那一侧；已经定下的落脚处还站得下就不换。
  - `space.reach(我, x, tol)`：可达查询。落脚处加停步误差离 x 不超过 `tol`、路上每一排都过得去，才返回落脚处，否则 None。
  - `body.reach(x, tol)` 是它的入口（没有别的猫时一律走得到）：行为只从走得到的候选里挑，`walk_to` 也是它。
  - `space.free` / `lie_spot`：一段地上有没有别人停着的身子或预约；趴下的位置和朝向，原地趴得下就原地，否则找最近的、走得到的。
  - `space.keepout(我)`：停住又叫不动或趴着的猫身边，加上大雨时露天的位置；杆网寻路不经过，它们变了 `static_version` 才变。
  - `space.mates(我, 状态)`：同一层地上站着或趴着的猫；闲逛走位、挨着睡、分享、盯着别的猫看、`lying_beside` 都用它。
- 走路契约（`core/creature.py`）：
  - `walk_to(x, tol)` 当场求 `reach`：到不了返回 False，不改现在的走路；到得了就记下意图（`walk_target_x`，只有行为写）和腿走向的落脚处（`target_x`）。同一意图重复下达不重算。
  - 到达只看落脚处：走到了就清掉意图，`walk_result` 记 `arrived`，这时离意图一定不超过 `tol`。
  - 走到一半路断了：通行当 tick 清掉意图，`walk_result` 记 `blocked`。`MakeWay` 挂的规则（`STALL`，优先级 25）当 tick 放下玩耍、待机里的这件事；找吃的、歇着、取暖、躲雨优先级更高，自己接着处理。
- 感知：`others(我)`、`state_of(猫)`、`wants_food(猫, 东西)`、`lying_beside(我)`。
- 关系（`bonds.py`）：每对猫一个亲疏值，`bond(我, 它)` 读。两只猫离得近时按双方亲和慢慢变好或变差（都趴着变好更快），被分享时变好，被顶下杆尖、被重压撞晕、被扔出的矛扎中时变差，平时慢慢回到初始值（猫种 `kin`）。存在存档的 `bonds` 里，移除猫时清掉它的。
- 远近意愿：`pull(我, 它)` 由我的亲和和我们的关系算出，-1 躲开到 1 凑近。闲逛走位（`wander_marks`）、爬架选位、挨着睡、分享都按它。
- 占用：`claim(我, 槽, 目标, 状态, ttl)` 占住一个目标，状态结束、被拖、晕、死、到时时自动失效；
  `free_for(我, 目标)` 判断能不能去，别的猫占着时只有比它近一半以上才抢。取果、捡石头、爬竖杆都是先判断能不能去、选定后占住。去横杆也会登记，但还不判断别的猫占没占。
- 通行（`traffic.py`）：所有让路判断都在这里，给让的一方下指令 `order(我)`。
  - 地上在走的猫每 tick 按现在的样子重求一次 `reach`（落脚处还站得下就不换，过不去了就重挑），到不了当场按路断交回。
  - 地上过路：身前一排进了视线就照它的走法做。走法由 `ground.py` 按 `ground.snapshot` 给的这排样子搜出（走、助跑、起跳、踩猫背再跳，按弧线查碰撞，取代价最小的；「叫这排的猫让开」作为整体变体一起比），结论按样子缓存，判断和执行是同一份走法。
    - 动作到第一跳为止，挂在身体上（`Stride`），身体在走路更新里自己执行；落地后按新的位置看下一排。
  - 叫猫让：走到等着那一步时叫走法里要让的，它们趴平、挪开了才放行。叫过的一直叫（跳在半空、换了一排也叫），直到越过它或不走了才放；让路的猫只在没有过路对象点名它时起来。
  - 迎面：对方整段要走的路（退后助跑时也算）挡到我的落脚处之前才算碰上。已经开始过的先过，叫不动的先过，再按优先级。输的一方原地等（`body.lane_hold`），这时算停住，过的一方把它算进身前这一排（叫得动就叫它趴下）。
  - 同向：跟在它后面，不超。
  - 状态不对就挪到对的地方：在地上和停住的猫叠着又在走的，穿过它走开，免得拖着它走；停在别的猫身上又没在走的，叫它自己跳下来，找不到能站的地方或叫了 60 tick 还在上面，才穿过臀碰着的那几只猫落到地上。
  - 兜底：有要去的地方却 `FALLBACK_TICKS` 一点没靠近，记下现场（写到 `~/.slugcatpet/slugcatpet.log`）并按路断交回；让路太久（`MakeWay` 的 `TIMEOUT`）也记下。设计上不该发生，触发即漏洞。
  - 水里：让的下潜（`core/swim_pass.py`），都潜不了时过的从水面跳过去。
  - 横杆：让的换到杆的另一面。竖杆：让的沿杆挪开，到顶站上杆尖，到底下到地上。
  - 杆上挨上时过的一方停住（`body.lane_hold`），谁都让不开就当场交回（`body.lane_blocked`）。
  - 失重抓杆：过不去的一方蹬开飘走，迎面时只蹬开一只。
  - 操控中的猫不当让的一方，也不被拦着；被带着的猫不在任何通道上。
- `MakeWay`（`behaviors/makeway/`）只执行指令：趴下的按指令的朝向趴，没有指令了、也趴够了就起来；让完回到待机或爬架，待机里要去的地方接着去。
- 背猫（`carry.py`）：一只猫用一只手拉着（`hand`）或背着（`back`）另一只，只叠一层，猫崽背不动。
  - 拉着：被拉的那一节和背者上身超过 `HAND_DIST` 时按质量互相拉近，背者按被拉的猫的质量跑慢；背着：被背的不积分、不和别的猫相撞，每 tick 贴到背者身上（成年猫沿脊柱往上错开，猫崽臀压在背者头上），画在背者身后。
  - 被带着的 AI 猫进入 `Carried`，待的时长按 `pull(我, 背者)` 定，拉在手里减半；到时拉着的先扭一会再挣脱，背着的往外跳下来。AI 猫背着操控的猫时到时用抗议跳把它甩下来。
  - 背者趴下、睡、入水、失重、吊横杆、晕得久、被甩得太快，或任一方离场、死、被光标拎起时松开；不存档。
- 定线：闲逛尽量不挑要从别的猫中间穿过的目标，爬架不挑要沿杆挤过别的猫的落脚点。

## 一个 tick

```
window._do_tick
  ├─ 水位
  ├─ 每只猫 check_grip：抓着的杆没了就松手
  ├─ social.tick       占位 → 通行 → 关系
  ├─ 每只猫 PetUnit.step
  │    ├─ fsm.update      每 tick 回调 → 强制规则（按优先级）→ 当前状态 tick → 体力 → 心情新鲜度
  │    ├─ ability.pre_step
  │    ├─ body.step       物理
  │    ├─ 寒冷结算、gfx.update
  │    ├─ ability.post_step、accessory.post_step
  │    └─ 尾巴
  ├─ carry.sync        被带着的猫贴到背者身上
  ├─ 物品推进、物体间碰撞、碰撞后推进的物品（灯）
  ├─ 雪
  └─ 雨：强度、风向、推露天的物品
```

物理固定 40Hz，绘制按插值因子 `_ts` 在两个物理 tick 之间插值。出帧节拍有四档（`window._set_mode`）：

- `vsync`：有动静时的默认档。`platform/vsync.py` 的后台线程等主屏 vblank，每隔「分频」个 vblank 投递一拍，dt 按刷新周期计并校正到真实时长（标称周期在游戏切刷新率、可变刷新率时不准），所以画面每次刷新前进的量一样。自动帧率和固定帧率都换算成刷新率的整数分频。主线程没跟上时漏掉的 vblank 计进下一拍；vblank 停了（熄屏、驱动重置）由 250ms 的看门狗定时器兜底。
- `fast`：vblank 不可用（非 Windows、打不开适配器、反复等待失败、`SLUGCATPET_VSYNC=0`）时用精确定时器，dt 用实测时长。换屏或刷新率变了会重试 `vsync`。
- `slow`：省电且没动静时用粗定时器。
- `snow` / `rain`：省电且只有雪或雨在动时，按物理 tick / `RAIN_FPS` 出帧。

冻结按原因记在 `window._freeze`，有任何一个就不推进：

- `env`：别的程序全屏、桌宠收起时（`envwatch`），定时器整个停掉。
- `user`：定格（定格热键，`set_frozen`），画面停在这一帧。粗定时器照跑，只更新点击穿透，好让右键菜单能解除定格；左键不响应。定格前解除光标劫持、退出放置模式。
- 定格时 `step_frozen(n)` 前进 n 个 tick，画出那一 tick 的位置（不插值）；单步热键只在定格期间注册。
- 定格时收起侧边栏、状态面板、控制面板（设置项 `freeze_hide_ui`），解除时经 `envwatch.reveal` 放回，全屏收起中就等退出全屏再显示。

全局热键的 id 和动作登记在 `control/hotkey.py` 的 `GLOBAL_KEYS` / `FROZEN_KEYS`，默认组合键在 `DEFAULT_HOTKEYS`，用户改的存在 `~/.slugcatpet/hotkeys.json`。组合键必须带 Ctrl、Alt 或 Win，否则会把这个键从所有程序里抢走。键位弹窗录键时先 `suspend` 摘掉自己的热键，免得按下去直接触发；保存后 main 全摘再重挂，互换的组合键才不会撞上。

### 优先级

每个状态有一个优先级 `tier`，每条强制规则也有一个（`brain/state.py` 的 `RULE_TIER`）。
规则只能打断优先级比它低的状态；同级和更高的状态不会被打断，等它自己做完。被打断的状态先收尾（`brk`）再切换。

| 优先级 | 状态 | 能打断它们的规则 |
|---|---|---|
| 死亡、超度 100 | Dead、Ascension | 无 |
| 被砸中 90 | | 砸晕 |
| 躲杀 80 | DodgeKill | 砸晕 |
| 被拖 70 | Dragged | 砸晕、杀死弹窗 |
| 被带着 68 | Carried | 以上，加被拖 |
| 定身 65 | Hold | 以上 |
| 晕、游泳 60 | Stunned、Swimming | 以上，加被抓 |
| 取暖、躲雨、起身 40 | SeekWarmth、SeekShelter、WakeSequence | 以上，加入水、无重力 |
| 趴下、睡觉 35 | LieDown、Sleep | 以上，加寒冷、大雨 |
| 找吃的 30 | FetchFruit、Hunt | 以上，加体力告急、困到撑不住 |
| 活动 20 | 上横杆、爬墙吊顶、舔光标、扔石头、各种撒欢、让路 | 以上，加睡前要吃够、走到一半路断了 |
| 待机 0 | IdleStand、Airborne、生完气闲逛、爬架 | 以上，加让路 |

每 tick 先跑优先级高的规则；规则一旦切了状态，后面的低级规则就打不断它。没有对应强制规则的每 tick 回调最后跑，按挂载顺序。

绘制：每帧把绘制调用录一遍、得到实际画到的范围，只刷新这部分，重绘时照着回放（`rendering/display_list.py`）。
雪、雨不进录制列表，按自己的节拍定格（雪每个物理 tick、雨按 `RAIN_FPS`），两次定格之间重画都画同一帧，其余帧只刷新会动的东西。
不动的东西（杆、扎住不颤的矛）录制时标成静态内容：照常画，但不算进每帧的刷新范围；它们的签名（`ItemKind.static_sig`）变了时，新旧位置刷一次。
猫的抓点下铺一层 alpha=1 的底（`_draw_click_pads`），免得点击漏到后面的窗口；放置 / 移除模式时铺满整个窗口。
身体部件的叠放顺序（`graphics_draw.py` 的 `draw_sprites`），从后到前：远侧臂、身体、尾巴、腿、臀、近侧臂、头、脸。
部件之间有四个槽位给能力和配件用：`over_tail`（尾巴斑点，尾巴、腿、臀之上，近侧臂之下）、`under_face`（鳃）、`behind_face`（疤）、`over_face`（舌头）。
贴在尾巴上的东西用各段的位置和半径现算（见 `accessories/tail_specks` 的 `spine_at`），段数、粗细随皮肤变也贴得上。
物品各层都只画到地面线（`items/host.py` 的 `_draw_kinds`），窗口下延盖住任务栏时地面以下的部分不露出来；猫的腿同理。

### 喵

`meow/`，照 Push to Meow 的按键喵，由 `MeowService`（`window._meow`）管：

- 入口：全局热键 `meow_hover` 喵光标下的猫，没有就喵控制中的猫；控制面板的喵键（键位表 `meow`）喵控制中的猫。喵谁在按下时定下。
- 按下后 `LONG_S` 内松开是短喵，按住过了 `LONG_S` 当场长喵。全局热键只报按下，松开靠轮询 `key_down`。
- 声音由 `voice.choose` 按猫种、幼崽、被光标拎着、水下选 SoundID 和音高，经 `audio/sfx.Bank` 播放。
- 动作挂在 `pet.meow_fx`，在状态机之后、物理之前推进，所以抬头能盖过东张西望和操控。
- 素材是玩家本机 Push to Meow 的录音，启动时复制到 `assets/meow`，找不到就只做动作不出声。

### 东张西望

`brain/objectlooker.py`，待机兜底：

- 每 tick 以 `LOOK_UPDATE_PROB`（乘精力换算的好动程度）的概率重新评估兴趣，目标不再存在则不看。
- 看腻按评估次数计：超过 `LOOK_INTEREST_DECAY_UPDATES` 次后兴趣打折，更有意思的候选（光标、物体、别的猫）才会取代当前目标。
- 另有 `LOOK_NOTHING_PROB` 的概率谁也不看。

## 接口

### 能力（`abilities/base.py`）

所有钩子可选。每只猫一份实例，`self.p` 是解析后的参数。

| 钩子 | 时机 / 用途 |
|---|---|
| `attach_body(pet, body)` | 身体刚建好：写物理特性，如 `body.aquatic`；也可往 `body.throw_hooks` 登记扔出武器后的回调（重压用它实现扔矛）、往 `body.pierce_hooks` 登记武器扎中东西后的回调（骨针用它进食） |
| `build(pet)` | 建器官，返回值存到 `pet.organs[key]` |
| `draw_layers(pet)` | `[(槽位, fn(gfx, painter, atlas, ts))]` |
| `pre_step(pet)` / `post_step(pet)` | 见上面的 tick 顺序 |
| `resize(pet, floor_y)` | 世界尺寸变了 |
| `reaches(pet)` | 给规划器的够取方式（`planning.Reach` 子类实例） |
| `release(pet)` | 状态被打断、晕、死时收回器官（舌头收舌、恢复默认参数） |
| `skills`（类属性） | 技能槽 → 类，见上面「技能槽」 |
| `mount(fsm)` | 状态机建好后：挂回调、致死特效等 |
| `control(pet, body)` | 进入手动控制时挂操控器 |

### 物品（`items/base.py` 的 `ItemKind`）

常用的类属性：

| 属性 | 用途 |
|---|---|
| `key` | 放置模式名；界面文案用 `tip_<key>`、`toast_max_<key>` |
| `store` | 物体存在窗口的哪个属性上（自动建好）。`single = True` 时只有一件 |
| `affords` | 提供的用法 |
| `cap` | 上限，`None` 不限 |
| `layer` | 绘制层：`mid`（猫身后、手前）/ `front` / `top`（水之上） |
| `collides` | 参与物体间碰撞互推 |
| `grab_pad` | 设了就能用鼠标拖；抓取点按 `grab_dist()` 算（矛按整根矛身） |
| `varies` | 用法随物体状态变，配合 `affords_now()` |
| `placeable` | 侧边栏有没有放置按钮（蛋虫蛋只能从蛋虫身上掉出来） |

要写的方法：`make()`（放置时造物体）、`draw()`、`paint_icon()`（侧边栏图标）。
按需覆盖：`draw_hint()`（放置预览）、`hit()` / `draw_mark()`（移除模式的判定和高亮）、`drop()`、`on_grab()`、`on_release()`、`after_step()`、`floor_shift()`。
物体本身（模型）至少要有 `x, y, last_x, last_y, rad, state` 和 `step(WL, HL)`。

- 消解：模型带 `dissolve` 字段（-1 为没在消解）时，`kind.dissolve(win, 物)` 让它松手、不能再拿，淡出 `dissolve_ticks` 帧后移除；绘制乘 `dissolve_alpha()`。
- 程序生成（掉落物等）用 `win.spawn(key, x, y)`，不走放置模式；满了先由 `make_room()` 腾位置（蛋虫蛋消解最早那个）。

### 行为（`behaviors/base.py`）

`mount(fsm)` 里可以用：

| 接口 | 用途 |
|---|---|
| `fsm.add_state(StateSubclass)` | 注册状态，返回状态实例（钩子与声明见 `brain/state.py`） |
| `fsm.register_rule(fn(cursor), 规则)` | 每 tick 的强制规则；规则是 `RULE_TIER` 里的名字，不写就是普通每 tick 回调 |
| `fsm.interrupt(规则, 状态名)` / `fsm.blocked(规则)` | 按优先级打断当前状态 / 当前状态挡不挡这条规则 |
| `fsm.register_ticker(fn())` | 每 tick 最先调用 |
| `fsm.add_task(扩展点, fn())` | 在基础状态的扩展点挂子任务，见上面「扩展点」 |
| `fsm.intents.add(名, 效用, 开始)` | 意图：闲待到点时和别的意图按效用比例挑，见上面「需求与意图」 |
| `mood_candidate(fsm, 名, p, gate)` + `fsm.register_mood_entry(名, 状态)` | 玩的心情候选：挑中「玩」这个意图时，在这些候选里按权重抽一个进入。参数用 `mood_params(...)` 生成；`outdoor=` 给出要去的各点，大雨时有一处不该去就不选 |
| `fsm.items(用法)` | 场上提供这种用法的物品 |
| `fsm.social` | 多猫协调，见上 |
| `fsm.traits` | 性格 |
| `fsm.provide(名, fn)` / `fsm.ask(名)` | 行为之间互相查询，不直接引用对方 |
| `fsm.on_stun(fn)` | 被砸晕时收尾（松手、放掉占用） |
| `fsm.add_threat_responder(fn)` | 杀死弹窗出现时的应对 |
| `fsm.block_interaction(状态名)` | 该状态期间屏蔽光标交互 |
| `fsm._transition(状态名)` / `fsm.settle()` | 切换状态 / 动作结束回待机或空中 |

状态自己声明 `tier`（优先级）、`energy`（体力档位）、`mood`（心情名）、`hands`（要几只空手）、`needs`（False 时困意不涨）。状态机不按名字特判。

各注册表（`abilities/`、`items/`、`behaviors/`、`accessories/` 的 `__init__.py` 里的 `BUILTIN`）的顺序会影响心情抽签、规则、推进和绘制的先后。新模块加在末尾，原有的猫就不受影响。

## 怎么加东西

### 新物品

1. 建 `items/<key>/model.py`：物体的物理，见上面「物品」的最低要求。行为相近的可以直接继承已有的模型。
2. 建 `items/<key>/__init__.py`：写 `ItemKind` 子类，设 `key`、`store`、`affords`，实现 `make()`、`draw()`、`paint_icon()`；文件末尾 `KIND = 子类()`。
3. 在 `items/__init__.py` 导入它，加到 `BUILTIN` 末尾。
4. 在 `i18n.py` 加 `tip_<key>` 和 `toast_max_<key>`。

侧边栏按钮、放置、移除、拖拽、绘制都会自动带上。猫会不会用它，取决于它提供的用法有没有行为在用。

### 新的物品用法

1. 在 `items/base.py` 加一个常量，并放进 `USES`。
2. 给提供它的物品写进 `affords`，再写一个用它的行为。

### 新行为

1. 建 `behaviors/<key>/__init__.py`：写 `Behavior` 子类，设 `key`、`title`、`doc`、`requires`、`params`，实现 `mount`。状态可以放在同目录的其他文件里，每个状态按上面的表定一个 `tier`。
2. 在 `behaviors/__init__.py` 导入它，加到 `BUILTIN` 末尾。
3. 会和别的猫争同一个目标时，选目标前用 `fsm.social.free_for` 过滤，选定后用 `fsm.social.claim` 占住。
4. 要在游泳、失重这些基础状态里也做这件事时，用 `fsm.add_task` 挂到对应扩展点，不要改基础状态本身。
5. 运行 `python tools/params_doc.py` 重新生成参数文档。

### 新能力

1. 建 `abilities/<key>/`：写 `Ability` 子类，按需实现钩子；器官、绘制、够取方式放在同一个包里。
2. 在 `abilities/__init__.py` 导入它，加到 `BUILTIN` 末尾。
3. 需要核心配合时，加一个通用的钩子或身体特性（像 `body.aquatic`），不要在核心里写能力的名字。
4. 身体少了某种通用的本事（像没有嘴），在 `removes` 里去掉对应标签，依赖它的行为自动不挂；多了一种，写进 `provides`。
5. 只是换掉共用动作里的一步（像舌头钩横杆），登记到 `skills`；器官要在被打断时收回，实现 `release(pet)`。通用代码里不要出现这个能力的名字。
6. 写依赖它的行为，在某个角色 JSON 里启用，重新生成参数文档。

### 新角色

只写一个 JSON，见 [CHARACTERS.md](CHARACTERS.md)。

### 外观与皮肤

一只猫的外观是 `appearance.py` 的 `Look`，存档里的 `look` 字段：

| 字段 | 含义 |
|---|---|
| `skin` | 整套皮肤 id，空为原皮 |
| `parts` | 部位组 → 皮肤 id，覆盖整套；`""` 为原皮，`EMPTY` 为隐藏该部位 |
| `part_colors` | 部位组 → 颜色 |
| `colors` | 自定体色 / 脸色 |
| `tail` | 自定尾巴形状 `length` / `wideness` / `roundness`，空则跟随皮肤 |
| `accessories` | 配件 key → 开关与颜色 |
| `pup` | 幼崽形态 |

- 部位组：`HEAD`、`FACE`、`BODY`、`ARMS`、`HIPS`、`LEGS`、`TAIL`；配件经 `Accessory.skin_group` 对应 `FACESCAR`、`GILLS`。
- 查找：`skins.resolver_for(atlas, look, species)` 给每个部位组选来源皮肤，建出 `SkinAtlasSet`；皮肤缺该部位时回到原皮。部位的颜色和尾巴形状随它的来源皮肤。
- 换整套（`Look.set_skin`）会清掉部位覆盖、部位颜色和自定尾巴。
- 取色优先级（`base_colors`）：部位颜色 > 自定体色 / 脸色 > 皮肤默认色 > 猫种自带色。
- 皮肤列表里隐藏 Dress My Slugcat 的两个绘制底稿包，其余（含内置的 Saint Shirt）都可选。
- 幼崽形态照原版 Jolly 的幼崽：头和脸换成原版幼崽的（`CatDef.frames_for`，皮肤的头和脸不换），上身绘制点往臀部收、身体贴图压扁、尾巴每段减半，体重乘 0.7/0.75；身体间距、能力和数值不变。
- 角色写了 `pup: true` 的猫种（猫崽）固定幼崽形态，不看 `look.pup`；判断统一用 `CatDef.is_pup(look)`。

### 新配件

1. 建 `accessories/<key>/__init__.py`：写 `Accessory` 子类，设 `key`、`title`、`default_color`、`default_on`（默认戴的猫种），按需设 `color_group`（取色用的皮肤部位组）和 `skin_group`（外观面板里可单独换包的部位组），实现 `post_step` 和 `draw_layers`。
2. 在 `accessories/__init__.py` 导入它，加到 `BUILTIN` 末尾。

外观弹窗会自动列出它。配件只管外观，需要能力的部分写成能力。

### 新性格轴

1. 在 `characters/personality.py` 的 `CatPersonality` 加字段和默认值（中性值）。
2. 在 `Traits` 里换算成行为要用的量。
3. 在 `EFFECTS` 登记它作用在哪。
4. 在需要它的行为里读 `fsm.traits`。角色 JSON 的 `personality` 里就能写这个字段了。
5. 要每只猫一份、能在弹窗里调，再加进 `AXES`，并在 `i18n.py` 加 `pers_<轴>`、`pers_<轴>_lo`、`pers_<轴>_hi` 文案。

界面样式：配色在 `ui/theme/palette.py`，样式表在 `ui/theme/qss.py`。

## 测试

```
python tests/behavior_check.py check     4 个固定种子场景，逐 tick 比对状态、位置和渲染帧
python tests/behavior_check.py record    有意改了行为或画面后重录基线
python tools/params_doc.py --check       参数文档是否最新
python tools/gen_arcs.py --check         离线弧线是否最新
uv run ruff format / uv run ruff check   格式和静态检查（版本与规则在 pyproject.toml）
```

不该改变行为的改动，`check` 必须全过。有意改了手感，确认没问题后再 `record`。
`packaging/build_release.ps1 -Test` 打包前会跑行为检查和参数文档检查。

## 还没模块化的部分

- 上横杆（`HPole`）还按 `kind == "horizontal"` 认横杆；舌头猫的竖杆舌钩用 `PoleClimber`。
- 世界状态还挂在 `window.py` 的窗口上，没有单独的 World 对象。
- 超度时睁眼的脸是核心绘制直接读 `gfx.ascension`，不是绘制槽位。
- 游泳和无重力时追食物还没走占用登记；横杆位置只登记，不判断。

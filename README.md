# Telegram 群抽奖机器人

拉进群就能用的抽奖机器人。四种玩法、三种开奖条件、可配的参与门槛，开奖结果可复核。

---

## 一分钟跑起来

**Linux / macOS**（推荐用虚拟环境，Debian 12 / Ubuntu 24.04 起是强制的）：

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env          # 把 @BotFather 给的 token 填进 BOT_TOKEN
.venv/bin/python bot.py
```

或者直接 `./start.sh` —— 它会自动建好 .venv 并装依赖。

**Windows**：

```
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
copy .env.example .env
.venv\Scripts\python bot.py
```

或者双击 `start.bat`。

然后把机器人加进群，群管理员发 `/new`。

### ⚠️ 必做的一步：关掉 Group Privacy

Telegram 默认不让机器人看到群里的普通聊天消息。**口令玩法、发言积分玩法、防小号统计都依赖这个**，所以要关掉：

在 Telegram 里找 [@BotFather](https://t.me/BotFather) → `/mybots` → 选你的机器人 → **Bot Settings** → **Group Privacy** → **Turn off**。
改完把机器人踢出群再重新加一次才会生效。

只用「按钮报名」玩法的话可以不关。

### 建议给机器人的群权限

| 权限 | 用途 | 必须吗 |
|---|---|---|
| 发送消息 | 发抽奖卡片和开奖结果 | 必须 |
| 删除消息 | 清理口令报名的提示、配置过程中的临时消息 | 建议 |
| 置顶消息 | 你想手动置顶抽奖时 | 可选 |

---

## 四种玩法

| 玩法 | 怎么参与 | 适合 |
|---|---|---|
| **按钮报名** | 点抽奖卡片上的「🎉 我要参与」 | 默认玩法，最省事 |
| **口令报名** | 在群里发指定口令（比如「抽」） | 想制造点群内气氛 |
| **发言积分** | 抽奖期间正常聊天就自动参与，发言越多权重越高（有上限） | 激励活跃度 |
| **手动名单** | 管理员贴一份名单，`/pick` 直接抽 | 名单在群外，比如报名表 |

## 三种开奖条件（可叠加）

- **到时间自动开** —— 创建时设时长（5 分钟到 3 天）
- **满人数自动开** —— 到达设定人数立刻开
- **管理员手动开** —— `/end`

两个都设的话，谁先满足按谁开。

## 参与门槛（可选）

- 必须已加入指定频道 / 群（最多 5 个；**需要把机器人也加进那些频道**，否则查不到成员身份，该项会自动跳过）
- 必须设置了 Telegram 用户名
- 防小号：必须在本群「出现」满 N 小时（机器人第一次看到你发言开始算）
- 只有群管理员能发起抽奖（默认开，`/settings` 里可以放开）

---

## 命令

| 命令 | 说明 |
|---|---|
| `/new` | 打开配置面板，按按钮选玩法、名额、时长、门槛，点「🚀 发布」 |
| `/new 会员月卡 \| 3 \| 30m` | 一行创建：奖品 \| 名额 \| 时长（`30m` `2h` `1d`，`0` = 不限时） |
| `/pick 3` | 回复一条含名单的消息，从名单里抽 3 个 |
| `/list` | 本群进行中的抽奖和编号 |
| `/end [编号]` | 立即开奖（只有一个进行中时可以省略编号） |
| `/cancel [编号]` | 取消抽奖 |
| `/reroll 编号` | 重抽，排除上一轮中奖者 |
| `/verify 编号` | 公示种子、算法和排名，任何人可复核 |
| `/settings` | 群级设置：发起权限、默认必加频道 |
| `/help` | 使用说明 |

---

## 开奖公平性

开奖那一刻才生成一个随机种子并公示。每个人的分数是：

```
score = ( sha256("种子:抽奖编号:用户ID") 取前 64 位归一化到 (0,1) ) ** (1 / 权重)
```

按 score 从大到小取前 N 名。非积分玩法权重恒为 1，就是等概率均匀抽取；积分玩法按发言数加权（这是标准的 A-Res 加权抽样，权重 w 的人被抽中的概率正比于 w）。

因为种子是开奖后才公布的、且结果对种子完全确定，机器人既不能提前挑人，也不能事后改结果 —— 任何人拿 `/verify` 给出的种子和名单都能自己算一遍。测试里跑了 2 万次均匀性检验，见 `tests/test_engine.py`。

---

## 后台运行与日志

`ctl.sh` 负责启停，日志由程序自己滚动写入 `logs/bot.log`。

```bash
./ctl.sh start        # 后台启动（首次会自动建 .venv 装依赖）
./ctl.sh status       # 看是否在跑、PID、运行时长、内存、最近 5 行日志
./ctl.sh log          # 实时跟日志，Ctrl+C 退出（不影响机器人）
./ctl.sh tail 200     # 看最近 200 行
./ctl.sh restart      # 重启
./ctl.sh stop         # 停止（先 TERM，15 秒不退再 KILL）
```

几个细节：

- `start` 之后会等 3 秒确认进程真的活着；如果启动即崩（token 填错、依赖缺失），
  它会直接把最后 20 行输出打给你，而不是假装启动成功。
- 重复 `start` 不会起第二个进程 —— 同一个 token 跑两个 polling 进程，Telegram 会报 Conflict。
  脚本还会检查有没有「在跑但没 PID 文件」的野进程。
- 日志有两个文件：`logs/bot.log` 是程序日志（默认 10 MB 滚动，保留 5 份，在 `.env` 里用
  `LOG_MAX_MB` / `LOG_BACKUPS` 调），`logs/boot.log` 只兜底接住日志系统起来之前的崩溃信息。
- 想只看控制台不写文件，把 `.env` 里的 `LOG_FILE` 留空。
- `start.sh` 是前台运行版本，调试时用它能直接看到输出。

## 部署

**Windows（开机自启）**：把 `start.bat` 做个快捷方式丢进
`shell:startup`（Win+R 输入这个路径）。

**Linux（systemd）**：比 `ctl.sh` 更稳，开机自启、崩溃自动拉起都归系统管。
两者别同时用，选一个。

先在项目目录里把虚拟环境建好，然后让 systemd 用 venv 里的 python：

```ini
# /etc/systemd/system/tg-lottery.service
[Unit]
Description=Telegram 抽奖机器人
After=network-online.target

[Service]
WorkingDirectory=/opt/mrt_drawl
ExecStart=/opt/mrt_drawl/.venv/bin/python /opt/mrt_drawl/bot.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now tg-lottery
sudo journalctl -u tg-lottery -f      # 看日志
```

进程重启后，没开的定时抽奖会自动重新挂上；重启期间已经过点的会立刻补开。

---

## 目录结构

```
bot.py                    入口：读配置、初始化日志、启动 polling
ctl.sh                    启停脚本：start / stop / restart / status / log / tail
start.sh / start.bat      前台运行（调试用）
lottery/
  config.py               环境变量 -> Config
  db.py                   SQLite 表结构与全部读写
  engine.py               可验证的随机抽奖算法
  eligibility.py          管理员判断、频道成员、防小号
  service.py              发布 / 刷新卡片 / 开奖 / 重抽 / 定时调度
  keyboards.py            内联键盘
  texts.py                全部中文文案
  handlers/
    common.py             /start /help、进群问候
    create.py             /new 和配置面板交互
    participate.py        报名按钮、口令、发言积分
    manage.py             /list /end /cancel /reroll /verify /pick /settings
tests/                    单元测试（python -m unittest discover -s tests -t .）
data/lottery.db           运行时生成的数据库
logs/bot.log              程序日志（滚动）
logs/boot.log             启动阶段的兜底输出
run/bot.pid               后台进程的 PID
```

---

## 常见问题

**口令 / 积分玩法没反应？** Group Privacy 没关，见上面那一节。

**门槛设了频道但没拦住人？** 机器人不在那个频道里，查不到成员身份。这种情况下代码选择放行而不是卡住所有人 —— 把机器人加进那个频道即可。

**同一个人能报两次吗？** 不能，数据库主键就是 (抽奖, 用户)。

**机器人重启会丢抽奖吗？** 不会，全都在 SQLite 里。只有「正在填一半的配置面板」会失效，重新 `/new` 就行。

**一个群能同时开几个？** 默认 5 个，改 `.env` 里的 `MAX_ACTIVE_PER_CHAT`。

**pip 报 `externally-managed-environment`？** Debian 12 / Ubuntu 24.04 起（PEP 668）禁止往系统 Python 装包。
按上面「一分钟跑起来」用 venv 就行。如果 `python3 -m venv` 也失败，先 `sudo apt install -y python3-venv python3-full`。
`pip install --break-system-packages` 虽然能绕过去，但会污染系统 Python，不建议。

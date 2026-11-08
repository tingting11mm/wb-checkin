# WorkBuddy 每日签到（云端保底）

每天 **23:58（北京时间）** 由 GitHub Actions 自动执行一次 WorkBuddy 签到，
并把结果渲染成一页积分看板，通过 GitHub Pages 公开访问。

## 它怎么工作

1. 从 `state.enc.json` 里解出登录令牌（AES-256-GCM，口令存在仓库 Secret）
2. 查一次签到状态：
   - **今天还没签** → 领（充当本地任务失败时的保底）
   - **今天已经签过**（本地那个计划任务已经跑过了）→ 什么都不领，只记录
3. 令牌快过期时自动用 refreshToken 续期，新令牌加密后写回 `state.enc.json` 并提交
4. 生成 `dashboard.html` / `index.html` → GitHub Pages 直接访问

所以：**本地能跑就本地签，本地没跑（比如电脑一整天没开）云端补上**，两边不会重复领。

## 目录

| 文件 | 作用 |
|---|---|
| `checkin_cloud.py` | 签到主程序 |
| `template.html` | 看板模板 |
| `state.enc.json` | 加密后的登录令牌（可安全公开） |
| `data.json` | 每次运行的记录（最近 90 次） |
| `dashboard.html` / `index.html` | 生成的看板（Pages 入口） |
| `.github/workflows/checkin.yml` | 定时任务定义 |

## 需要的仓库 Secret

| 名称 | 说明 |
|---|---|
| `WB_STATE_KEY` | 解开 `state.enc.json` 的口令，**唯一需要保密的东西** |
| `WB_UID` | 账号 ID |

换机器 / 重新授权时，重跑本地 `D:\Workbuddy\wb-checkin\run_setup.bat`，
再用新的令牌重新生成 `state.enc.json` 即可。

## 手动触发

仓库 → Actions → WorkBuddy 每日签到 → Run workflow。

## 注意

- GitHub 的定时任务在高峰期可能延迟几分钟，所以同时挂了 23:45 和 23:58 两个时间点；
  两个都跑也没关系，签到接口是幂等的。
- 仓库必须公开，免费账号的 GitHub Pages 才可用。

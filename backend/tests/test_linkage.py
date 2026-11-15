"""健康报告 × 动态限速联动的可复现验证脚本（仅依赖标准库）。

运行：cd backend && .venv/bin/python -I tests/test_linkage.py

覆盖诉求里点名的 THRO-0002 / THRO-0003 / HEAL-0002 / HEAL-0003：
- 未确认恢复必须被拦截，速率决策、健康状态、错误队列三处口径同时核对；
- 状态机只允许评审→启用→关闭单向推进，已启用不能退回评审中；
- 降档/恢复原子完成，同一降档版本重复恢复只生效一次；
- 两个窗口同时恢复时只有最早满足完整条件的报告能推进；
- 过期已确认报告退回待确认；指标缺项保持当前档位；
- 书签/上传/导入三通道拦截口径一致，回读后速率决策不变且原文件版本保留。
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

# 让脚本在 backend 目录下直接跑；-I 模式下没有 site-packages，只需要 app 本身。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services import linkage  # noqa: E402
from app.services.healthcheck import HealthcheckService  # noqa: E402
from app.services.recovery import RecoveryService  # noqa: E402
from app.services.throttle import ThrottleService  # noqa: E402
from app.store import store  # noqa: E402

# 固定“当前时间”，让窗口与过期逻辑可复现。
FIXED_NOW = datetime(2026, 11, 15, 10, 0, 0)
linkage.now = lambda: FIXED_NOW  # type: ignore[assignment]

throttle = ThrottleService()
health = HealthcheckService()
recovery = RecoveryService()

failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    mark = "PASS" if condition else "FAIL"
    print(f"[{mark}] {name}" + (f" —— {detail}" if detail else ""))
    if not condition:
        failures.append(name)


def find(module: str, rule_no: str) -> dict:
    for row in store.rows(module):
        if row.get("规则编号") == rule_no or row.get("报告编号") == rule_no:
            return row
    raise KeyError(rule_no)


def decisions(rule_no: str) -> list[dict]:
    return [d for d in store.rows("rate_decision") if d["规则编号"] == rule_no]


def make_report(no: str, *, checked: str, queue: int, window_end: str, cpu_peak: int = 70) -> int:
    entry, missing = health.create_entry({
        "报告编号": no,
        "采集器名称": f"采集器-{no}",
        "CPU占用率": "50%",
        "CPU峰值": cpu_peak,
        "错误队列长度": queue,
        "采样窗口开始": "2026-11-15 08:00:00",
        "采样窗口结束": window_end,
        "检查时间戳": checked,
    })
    assert not missing, missing
    rid = entry["id"]
    _, msg = health.run_action(rid, "生成报告")
    assert "已生成报告" in msg, msg
    _, msg = health.run_action(rid, "确认报告")
    assert "已确认报告" in msg, msg
    return rid


# ----------------------------------------------------------------------
print("== 1. 初始数据核对 ==")
thro2 = find("throttle", "THRO-0002")
thro3 = find("throttle", "THRO-0003")
heal2 = find("healthcheck", "HEAL-0002")
# 服务层读取会触发惰性过期：HEAL-0003 窗口是 11-10，应退回待确认。
health.list_entries()
heal3 = find("healthcheck", "HEAL-0003")
check("THRO-0002 初始为规则评审中", thro2["status"] == "规则评审中")
check("THRO-0003 初始为已启用高速度", thro3["status"] == "已启用" and thro3["当前速率"] == 500)
check("HEAL-0002 初始为报告中（未确认）", heal2["status"] == "报告中")
check("HEAL-0003 过期后退回待确认", heal3["status"] == "待确认", heal3["status"])

print("\n== 2. 未确认健康报告时，THRO-0002 不能启用恢复到高速度 ==")
entry, msg = throttle.run_action(thro2["id"], "启用规则", {"健康报告id": heal2["id"]})
check("引用报告中(未确认)的 HEAL-0002 启用被拦截", entry is None, msg)
thro2 = find("throttle", "THRO-0002")
check("被拦截后 THRO-0002 仍停留规则评审中", thro2["status"] == "规则评审中")
check("被拦截后速率未被改写", thro2["当前速率"] == 120)
last = decisions("THRO-0002")[-1]
check("拦截动作仍写速率决策（禁止静默放行）", last["结果"] == "拦截" and last["引用健康报告"] is None)
check("HEAL-0002 状态未被联动篡改", heal2["status"] == "报告中")

print("\n== 3. 不带健康报告直接启用同样被拦截 ==")
entry, msg = throttle.run_action(thro2["id"], "启用规则", {})
check("无报告引用启用被拦截", entry is None and "已确认" in msg, msg)

print("\n== 4. 状态回退：已启用的 THRO-0003 不能改回评审中/草稿 ==")
entry, msg = throttle.run_action(thro3["id"], "提交规则评审", {})
check("已启用→规则评审中 的回退被拦截", entry is None and "单向推进" in msg, msg)
entry, msg = throttle.run_action(thro3["id"], "启用规则", {})
check("已启用状态重复启用被拦截", entry is None, msg)
thro3 = find("throttle", "THRO-0003")
check("回退拦截后 THRO-0003 仍已启用且速率不变", thro3["status"] == "已启用" and thro3["当前速率"] == 500)

print("\n== 5. 健康报告确认门槛：指标缺项保持待确认 ==")
entry, missing = health.create_entry({
    "报告编号": "HEAL-0010", "采集器名称": "缺指标节点", "CPU占用率": "30%",
})
assert not missing
hid = entry["id"]
health.run_action(hid, "生成报告")
entry, msg = health.run_action(hid, "确认报告")
check("缺采样窗口/CPU峰值/错误队列时确认被驳回为待确认",
      entry is not None and entry["status"] == "待确认" and "待确认" in msg, msg)
check("缺项报告不能用于 THRO-0002 启用",
      throttle.run_action(thro2["id"], "启用规则", {"健康报告id": hid})[0] is None)

print("\n== 6. 补齐指标并确认 HEAL-0002，THRO-0002 才能启用 ==")
heal2["错误队列长度"] = 0  # 错误队列清零才放行
heal2["检查时间戳"] = "2026-11-15 09:30:00"
entry, msg = health.run_action(heal2["id"], "确认报告")
check("HEAL-0002 指标齐备后确认成功", entry is not None and entry["status"] == "已确认", msg)
entry, msg = throttle.run_action(thro2["id"], "启用规则", {"健康报告id": heal2["id"]})
thro2 = find("throttle", "THRO-0002")
check("引用已确认 HEAL-0002 后 THRO-0002 启用并写入高速度",
      entry is not None and thro2["status"] == "已启用" and thro2["当前速率"] == 600, msg)
check("决策记录引用了报告检查时间与错误队列长度",
      decisions("THRO-0002")[-1]["报告检查时间"] == "2026-11-15 09:30:00"
      and decisions("THRO-0002")[-1]["报告错误队列长度"] == 0)

print("\n== 7. THRO-0003 降档（原子写入）与错误队列快照 ==")
entry, msg = throttle.run_action(thro3["id"], "限速降档", {"降档速率": 120, "错误队列长度": 8})
thro3 = find("throttle", "THRO-0003")
check("降档后速率写入低值且版本号 v1",
      entry is not None and thro3["当前速率"] == 120 and thro3["降档版本"] == 1, msg)
check("降档快照错误队列长度", thro3["降档时错误队列"] == 8)
check("降档标记异常", thro3["abnormal"] is True)
# 降档速率不低于当前速率时拒绝
entry, msg = throttle.run_action(thro3["id"], "限速降档", {"降档速率": 999})
check("无效降档（低速≥当前）被拦截，保持当前档位", entry is None and "保持当前档位" in msg, msg)

print("\n== 8. 错误队列未清零时恢复被拦截（异常队列不放行恢复） ==")
heal2["错误队列长度"] = 5
entry, msg = throttle.run_action(thro3["id"], "恢复速率", {"健康报告id": heal2["id"]})
thro3 = find("throttle", "THRO-0003")
check("错误队列长度>0 时恢复被拦截", entry is None and "错误队列" in msg, msg)
check("拦截后速率仍是低档 120", thro3["当前速率"] == 120)
heal2["错误队列长度"] = 0

print("\n== 9. 重复恢复只生效一次 ==")
entry, msg1 = throttle.run_action(thro3["id"], "恢复速率", {"健康报告id": heal2["id"]})
thro3 = find("throttle", "THRO-0003")
check("首次恢复成功写回高速度 500", entry is not None and thro3["当前速率"] == 500 and thro3["已恢复版本"] == 1, msg1)
entry, msg2 = throttle.run_action(thro3["id"], "恢复速率", {"健康报告id": heal2["id"]})
thro3 = find("throttle", "THRO-0003")
check("同版本重复恢复被幂等拦截", entry is None and "只生效一次" in msg2, msg2)
check("重复恢复后速率决策不变（仍 500）", thro3["当前速率"] == 500)
check("第二次提交也留了拦截决策", decisions("THRO-0003")[-1]["结果"] == "拦截")

print("\n== 10. 两个窗口同时恢复：只允许最早满足完整条件的报告推进 ==")
# 先再降一档产生 v2
throttle.run_action(thro3["id"], "限速降档", {"降档速率": 90, "错误队列长度": 3})
rid_early = make_report("HEAL-0020", checked="2026-11-15 09:10:00", queue=0,
                        window_end="2026-11-15 23:59:59")
rid_late = make_report("HEAL-0021", checked="2026-11-15 09:40:00", queue=0,
                       window_end="2026-11-15 23:59:59")
early = find("healthcheck", "HEAL-0020")
late = find("healthcheck", "HEAL-0021")
check("两份报告均已确认并保留（窗口冲突留双份审计）",
      early["status"] == "已确认" and late["status"] == "已确认")
entry, msg = throttle.run_action(thro3["id"], "恢复速率", {"健康报告id": late["id"]})
check("引用较晚报告被仲裁拦截", entry is None and "最早满足完整条件" in msg, msg)
entry, msg = throttle.run_action(thro3["id"], "恢复速率", {"健康报告id": early["id"]})
thro3 = find("throttle", "THRO-0003")
check("引用最早完整报告恢复成功", entry is not None and thro3["当前速率"] == 500, msg)
check("两份冲突报告都仍在库中未被覆盖",
      store.find("healthcheck", rid_early) is not None and store.find("healthcheck", rid_late) is not None)

print("\n== 11. 健康报告过期：已确认退回待确认，恢复需引用当前窗口 ==")
# HEAL-0020 的窗口改到过去，触发惰性过期
early["采样窗口结束"] = "2026-11-14 23:59:59"
throttle.run_action(thro3["id"], "限速降档", {"降档速率": 80, "错误队列长度": 1})
thro3 = find("throttle", "THRO-0003")
entry, msg = throttle.run_action(thro3["id"], "恢复速率", {"健康报告id": early["id"]})
check("过期报告在恢复时被识别并退回待确认/拦截",
      entry is None and ("不覆盖当前时间" in msg or "未确认" in msg), msg)
check("过期报告状态为待确认", find("healthcheck", "HEAL-0020")["status"] == "待确认")
check("过期拦截后 THRO-0003 保持低档 80", thro3["当前速率"] == 80 and thro3["abnormal"] is True)

print("\n== 12. 关闭规则单向收尾，关闭后拒绝一切动作 ==")
entry, msg = throttle.run_action(thro2["id"], "关闭规则", {})
thro2 = find("throttle", "THRO-0002")
check("THRO-0002 可从已启用关闭", entry is not None and thro2["status"] == "已关闭", msg)
entry, msg = throttle.run_action(thro2["id"], "启用规则", {"健康报告id": heal2["id"]})
check("已关闭规则不能再启用", entry is None and "已关闭" in msg, msg)

print("\n== 13. 书签/上传/导入三通道：未确认场景拦截口径一致 ==")
# 新建一条评审中规则，尝试用三种通道跳过评审直接恢复/启用语义（恢复需要降档版本）
created, missing = throttle.create_entry({
    "规则编号": "THRO-0098", "目标域组": "通道拦截验证组", "初始速率": 40, "最大速率": 280,
})
assert not missing
probe_id = created["id"]
payload = json.dumps({"规则编号": "THRO-0098", "健康报告编号": "HEAL-0002"}, ensure_ascii=False).encode("utf-8")
a1, m1 = recovery.submit(channel="bookmark", raw=payload)
a2, m2 = recovery.submit(channel="upload", raw=payload, filename="recover.json")
a3, m3 = recovery.submit(channel="import", raw=payload)
check("三通道对无降档版本的恢复均拦截",
      a1 and a2 and a3 and a1["处理结果"] == "拦截" and a2["处理结果"] == "拦截" and a3["处理结果"] == "拦截",
      f"{m1} | {m2} | {m3}")
check("三通道均留存原始凭证版本",
      len(store.rows("recovery_artifact")) >= 3
      and all(r["原始内容"] == payload.decode("utf-8") for r in store.rows("recovery_artifact"))
      and {r["通道"] for r in store.rows("recovery_artifact")} == {"bookmark", "upload", "import"})
probe = find("throttle", "THRO-0098")
check("通道拦截后规则状态/速率不变", probe["status"] == "草稿" and probe["当前速率"] == 40)

print("\n== 14. 凭证回读：速率决策不变，原文件版本保留 ==")
# 构造一条合法恢复链路：评审→启用→降档，再用书签恢复
created, missing = throttle.create_entry({
    "规则编号": "THRO-0099", "目标域组": "通道验证组", "初始速率": 50, "最大速率": 300,
})
assert not missing
new_id = created["id"]
throttle.run_action(new_id, "提交规则评审", {})
throttle.run_action(new_id, "启用规则", {"健康报告id": heal2["id"]})
throttle.run_action(new_id, "限速降档", {"降档速率": 60, "错误队列长度": 0})
valid_payload = json.dumps({"规则编号": "THRO-0099", "健康报告编号": "HEAL-0002"},
                           ensure_ascii=False).encode("utf-8")
art, m = recovery.submit(channel="bookmark", raw=valid_payload)
rule99 = find("throttle", "THRO-0099")
check("书签通道恢复成功并写高速度", art is not None and art["处理结果"] == "成功" and rule99["当前速率"] == 300, m)
rate_after_first = rule99["当前速率"]
# 同通道同字节重放，命中哈希去重，速率决策不变
art_same, m_same = recovery.submit(channel="bookmark", raw=valid_payload)
rule99 = find("throttle", "THRO-0099")
check("同通道同内容重复提交回读不改变速率决策", art_same is not None and art_same["id"] == art["id"]
      and rule99["当前速率"] == rate_after_first, m_same)
# 同字节文件换上传通道：作为独立文件版本留存，但恢复被幂等拦截，速率同样不变
art2, m2 = recovery.submit(channel="upload", raw=valid_payload, filename="same-bytes.json")
rule99 = find("throttle", "THRO-0099")
check("跨通道同内容保留独立文件版本且速率决策不变",
      art2 is not None and art2["id"] != art["id"] and art2["处理结果"] == "拦截"
      and rule99["当前速率"] == rate_after_first, m2)
replayed, m3 = recovery.replay(art["id"])
check("凭证回读重放返回同一版本且速率不变",
      replayed is not None and replayed["id"] == art["id"] and "保持" in m3, m3)
check("原始文件版本与哈希完整保留",
      art["原始内容"] == valid_payload.decode("utf-8") and len(art["内容哈希"]) == 64)

print("\n== 15. 三端口径总核对：速率决策 / 采集器健康状态 / 错误队列 ==")
thro3 = find("throttle", "THRO-0003")
# THRO-0003 最后一次恢复被过期拦截，应仍处于低档
check("THRO-0003 过期拦截后保持低档", thro3["当前速率"] == 80 and thro3["abnormal"] is True)
check("HEAL-0020 已退回待确认（健康状态口径）", find("healthcheck", "HEAL-0020")["status"] == "待确认")
blocked = [d for d in decisions("THRO-0003") if d["结果"] == "拦截"]
check("THRO-0003 每次拦截都有决策留痕", len(blocked) >= 3, f"拦截决策 {len(blocked)} 条")
# 错误队列长度为数值且被记录进最近的成功恢复决策
ok_decisions = [d for d in store.rows("rate_decision") if d["结果"] == "成功" and d["动作"] == "恢复速率"]
check("成功恢复决策记录了检查时间与错误队列长度",
      all(d["报告检查时间"] and d["报告错误队列长度"] == 0 for d in ok_decisions))
check("所有成功速率写入都有对应决策（无只改速率不记决策）",
      len(ok_decisions) >= 2 and all(d["变更前速率"] != d["变更后速率"] for d in ok_decisions))

print("\n== 16. 并发恢复：多线程同时提交，原子性保证只有最早完整报告推进一次 ==")
import threading  # noqa: E402

created, missing = throttle.create_entry({
    "规则编号": "THRO-0088", "目标域组": "并发验证组", "初始速率": 60, "最大速率": 400,
})
assert not missing
race_id = created["id"]
throttle.run_action(race_id, "提交规则评审", {})
throttle.run_action(race_id, "启用规则", {"健康报告id": heal2["id"]})
throttle.run_action(race_id, "限速降档", {"降档速率": 70, "错误队列长度": 0})
r_early = make_report("HEAL-0030", checked="2026-11-15 09:05:00", queue=0,
                      window_end="2026-11-15 23:59:59")
r_late = make_report("HEAL-0031", checked="2026-11-15 09:55:00", queue=0,
                     window_end="2026-11-15 23:59:59")
results: list[tuple[str, bool, str]] = []
results_lock = threading.Lock()


def race(report_id: int, tag: str) -> None:
    ok_entry, ok_msg = throttle.run_action(race_id, "恢复速率", {"健康报告id": report_id})
    with results_lock:
        results.append((tag, ok_entry is not None, ok_msg))


threads = []
for i in range(6):
    threads.append(threading.Thread(target=race, args=(r_early, f"early-{i}")))
    threads.append(threading.Thread(target=race, args=(r_late, f"late-{i}")))
for t in threads:
    t.start()
for t in threads:
    t.join()

successes = [r for r in results if r[1]]
race_rule = find("throttle", "THRO-0088")
check("12 个并发恢复只有 1 个成功", len(successes) == 1, f"成功数={len(successes)}")
check("唯一成功的恢复引用的是最早完整报告", successes and successes[0][0].startswith("early-"))
check("恢复后速率为高速度且版本只推进一次", race_rule["当前速率"] == 400 and race_rule["已恢复版本"] == 1)
race_ok = [d for d in decisions("THRO-0088") if d["结果"] == "成功" and d["动作"] == "恢复速率"]
check("该降档版本只产生一条成功速率决策", len(race_ok) == 1 and race_ok[0]["引用健康报告"] == "HEAL-0030")
check("两份窗口冲突报告均保留",
      store.find("healthcheck", r_early) is not None and store.find("healthcheck", r_late) is not None)

print("\n" + "=" * 60)
if failures:
    print(f"存在 {len(failures)} 个失败项：")
    for name in failures:
        print(f"  - {name}")
    sys.exit(1)
print("全部联动校验通过")

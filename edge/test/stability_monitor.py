"""
edge/test/stability_monitor.py

4小时稳定性测试监控脚本 + 数据库定期清理。
验收标准：系统连续运行 4 小时无崩溃。

功能：
  1. 每 60 秒探测一次 LLM 服务是否在线
  2. 每 30 分钟执行一次数据库清理（保留最近 7 天）
  3. 记录异常次数，4 小时结束后输出稳定性报告
  4. 实时写入 /tmp/stability_report.json

运行方式：
  # 后台运行（同时运行 main.py）
  nohup python3 edge/test/stability_monitor.py > /tmp/stability.log 2>&1 &
  echo $! > /tmp/stability_monitor.pid

  # 查看实时日志
  tail -f /tmp/stability.log

  # 查看报告
  cat /tmp/stability_report.json
"""

import os
import sys
import time
import json
import sqlite3
import logging
import threading
import signal
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [stability] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("stability")

DB_PATH         = "/home/lubancat/smart_home/data/smart_home.db"
REPORT_PATH     = "/tmp/stability_report.json"
LLM_URL         = "http://localhost:8080"
TEST_DURATION   = 4 * 3600        # 4 小时
CHECK_INTERVAL  = 60              # 每 60 秒检查一次
DB_CLEAN_INTERVAL = 30 * 60       # 每 30 分钟清理一次数据库
DB_KEEP_DAYS    = 7               # 保留最近 7 天数据
DB_MAX_ROWS     = 100_000         # sensor_log 最大行数（超过则强制清理旧数据）


# ═══════════════════════════════════════════════════════════════
# 数据库维护
# ═══════════════════════════════════════════════════════════════

class DBMaintainer:
    """
    数据库容量管理。

    现有接口：Database.cleanup_old(keep_days) 已在 database.py 中定义。
    本类直接操作 SQLite，不依赖 Database 对象，适合独立脚本调用。
    """

    def __init__(self, db_path: str):
        self._path = db_path

    def get_stats(self) -> dict:
        """查询各表记录数和数据库文件大小"""
        if not os.path.exists(self._path):
            return {"exists": False}

        size_mb = os.path.getsize(self._path) / 1024 / 1024
        conn    = sqlite3.connect(self._path)
        stats   = {"exists": True, "size_mb": round(size_mb, 2)}

        for table in ("sensor_log", "event_log", "decision_log", "cam_log"):
            try:
                count = conn.execute(
                    f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                stats[table] = count
            except sqlite3.OperationalError:
                stats[table] = 0   # 表不存在

        conn.close()
        return stats

    def cleanup_by_age(self, keep_days: int = DB_KEEP_DAYS) -> dict:
        """
        删除超过 keep_days 天的历史数据。
        对应 database.py 中的 Database.cleanup_old(keep_days)。
        """
        if not os.path.exists(self._path):
            return {"skipped": "db not found"}

        cutoff  = time.time() - keep_days * 86400
        conn    = sqlite3.connect(self._path)
        deleted = {}

        for table in ("sensor_log", "event_log", "decision_log", "cam_log"):
            try:
                cursor = conn.execute(
                    f"DELETE FROM {table} WHERE timestamp < ?", (cutoff,))
                deleted[table] = cursor.rowcount
            except sqlite3.OperationalError:
                deleted[table] = 0

        conn.commit()

        # SQLite VACUUM 回收磁盘空间
        conn.execute("VACUUM")
        conn.commit()
        conn.close()

        log.info("DB cleanup (keep %dd): deleted %s", keep_days, deleted)
        return deleted

    def cleanup_by_count(self, table: str = "sensor_log",
                         max_rows: int = DB_MAX_ROWS) -> int:
        """
        如果 table 超过 max_rows 行，删除最旧的超出部分。
        作为 cleanup_by_age 的补充，防止短时间内写入量过大。
        """
        if not os.path.exists(self._path):
            return 0

        conn  = sqlite3.connect(self._path)
        count = conn.execute(
            f"SELECT COUNT(*) FROM {table}").fetchone()[0]

        if count <= max_rows:
            conn.close()
            return 0

        # 删除最旧的 (count - max_rows) 条
        to_delete = count - max_rows
        conn.execute(f"""
            DELETE FROM {table}
            WHERE id IN (
                SELECT id FROM {table}
                ORDER BY timestamp ASC
                LIMIT {to_delete}
            )
        """)
        conn.commit()
        conn.close()
        log.info("DB count cleanup: deleted %d rows from %s", to_delete, table)
        return to_delete

    def full_maintenance(self) -> dict:
        """
        完整维护流程：
          1. 按时间清理超过 keep_days 的旧数据
          2. 按行数限制防止短期爆炸
        返回清理摘要。
        """
        result = {}
        result["by_age"]  = self.cleanup_by_age(keep_days=DB_KEEP_DAYS)
        result["by_count"] = self.cleanup_by_count(
            table="sensor_log", max_rows=DB_MAX_ROWS)
        result["stats_after"] = self.get_stats()
        return result


# ═══════════════════════════════════════════════════════════════
# LLM 探针
# ═══════════════════════════════════════════════════════════════

def check_llm(url: str) -> bool:
    try:
        import requests
        resp = requests.post(
            f"{url}/rkllm_chat",
            json={"messages": [{"role": "user", "content": "ping"}],
                  "stream": False},
            timeout=10,
        )
        return resp.status_code == 200
    except Exception:
        return False


def check_mqtt() -> bool:
    try:
        import paho.mqtt.client as mqtt
        result = {"connected": False}
        ev = threading.Event()

        def on_connect(c, u, f, rc):
            result["connected"] = (rc == 0)
            ev.set()
            c.disconnect()

        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1, "stability_probe")
        c.on_connect = on_connect
        c.connect_async("localhost", 1883)
        c.loop_start()
        ev.wait(timeout=3)
        c.loop_stop()
        return result["connected"]
    except Exception:
        return False


# ═══════════════════════════════════════════════════════════════
# 稳定性监控主循环
# ═══════════════════════════════════════════════════════════════

class StabilityMonitor:

    def __init__(self, duration_sec: int = TEST_DURATION):
        self._duration   = duration_sec
        self._start_time = None
        self._running    = False
        self._db         = DBMaintainer(DB_PATH)

        self._report = {
            "start_time":      "",
            "end_time":        "",
            "duration_plan":   f"{duration_sec // 3600}h",
            "checks_total":    0,
            "llm_failures":    0,
            "mqtt_failures":   0,
            "db_cleanups":     0,
            "db_stats_final":  {},
            "passed":          False,
            "log":             [],
        }

    def _log_event(self, level: str, msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        entry = f"[{ts}] {level}: {msg}"
        self._report["log"].append(entry)
        if level == "ERROR":
            log.error(msg)
        else:
            log.info(msg)
        # 实时写报告
        self._save_report()

    def _save_report(self):
        try:
            with open(REPORT_PATH, "w", encoding="utf-8") as f:
                json.dump(self._report, f, ensure_ascii=False, indent=2)
        except Exception as e:
            log.warning("保存报告失败: %s", e)

    def run(self):
        self._start_time = time.time()
        self._running    = True
        self._report["start_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        log.info("="*56)
        log.info("稳定性测试开始  计划时长: %s", self._report["duration_plan"])
        log.info("数据库: %s", DB_PATH)
        log.info("="*56)

        # 注册信号处理（Ctrl+C 或 kill 时输出最终报告）
        def _signal_handler(sig, frame):
            log.info("收到终止信号，提前结束测试")
            self._finish()
            sys.exit(0)
        signal.signal(signal.SIGINT,  _signal_handler)
        signal.signal(signal.SIGTERM, _signal_handler)

        last_db_clean = time.time()
        check_count   = 0

        while self._running:
            elapsed = time.time() - self._start_time
            if elapsed >= self._duration:
                break

            remaining = self._duration - elapsed
            log.info("─ 检查 #%d  已运行 %.1fh  剩余 %.1fh",
                     check_count + 1,
                     elapsed / 3600,
                     remaining / 3600)

            # ── LLM 探针 ──────────────────────────────────
            llm_ok = check_llm(LLM_URL)
            if llm_ok:
                self._log_event("INFO", "LLM 服务在线 ✅")
            else:
                self._report["llm_failures"] += 1
                self._log_event("ERROR",
                    f"LLM 服务离线！累计失败: {self._report['llm_failures']}")

            # ── MQTT 探针 ─────────────────────────────────
            mqtt_ok = check_mqtt()
            if mqtt_ok:
                self._log_event("INFO", "MQTT broker 在线 ✅")
            else:
                self._report["mqtt_failures"] += 1
                self._log_event("ERROR",
                    f"MQTT broker 离线！累计失败: {self._report['mqtt_failures']}")

            # ── 数据库统计 ────────────────────────────────
            stats = self._db.get_stats()
            if stats.get("exists"):
                self._log_event("INFO",
                    f"DB: sensor_log={stats.get('sensor_log',0)}行  "
                    f"event_log={stats.get('event_log',0)}行  "
                    f"大小={stats.get('size_mb',0):.1f}MB")

            # ── 定期数据库清理 ────────────────────────────
            if time.time() - last_db_clean >= DB_CLEAN_INTERVAL:
                self._log_event("INFO", "执行数据库定期清理...")
                result = self._db.full_maintenance()
                self._report["db_cleanups"] += 1
                self._log_event("INFO",
                    f"清理完成: {result['by_age']}  "
                    f"按行数删除: {result['by_count']}行  "
                    f"清理后大小: {result['stats_after'].get('size_mb',0):.1f}MB")
                last_db_clean = time.time()

            check_count += 1
            self._report["checks_total"] = check_count
            self._save_report()

            # 等待下次检查
            for _ in range(CHECK_INTERVAL):
                if not self._running:
                    break
                time.sleep(1)

        self._finish()

    def _finish(self):
        elapsed = time.time() - (self._start_time or time.time())
        self._report["end_time"]     = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self._report["db_stats_final"] = self._db.get_stats()

        # 判定通过标准：无 LLM 失败且无 MQTT 失败
        llm_fail  = self._report["llm_failures"]
        mqtt_fail = self._report["mqtt_failures"]
        self._report["passed"] = (llm_fail == 0 and mqtt_fail == 0)

        self._save_report()

        log.info("="*56)
        log.info("稳定性测试结束")
        log.info("实际运行时长: %.2fh", elapsed / 3600)
        log.info("检查次数: %d", self._report["checks_total"])
        log.info("LLM 失败次数: %d", llm_fail)
        log.info("MQTT 失败次数: %d", mqtt_fail)
        log.info("数据库清理次数: %d", self._report["db_cleanups"])
        final = self._report["db_stats_final"]
        log.info("数据库最终大小: %.1fMB", final.get("size_mb", 0))
        verdict = "✅ PASS" if self._report["passed"] else "❌ FAIL"
        log.info("最终结论: %s", verdict)
        log.info("报告已保存: %s", REPORT_PATH)
        log.info("="*56)


# ═══════════════════════════════════════════════════════════════
# 手动数据库维护命令行工具
# ═══════════════════════════════════════════════════════════════

def cli_db_maintenance():
    """
    独立的数据库维护命令，不启动监控循环。
    用法：python3 stability_monitor.py --db-clean
    """
    db = DBMaintainer(DB_PATH)

    print("\n数据库维护工具")
    print("="*56)
    print(f"数据库路径: {DB_PATH}")

    stats_before = db.get_stats()
    if not stats_before.get("exists"):
        print("数据库文件不存在")
        return

    print(f"\n清理前：")
    print(f"  sensor_log : {stats_before.get('sensor_log', 0)} 行")
    print(f"  event_log  : {stats_before.get('event_log',  0)} 行")
    print(f"  cam_log    : {stats_before.get('cam_log',    0)} 行")
    print(f"  文件大小   : {stats_before.get('size_mb', 0):.2f} MB")

    result = db.full_maintenance()

    stats_after = result["stats_after"]
    print(f"\n清理后：")
    print(f"  sensor_log : {stats_after.get('sensor_log', 0)} 行")
    print(f"  event_log  : {stats_after.get('event_log',  0)} 行")
    print(f"  文件大小   : {stats_after.get('size_mb', 0):.2f} MB")
    print(f"\n按时间删除: {result['by_age']}")
    print(f"按行数删除: {result['by_count']} 行")
    print("\n✅ 数据库维护完成")


# ═══════════════════════════════════════════════════════════════
# 入口
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="稳定性监控 + 数据库维护")
    parser.add_argument("--db-clean",  action="store_true",
                        help="仅执行数据库维护，不启动监控")
    parser.add_argument("--db-stats",  action="store_true",
                        help="仅查询数据库状态")
    parser.add_argument("--duration",  type=int, default=TEST_DURATION,
                        help=f"测试时长（秒，默认{TEST_DURATION}）")
    args = parser.parse_args()

    if args.db_stats:
        db = DBMaintainer(DB_PATH)
        stats = db.get_stats()
        print(json.dumps(stats, indent=2, ensure_ascii=False))
    elif args.db_clean:
        cli_db_maintenance()
    else:
        monitor = StabilityMonitor(duration_sec=args.duration)
        monitor.run()
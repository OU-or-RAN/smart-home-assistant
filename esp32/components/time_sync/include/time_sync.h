#ifndef TIME_SYNC_H
#define TIME_SYNC_H

#include <stdbool.h>
#include <time.h>

// ==================== SNTP 配置 ====================
#define SNTP_SERVER     "pool.ntp.org"
#define TIMEZONE        "CST-8"

// ==================== 初始化与同步 ====================

/**
 * @brief 初始化 SNTP 并阻塞等待时间同步完成
 *        同步成功后自动设置时区为 CST-8
 *        失败则打印警告并继续运行
 */
void time_sync_obtain(void);

// ==================== 时间获取 ====================

/**
 * @brief 获取当前 Unix 时间戳
 * @return time_t 当前秒级时间戳
 */
time_t time_sync_get_timestamp(void);

/**
 * @brief 获取时间同步状态
 * @return true  已完成同步
 * @return false 尚未同步或同步失败
 */
bool time_sync_is_synced(void);

#endif // TIME_SYNC_H
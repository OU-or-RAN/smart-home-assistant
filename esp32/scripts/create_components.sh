#!/bin/bash

# ============================================================
# ESP32-S3 Smart Home - Components 目录结构创建脚本
# ============================================================

set -e

BASE_DIR="components"

# 颜色输出
GREEN='\033[0;32m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

echo -e "${CYAN}Creating ESP32-S3 components directory structure...${NC}"
echo ""

# ============================================================
# 定义所有组件
# ============================================================

COMPONENTS=(
    "pin_config"
    "led_control"
    "sensor_dht11"
    "sensor_mq4"
    "sensor_yl38"
    "mqtt_handler"
    "json_builder"
    "time_sync"
)

# ============================================================
# 创建目录结构 & 占位文件
# ============================================================

for COMP in "${COMPONENTS[@]}"; do
    COMP_DIR="${BASE_DIR}/${COMP}"
    INCLUDE_DIR="${COMP_DIR}/include"

    mkdir -p "${INCLUDE_DIR}"

    # pin_config 只有头文件，无 .c 文件
    if [ "${COMP}" == "pin_config" ]; then
        touch "${INCLUDE_DIR}/pin_config.h"
    else
        touch "${COMP_DIR}/${COMP}.c"
        touch "${INCLUDE_DIR}/${COMP}.h"
    fi

    # 创建 CMakeLists.txt
    if [ "${COMP}" == "pin_config" ]; then
        # 纯头文件组件，INTERFACE 库
        cat > "${COMP_DIR}/CMakeLists.txt" <<EOF
idf_component_register(
    INCLUDE_DIRS "include"
)
EOF
    else
        cat > "${COMP_DIR}/CMakeLists.txt" <<EOF
idf_component_register(
    SRCS "${COMP}.c"
    INCLUDE_DIRS "include"
    REQUIRES ""
)
EOF
    fi

    echo -e "  ${GREEN}[OK]${NC} ${COMP_DIR}"
done

# ============================================================
# 完成提示
# ============================================================

echo ""
echo -e "${CYAN}Done! Directory structure:${NC}"
echo ""

find "${BASE_DIR}" | sort | sed 's|[^/]*/|  |g'

echo ""
echo -e "${GREEN}All components created successfully.${NC}"
echo "Next step: fill in REQUIRES in each CMakeLists.txt based on dependencies."

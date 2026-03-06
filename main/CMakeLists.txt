file(GLOB SOURCES "*.c")

idf_component_register(SRCS ${SOURCES}
                    INCLUDE_DIRS "."
                    REQUIRES 
                        log
                        time_sync
                        mqtt
                        json
                        nvs_flash
                        esp_netif
                        esp_wifi
                        protocol_examples_common
                        driver
                        esp_driver_rmt
                        led_strip
                        esp_timer
                        esp_hw_support    # 添加 CPU 支持
                        esp_adc  # 新增 ADC 校准支持
                        led_control
                        mqtt_handler
)
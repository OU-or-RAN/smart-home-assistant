idf_component_register(SRCS "app_main.c"
                    INCLUDE_DIRS "."
                    REQUIRES 
                        mqtt
                        json                    # 不需要 espressif/ 前缀
                        nvs_flash               # 内置组件
                        esp_netif               # 内置组件
                        esp_wifi                # 内置组件
                        protocol_examples_common
                        driver                  # 使用 driver 而不是 esp_driver_gpio
                        esp_driver_rmt          # RMT 驱动
                        led_strip               # LED 灯带库
                        esp_timer          
)
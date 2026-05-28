idf_component_register(
    SRCS "app_main.c"
    INCLUDE_DIRS "."
    REQUIRES
        led_control
        time_sync
        mqtt_handler
        nvs_flash
        esp_netif
        esp_event
        protocol_examples_common
        log
        esp_wifi
)
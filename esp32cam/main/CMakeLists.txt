idf_component_register(
    SRCS
        "app_main.c"
        "camera_init.c"
        "http_stream.c"
        "mqtt_cam.c"
    INCLUDE_DIRS "."
    REQUIRES
        esp32-camera
        esp_http_server
        mqtt
        json
        esp_netif
        esp_wifi
        nvs_flash
        esp_event
        log
)
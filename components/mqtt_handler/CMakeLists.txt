idf_component_register(
    SRCS "mqtt_handler.c"
    INCLUDE_DIRS "include"
    REQUIRES
        json_builder
        led_control
        sensor_dht11
        sensor_mq2
        sensor_mq4
        sensor_yl38
        sensor_sht40
        adc_shared
        esp_adc
        mqtt
        json
        esp_timer
        freertos
        log
    LDFRAGMENTS
        ""
)

target_link_libraries(__idf_mqtt_handler INTERFACE m)

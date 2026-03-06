idf_component_register(
    SRCS "mqtt_handler.c"
    INCLUDE_DIRS "include"
    REQUIRES
        json_builder
        led_control
        sensor_mq4
        sensor_dht11
        sensor_yl38
        mqtt
        json
        time_sync
)
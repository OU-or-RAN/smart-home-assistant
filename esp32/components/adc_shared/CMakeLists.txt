idf_component_register(
    SRCS "adc_shared.c"
    INCLUDE_DIRS "include"
    REQUIRES 
        esp_adc  # 新增 ADC 校准支持
)

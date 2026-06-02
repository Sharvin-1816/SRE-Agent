"""
services/definitions.py

All 6 mock service instances with their realistic baseline characteristics.
Imported by service_runner.py to spin them all up.
"""

from services.base_service import BaseService


payment_service = BaseService(
    name     = "payment_service",
    base_rt  = 410,
    base_er  = 0.8,
    base_tp  = 155,
    base_cpu = 52,
    base_mem = 61,
    rt_noise = 30,
    er_noise = 0.4,
    tp_noise = 18,
)

cart_service = BaseService(
    name     = "cart_service",
    base_rt  = 280,
    base_er  = 0.5,
    base_tp  = 210,
    base_cpu = 38,
    base_mem = 44,
    rt_noise = 22,
    er_noise = 0.3,
    tp_noise = 25,
)

notification_service = BaseService(
    name     = "notification_service",
    base_rt  = 190,
    base_er  = 1.1,
    base_tp  = 320,
    base_cpu = 29,
    base_mem = 35,
    rt_noise = 18,
    er_noise = 0.6,
    tp_noise = 40,
)

auth_service = BaseService(
    name     = "auth_service",
    base_rt  = 95,
    base_er  = 0.2,
    base_tp  = 480,
    base_cpu = 22,
    base_mem = 28,
    rt_noise = 10,
    er_noise = 0.15,
    tp_noise = 35,
)

inventory_service = BaseService(
    name     = "inventory_service",
    base_rt  = 620,
    base_er  = 1.5,
    base_tp  = 88,
    base_cpu = 65,
    base_mem = 72,
    rt_noise = 55,
    er_noise = 0.8,
    tp_noise = 12,
)

gateway_service = BaseService(
    name     = "gateway_service",
    base_rt  = 45,
    base_er  = 0.1,
    base_tp  = 890,
    base_cpu = 18,
    base_mem = 22,
    rt_noise = 8,
    er_noise = 0.08,
    tp_noise = 60,
)

ALL_SERVICES = {
    "payment_service":      (payment_service,      3001),
    "cart_service":         (cart_service,          3002),
    "notification_service": (notification_service,  3003),
    "auth_service":         (auth_service,          3004),
    "inventory_service":    (inventory_service,     3005),
    "gateway_service":      (gateway_service,       3006),
}

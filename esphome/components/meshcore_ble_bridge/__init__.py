import esphome.codegen as cg
from esphome.components import ble_client
import esphome.config_validation as cv
from esphome.const import CONF_ID, CONF_PORT

DEPENDENCIES = ["ble_client"]

CONF_TCP_NO_DELAY = "tcp_no_delay"
CONF_FORCE_ENCRYPTION = "force_encryption"
CONF_WAIT_FOR_AUTH = "wait_for_auth"
CONF_WRITE_WITH_RESPONSE = "write_with_response"

meshcore_ble_bridge_ns = cg.esphome_ns.namespace("meshcore_ble_bridge")
MeshCoreBLEBridge = meshcore_ble_bridge_ns.class_(
    "MeshCoreBLEBridge", cg.Component, ble_client.BLEClientNode
)

CONFIG_SCHEMA = (
    cv.Schema(
        {
            cv.GenerateID(): cv.declare_id(MeshCoreBLEBridge),
            cv.Optional(CONF_PORT, default=5000): cv.port,
            cv.Optional(CONF_TCP_NO_DELAY, default=True): cv.boolean,
            cv.Optional(CONF_FORCE_ENCRYPTION, default=True): cv.boolean,
            cv.Optional(CONF_WAIT_FOR_AUTH, default=True): cv.boolean,
            cv.Optional(CONF_WRITE_WITH_RESPONSE, default=True): cv.boolean,
        }
    )
    .extend(cv.COMPONENT_SCHEMA)
    .extend(ble_client.BLE_CLIENT_SCHEMA)
)


async def to_code(config):
    var = cg.new_Pvariable(config[CONF_ID])
    cg.add(var.set_port(config[CONF_PORT]))
    cg.add(var.set_tcp_no_delay(config[CONF_TCP_NO_DELAY]))
    cg.add(var.set_force_encryption(config[CONF_FORCE_ENCRYPTION]))
    cg.add(var.set_wait_for_auth(config[CONF_WAIT_FOR_AUTH]))
    cg.add(var.set_write_with_response(config[CONF_WRITE_WITH_RESPONSE]))
    await ble_client.register_ble_node(var, config)
    await cg.register_component(var, config)

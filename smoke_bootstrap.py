from snowflake_emulator.testing import running_server
import snowflake.connector
from calculation_api.bootstrap import seed
from calculation_api.gateway import SnowflakeGateway

with running_server() as options:
    with snowflake.connector.connect(**options) as connection:
        seed(connection)
        bindings, objective = SnowflakeGateway(connection).load("insurance-v1")
        print("binding_groups", len(bindings), "objective", objective)

# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.

import asyncio
import logging
import typing

import pytest
import tenacity
from pytest_operator.plugin import OpsTest

from . import juju_
from .helpers import (
    MYSQL_DEFAULT_APP_NAME,
    MYSQL_ROUTER_DEFAULT_APP_NAME,
    execute_queries_against_unit,
    get_tls_certificate_issuer,
)

logger = logging.getLogger(__name__)

MYSQL_APP_NAME = MYSQL_DEFAULT_APP_NAME
MYSQL_ROUTER_APP_NAME = MYSQL_ROUTER_DEFAULT_APP_NAME
DATA_INTEGRATOR_APP_NAME = "data-integrator"
SLOW_TIMEOUT = 15 * 60
RETRY_TIMEOUT = 60
TEST_DATABASE = "testdatabase"
TEST_TABLE = "testtable"

if juju_.is_3_or_higher:
    TLS_APP_NAME = "self-signed-certificates"
    TLS_CONFIG = {"ca-common-name": "Test CA"}
else:
    TLS_APP_NAME = "tls-certificates-operator"
    TLS_CONFIG = {"generate-self-signed-certificates": "true", "ca-common-name": "Test CA"}


async def get_data_integrator_credentials(ops_test: OpsTest) -> typing.Dict:
    """Helper to get the credentials from the deployed data integrator"""
    data_integrator_unit = ops_test.model.applications[DATA_INTEGRATOR_APP_NAME].units[0]
    action = await data_integrator_unit.run_action(action_name="get-credentials")
    result = await action.wait()
    if juju_.is_3_or_higher:
        assert result.results["return-code"] == 0
    else:
        assert result.results["Code"] == "0"
    assert result.results["ok"] == "True"
    return result.results["mysql"]


@pytest.mark.group(1)
@pytest.mark.abort_on_fail
async def test_external_connectivity_with_data_integrator(
    ops_test: OpsTest, mysql_router_charm_series: str
) -> None:
    """Test encryption when backend database is using TLS."""
    logger.info("Deploy and relate all applications")
    async with ops_test.fast_forward():
        # deploy mysql first
        await ops_test.model.deploy(
            MYSQL_APP_NAME, channel="8.0/edge", config={"profile": "testing"}, num_units=1
        )
        data_integrator_config = {"database-name": TEST_DATABASE}

        # ROUTER
        mysqlrouter_charm = await ops_test.build_charm(".")

        # tls, data-integrator and router
        await asyncio.gather(
            ops_test.model.deploy(
                mysqlrouter_charm,
                application_name=MYSQL_ROUTER_APP_NAME,
                num_units=None,
                series=mysql_router_charm_series,
            ),
            ops_test.model.deploy(
                TLS_APP_NAME, application_name=TLS_APP_NAME, channel="stable", config=TLS_CONFIG
            ),
            ops_test.model.deploy(
                DATA_INTEGRATOR_APP_NAME,
                application_name=DATA_INTEGRATOR_APP_NAME,
                channel="latest/stable",
                series=mysql_router_charm_series,
                config=data_integrator_config,
            ),
        )

        await ops_test.model.relate(
            f"{MYSQL_ROUTER_APP_NAME}:backend-database", f"{MYSQL_APP_NAME}:database"
        )
        await ops_test.model.relate(
            f"{DATA_INTEGRATOR_APP_NAME}:mysql", f"{MYSQL_ROUTER_APP_NAME}:database"
        )

        logger.info("Waiting for applications to become active")
        # We can safely wait only for test application to be ready, given that it will
        # only become active once all the other applications are ready.
        await ops_test.model.wait_for_idle(
            [DATA_INTEGRATOR_APP_NAME], status="active", timeout=SLOW_TIMEOUT
        )

        credentials = await get_data_integrator_credentials(ops_test)
        databases = await execute_queries_against_unit(
            credentials["endpoints"].split(",")[0].split(":")[0],
            credentials["username"],
            credentials["password"],
            ["SHOW DATABASES;"],
            port=credentials["endpoints"].split(",")[0].split(":")[1],
        )
        assert TEST_DATABASE in databases


@pytest.mark.group(1)
@pytest.mark.abort_on_fail
async def test_external_connectivity_with_data_integrator_and_tls(ops_test: OpsTest) -> None:
    """Test data integrator along with TLS operator"""
    logger.info("Ensuring no data exists in the test database")

    credentials = await get_data_integrator_credentials(ops_test)
    [database_host, database_port] = credentials["endpoints"].split(",")[0].split(":")
    mysqlrouter_unit = ops_test.model.applications[MYSQL_ROUTER_APP_NAME].units[0]

    show_tables_sql = [
        f"SHOW TABLES IN {TEST_DATABASE};",
    ]
    tables = await execute_queries_against_unit(
        database_host,
        credentials["username"],
        credentials["password"],
        show_tables_sql,
        port=database_port,
    )
    assert len(tables) == 0, f"Unexpected tables in the {TEST_DATABASE} database"

    issuer = await get_tls_certificate_issuer(
        ops_test,
        mysqlrouter_unit.name,
        host=database_host,
        port=database_port,
    )
    assert (
        "Issuer: CN = MySQL_Router_Auto_Generated_CA_Certificate" in issuer
    ), "Expected mysqlrouter autogenerated certificate"

    logger.info(f"Relating mysqlrouter with {TLS_APP_NAME}")
    await ops_test.model.relate(
        f"{MYSQL_ROUTER_APP_NAME}:certificates", f"{TLS_APP_NAME}:certificates"
    )

    for attempt in tenacity.Retrying(
        reraise=True,
        stop=tenacity.stop_after_delay(RETRY_TIMEOUT),
        wait=tenacity.wait_fixed(10),
    ):
        with attempt:
            issuer = await get_tls_certificate_issuer(
                ops_test,
                mysqlrouter_unit.name,
                host=database_host,
                port=database_port,
            )
            assert (
                "CN = Test CA" in issuer
            ), f"Expected mysqlrouter certificate from {TLS_APP_NAME}"

    create_table_and_insert_data_sql = [
        f"CREATE TABLE {TEST_DATABASE}.{TEST_TABLE} (id int, primary key(id));",
        f"INSERT INTO {TEST_DATABASE}.{TEST_TABLE} VALUES (1), (2);",
    ]
    await execute_queries_against_unit(
        database_host,
        credentials["username"],
        credentials["password"],
        create_table_and_insert_data_sql,
        port=database_port,
        commit=True,
    )

    select_data_sql = [
        f"SELECT * FROM {TEST_DATABASE}.{TEST_TABLE};",
    ]
    data = await execute_queries_against_unit(
        database_host,
        credentials["username"],
        credentials["password"],
        select_data_sql,
        port=database_port,
    )
    assert data == [1, 2], f"Unexpected data in table {TEST_DATABASE}.{TEST_TABLE}"

    logger.info(f"Removing relation between mysqlrouter and {TLS_APP_NAME}")
    await ops_test.model.applications[MYSQL_ROUTER_APP_NAME].remove_relation(
        f"{MYSQL_ROUTER_APP_NAME}:certificates", f"{TLS_APP_NAME}:certificates"
    )

    for attempt in tenacity.Retrying(
        reraise=True,
        stop=tenacity.stop_after_delay(RETRY_TIMEOUT),
        wait=tenacity.wait_fixed(10),
    ):
        with attempt:
            issuer = await get_tls_certificate_issuer(
                ops_test,
                mysqlrouter_unit.name,
                host=database_host,
                port=database_port,
            )
            assert (
                "Issuer: CN = MySQL_Router_Auto_Generated_CA_Certificate" in issuer
            ), "Expected mysqlrouter autogenerated certificate"

    select_data_sql = [
        f"SELECT * FROM {TEST_DATABASE}.{TEST_TABLE};",
    ]
    data = await execute_queries_against_unit(
        database_host,
        credentials["username"],
        credentials["password"],
        select_data_sql,
        port=database_port,
    )
    assert data == [1, 2], f"Unexpected data in table {TEST_DATABASE}.{TEST_TABLE}"

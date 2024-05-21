import os
import random
from unittest.mock import Mock, patch

import mysql.connector
import pytest
from dotenv import find_dotenv, load_dotenv
from pytest_docker.plugin import get_docker_services
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, create_engine
from starlette_context import context, request_cycle_context

# This import is required to create the tables
from keep.api.core.dependencies import SINGLE_TENANT_UUID
from keep.api.models.db.alert import *
from keep.api.models.db.provider import *
from keep.api.models.db.rule import *
from keep.api.models.db.tenant import *
from keep.api.models.db.user import *
from keep.api.models.db.workflow import *
from keep.contextmanager.contextmanager import ContextManager

load_dotenv(find_dotenv())


@pytest.fixture
def ctx_store() -> dict:
    """
    Create a context store
    """
    return {"X-Request-ID": random.randint(10000, 90000)}


@pytest.fixture(autouse=True)
def mocked_context(ctx_store) -> None:
    with request_cycle_context(ctx_store):
        yield context


@pytest.fixture
def context_manager():
    os.environ["STORAGE_MANAGER_DIRECTORY"] = "/tmp/storage-manager"
    return ContextManager(tenant_id=SINGLE_TENANT_UUID, workflow_id="1234")


@pytest.fixture(scope="session")
def docker_services(
    docker_compose_command,
    docker_compose_file,
    docker_compose_project_name,
    docker_setup,
    docker_cleanup,
):
    """Start the MySQL service (or any other service from docker-compose.yml)."""

    # If we are running in Github Actions, we don't need to start the docker services
    # as they are already handled by the Github Actions
    if os.getenv("GITHUB_ACTIONS") == "true":
        print("Running in Github Actions, skipping docker services")
        yield
        return

    # For local development, you can avoid spinning up the mysql container every time:
    if os.getenv("SKIP_DOCKER"):
        yield
        return

    # Else, start the docker services
    try:
        import inspect

        stack = inspect.stack()
        # this is a hack to support more than one docker-compose file
        for frame in stack:
            if frame.function == "db_session":
                db_type = frame.frame.f_locals["db_type"]
                docker_compose_file = docker_compose_file.replace(
                    "docker-compose.yml", f"docker-compose-{db_type}.yml"
                )
                break
        with get_docker_services(
            docker_compose_command,
            docker_compose_file,
            docker_compose_project_name,
            docker_setup,
            docker_cleanup,
        ) as docker_service:
            yield docker_service

    except Exception as e:
        print(f"Docker services could not be started: {e}")
        # Optionally, provide a fallback or mock service here
        yield None


def is_mysql_responsive(host, port, user, password, database):
    try:
        # Create a MySQL connection
        connection = mysql.connector.connect(
            host=host, port=port, user=user, password=password, database=database
        )

        # Check if the connection is established
        if connection.is_connected():
            return True

    except Exception:
        print("Mysql still not up")
        pass

    return False


@pytest.fixture(scope="session")
def mysql_container(docker_ip, docker_services):
    try:
        if os.getenv("SKIP_DOCKER") or os.getenv("GITHUB_ACTIONS") == "true":
            print("Running in Github Actions or SKIP_DOCKER is set, skipping mysql")
            yield
            return
        docker_services.wait_until_responsive(
            timeout=60.0,
            pause=0.1,
            check=lambda: is_mysql_responsive(
                "127.0.0.1", 3306, "root", "keep", "keep"
            ),
        )
        yield "mysql+pymysql://root:keep@localhost:3306/keep"
    except Exception:
        print("Exception occurred while waiting for MySQL to be responsive")
    finally:
        print("Tearing down MySQL")
        if docker_services:
            docker_services.down()


def is_mssql_responsive(host, port, user, password, database):
    import pyodbc

    try:
        conn = pyodbc.connect(
            f"DRIVER=FreeTDS;SERVER={host};PORT={port};DATABASE={database};UID={user};PWD={password}"
        )
        cursor = conn.cursor()
        cursor.execute("SELECT 1")
        cursor.fetchall()
        return True
    except Exception:
        print("MSSQL still not up")
        pass

    return False


@pytest.fixture(scope="session")
def mssql_container(docker_ip, docker_services):
    try:
        if os.getenv("SKIP_DOCKER") or os.getenv("GITHUB_ACTIONS") == "true":
            print("Running in Github Actions or SKIP_DOCKER is set, skipping mysql")
            yield
            return
        docker_services.wait_until_responsive(
            timeout=60.0,
            pause=0.1,
            check=lambda: is_mssql_responsive(
                "127.0.0.1", 1433, "sa", "VeryStrongPassword1", "keepdb"
            ),
        )
        yield "mssql+pyodbc://sa:VeryStrongPassword1#@localhost:1433/keepdb?driver=FreeTDS"
    except Exception:
        print("Exception occurred while waiting for MySQL to be responsive")
    finally:
        print("Tearing down MSSQL")
        if docker_services:
            docker_services.down()


@pytest.fixture
def db_session(request):
    # mysql/mssql
    if request and hasattr(request, "param") and "db" in request.param:
        db_type = request.param.get("db")
        db_connection_string = request.getfixturevalue(f"{db_type}_container")
        mock_engine = create_engine(db_connection_string)
    # sqlite
    else:
        db_connection_string = "sqlite:///:memory:"
        mock_engine = create_engine(
            db_connection_string,
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )

    SQLModel.metadata.create_all(mock_engine)

    # Mock the environment variables so db.py will use it
    os.environ["DATABASE_CONNECTION_STRING"] = db_connection_string

    # Create a session
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=mock_engine)
    session = SessionLocal()
    # Prepopulate the database with test data

    # 1. Create a tenant
    tenant_data = [
        Tenant(id=SINGLE_TENANT_UUID, name="test-tenant", created_by="tests@keephq.dev")
    ]
    session.add_all(tenant_data)
    session.commit()

    with patch("keep.api.core.db.engine", mock_engine):
        yield session

    # delete the database
    SQLModel.metadata.drop_all(mock_engine)
    # Clean up after the test
    session.close()


@pytest.fixture
def mocked_context_manager():
    context_manager = Mock(spec=ContextManager)
    # Simulate contexts as needed for each test case
    context_manager.steps_context = {}
    context_manager.providers_context = {}
    context_manager.event_context = {}
    context_manager.click_context = {}
    context_manager.foreach_context = {"value": None}
    context_manager.dependencies = set()
    context_manager.get_full_context.return_value = {
        "steps": {},
        "providers": {},
        "event": {},
        "alert": {},
        "foreach": {"value": None},
        "env": {},
    }
    return context_manager

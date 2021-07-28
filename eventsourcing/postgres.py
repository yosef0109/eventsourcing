import threading
from distutils.util import strtobool
from threading import Event, Timer
from types import TracebackType
from typing import Any, Dict, List, Mapping, Optional, Type
from uuid import UUID

import psycopg2
import psycopg2.errors
import psycopg2.extras
from psycopg2.extensions import connection, cursor

from eventsourcing.persistence import (
    AggregateRecorder,
    ApplicationRecorder,
    InfrastructureFactory,
    Notification,
    OperationalError,
    ProcessRecorder,
    RecordConflictError,
    StoredEvent,
    Tracking,
)

psycopg2.extras.register_uuid()


class Connection:
    def __init__(self, c: connection, max_age: Optional[float]):
        self.c = c
        self.max_age = max_age
        self.is_idle = Event()
        self.is_closing = Event()
        self.timer: Optional[Timer]
        if max_age is not None:
            self.timer = Timer(interval=max_age, function=self.close_on_timer)
            self.timer.setDaemon(True)
            self.timer.start()
        else:
            self.timer = None

    def cursor(self) -> cursor:
        return self.c.cursor(cursor_factory=psycopg2.extras.DictCursor)

    def rollback(self) -> None:
        self.c.rollback()

    def commit(self) -> None:
        self.c.commit()

    def close_on_timer(self) -> None:
        self.close()

    def close(self, timeout: Optional[float] = None) -> None:
        if self.timer is not None:
            self.timer.cancel()
        self.is_closing.set()
        self.is_idle.wait(timeout=timeout)
        self.c.close()

    @property
    def is_closed(self) -> bool:
        return self.c.closed


class Transaction:
    # noinspection PyShadowingNames
    def __init__(self, c: Connection):
        self.c = c
        self.has_entered = False

    def __enter__(self) -> cursor:
        self.has_entered = True
        return self.c.cursor()

    def __exit__(
        self,
        exc_type: Type[BaseException],
        exc_val: BaseException,
        exc_tb: TracebackType,
    ) -> None:
        try:
            if exc_type:
                self.c.rollback()
            else:
                self.c.commit()
        finally:
            self.c.is_idle.set()

    def __del__(self) -> None:
        if not self.has_entered:
            self.c.is_idle.set()
            raise RuntimeWarning(f"Transaction {self} was not used as context manager")


class PostgresDatastore:
    def __init__(
        self,
        dbname: str,
        host: str,
        port: str,
        user: str,
        password: str,
        conn_max_age: Optional[float] = None,
        pre_ping: bool = False,
    ):
        self.dbname = dbname
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.conn_max_age = conn_max_age
        self.pre_ping = pre_ping
        self._connections: Dict[int, Connection] = {}

    def transaction(self) -> Transaction:
        thread_id = threading.get_ident()
        try:
            c = self._connections[thread_id]
            c.is_idle.clear()
            if c.is_closing.is_set() or c.is_closed:
                c = self._create_connection(thread_id)
            elif self.pre_ping:
                try:
                    c.cursor().execute("SELECT 1")
                except psycopg2.Error:
                    c = self._create_connection(thread_id)
        except KeyError:
            c = self._create_connection(thread_id)
        return Transaction(c)

    def _create_connection(self, thread_id: int) -> Connection:
        # Make a connection to a Postgres database.
        c = Connection(
            psycopg2.connect(
                dbname=self.dbname,
                host=self.host,
                port=self.port,
                user=self.user,
                password=self.password,
            ),
            max_age=self.conn_max_age,
        )
        self._connections[thread_id] = c
        return c

    def close_connection(self) -> None:
        thread_id = threading.get_ident()
        try:
            c = self._connections.pop(thread_id)
        except KeyError:
            pass
        else:
            c.close()

    def close_all_connections(self, timeout: Optional[float] = None) -> None:
        for c in self._connections.values():
            c.close(timeout=timeout)
        self._connections.clear()

    def __del__(self) -> None:
        self.close_all_connections(timeout=1)


# noinspection SqlResolve
class PostgresAggregateRecorder(AggregateRecorder):
    def __init__(self, datastore: PostgresDatastore, events_table_name: str):
        self.datastore = datastore
        self.events_table_name = events_table_name
        self.create_table_statements = self.construct_create_table_statements()
        self.insert_events_statement = (
            f"INSERT INTO {self.events_table_name} VALUES (%s, %s, %s, %s)"
        )
        self.select_events_statement = (
            f"SELECT * FROM {self.events_table_name} WHERE originator_id = %s "
        )

    def construct_create_table_statements(self) -> List[str]:
        statement = (
            "CREATE TABLE IF NOT EXISTS "
            f"{self.events_table_name} ("
            "originator_id uuid NOT NULL, "
            "originator_version integer NOT NULL, "
            "topic text, "
            "state bytea, "
            "PRIMARY KEY "
            "(originator_id, originator_version))"
        )
        return [statement]

    def create_table(self) -> None:
        try:
            with self.datastore.transaction() as c:
                for statement in self.create_table_statements:
                    c.execute(statement)
        except psycopg2.Error as e:
            self.datastore.close_connection()
            raise OperationalError(e)

    def insert_events(self, stored_events: List[StoredEvent], **kwargs: Any) -> None:
        try:
            with self.datastore.transaction() as c:
                self._insert_events(c, stored_events, **kwargs)
        except psycopg2.IntegrityError as e:
            raise RecordConflictError(e)
        except psycopg2.Error as e:
            self.datastore.close_connection()
            raise OperationalError(e)

    def _insert_events(
        self,
        c: cursor,
        stored_events: List[StoredEvent],
        **kwargs: Any,
    ) -> None:
        params = []
        for stored_event in stored_events:
            params.append(
                (
                    stored_event.originator_id,
                    stored_event.originator_version,
                    stored_event.topic,
                    stored_event.state,
                )
            )
        c.executemany(self.insert_events_statement, params)

    def select_events(
        self,
        originator_id: UUID,
        gt: Optional[int] = None,
        lte: Optional[int] = None,
        desc: bool = False,
        limit: Optional[int] = None,
    ) -> List[StoredEvent]:
        statement = self.select_events_statement
        params: List[Any] = [originator_id]
        if gt is not None:
            statement += "AND originator_version > %s "
            params.append(gt)
        if lte is not None:
            statement += "AND originator_version <= %s "
            params.append(lte)
        statement += "ORDER BY originator_version "
        if desc is False:
            statement += "ASC "
        else:
            statement += "DESC "
        if limit is not None:
            statement += "LIMIT %s "
            params.append(limit)
        # statement += ";"
        stored_events = []
        try:
            with self.datastore.transaction() as c:
                c.execute(statement, params)
                for row in c.fetchall():
                    stored_events.append(
                        StoredEvent(
                            originator_id=row["originator_id"],
                            originator_version=row["originator_version"],
                            topic=row["topic"],
                            state=bytes(row["state"]),
                        )
                    )
        except psycopg2.Error as e:
            self.datastore.close_connection()
            raise OperationalError(e)
        return stored_events


# noinspection SqlResolve
class PostgresApplicationRecorder(
    PostgresAggregateRecorder,
    ApplicationRecorder,
):
    def __init__(
        self,
        datastore: PostgresDatastore,
        events_table_name: str = "stored_events",
    ):
        super().__init__(datastore, events_table_name)
        self.select_notifications_statement = (
            "SELECT * "
            f"FROM {self.events_table_name} "
            "WHERE notification_id>=%s "
            "ORDER BY notification_id "
            "LIMIT %s"
        )
        self.select_max_notification_id_statement = (
            f"SELECT MAX(notification_id) FROM {self.events_table_name}"
        )

    def construct_create_table_statements(self) -> List[str]:
        statements = [
            "CREATE TABLE IF NOT EXISTS "
            f"{self.events_table_name} ("
            "originator_id uuid NOT NULL, "
            "originator_version integer NOT NULL, "
            "topic text, "
            "state bytea, "
            "notification_id SERIAL, "
            "PRIMARY KEY "
            "(originator_id, originator_version))",
            f"CREATE UNIQUE INDEX IF NOT EXISTS "
            f"{self.events_table_name}_notification_id_idx "
            f"ON {self.events_table_name} (notification_id ASC);",
        ]
        return statements

    def select_notifications(self, start: int, limit: int) -> List[Notification]:
        """
        Returns a list of event notifications
        from 'start', limited by 'limit'.
        """
        params = [start, limit]
        try:
            with self.datastore.transaction() as c:
                c.execute(self.select_notifications_statement, params)
                notifications = []
                for row in c.fetchall():
                    notifications.append(
                        Notification(
                            id=row["notification_id"],
                            originator_id=row["originator_id"],
                            originator_version=row["originator_version"],
                            topic=row["topic"],
                            state=bytes(row["state"]),
                        )
                    )
        except psycopg2.Error as e:
            self.datastore.close_connection()
            raise OperationalError(e)
        return notifications

    def max_notification_id(self) -> int:
        """
        Returns the maximum notification ID.
        """
        try:
            with self.datastore.transaction() as c:
                c.execute(self.select_max_notification_id_statement)
                return c.fetchone()[0] or 0
        except psycopg2.Error as e:
            self.datastore.close_connection()
            raise OperationalError(e)


class PostgresProcessRecorder(
    PostgresApplicationRecorder,
    ProcessRecorder,
):
    def __init__(
        self,
        datastore: PostgresDatastore,
        events_table_name: str,
        tracking_table_name: str,
    ):
        self.tracking_table_name = tracking_table_name
        super().__init__(datastore, events_table_name)
        self.insert_tracking_statement = (
            f"INSERT INTO {self.tracking_table_name} " "VALUES (%s, %s)"
        )
        self.select_max_tracking_id_statement = (
            "SELECT MAX(notification_id) "
            f"FROM {self.tracking_table_name} "
            "WHERE application_name=%s"
        )

    def construct_create_table_statements(self) -> List[str]:
        statements = super().construct_create_table_statements()
        statements.append(
            "CREATE TABLE IF NOT EXISTS "
            f"{self.tracking_table_name} ("
            "application_name text, "
            "notification_id int, "
            "PRIMARY KEY "
            "(application_name, notification_id))"
        )
        return statements

    def max_tracking_id(self, application_name: str) -> int:
        params = [application_name]
        try:
            with self.datastore.transaction() as c:
                c.execute(self.select_max_tracking_id_statement, params)
                return c.fetchone()[0] or 0
        except psycopg2.Error as e:
            self.datastore.close_connection()
            raise OperationalError(e)

    def _insert_events(
        self,
        c: cursor,
        stored_events: List[StoredEvent],
        **kwargs: Any,
    ) -> None:
        super()._insert_events(c, stored_events, **kwargs)
        tracking: Optional[Tracking] = kwargs.get("tracking", None)
        if tracking is not None:
            c.execute(
                self.insert_tracking_statement,
                (
                    tracking.application_name,
                    tracking.notification_id,
                ),
            )


class Factory(InfrastructureFactory):
    POSTGRES_DBNAME = "POSTGRES_DBNAME"
    POSTGRES_HOST = "POSTGRES_HOST"
    POSTGRES_PORT = "POSTGRES_PORT"
    POSTGRES_USER = "POSTGRES_USER"
    POSTGRES_PASSWORD = "POSTGRES_PASSWORD"
    POSTGRES_CONN_MAX_AGE = "POSTGRES_CONN_MAX_AGE"
    CREATE_TABLE = "CREATE_TABLE"
    POSTGRES_PRE_PING = "POSTGRES_PRE_PING"

    def __init__(self, application_name: str, env: Mapping):
        super().__init__(application_name, env)
        dbname = self.getenv(self.POSTGRES_DBNAME)
        if dbname is None:
            raise EnvironmentError(
                "Postgres database name not found "
                "in environment with key "
                f"'{self.POSTGRES_DBNAME}'"
            )

        host = self.getenv(self.POSTGRES_HOST)
        if host is None:
            raise EnvironmentError(
                "Postgres host not found "
                "in environment with key "
                f"'{self.POSTGRES_HOST}'"
            )

        port = self.getenv(self.POSTGRES_PORT) or "5432"

        user = self.getenv(self.POSTGRES_USER)
        if user is None:
            raise EnvironmentError(
                "Postgres user not found "
                "in environment with key "
                f"'{self.POSTGRES_USER}'"
            )

        password = self.getenv(self.POSTGRES_PASSWORD)
        if password is None:
            raise EnvironmentError(
                "Postgres password not found "
                "in environment with key "
                f"'{self.POSTGRES_PASSWORD}'"
            )

        conn_max_age: Optional[float]
        conn_max_age_str = self.getenv(self.POSTGRES_CONN_MAX_AGE)
        if conn_max_age_str is None:
            conn_max_age = None
        elif conn_max_age_str == "":
            conn_max_age = None
        else:
            try:
                conn_max_age = float(conn_max_age_str)
            except ValueError:
                raise EnvironmentError(
                    f"Postgres environment value for key "
                    f"'{self.POSTGRES_CONN_MAX_AGE}' is invalid. "
                    f"If set, a float or empty string is expected: "
                    f"'{conn_max_age_str}'"
                )

        pre_ping = strtobool(self.getenv(self.POSTGRES_PRE_PING) or "no")

        self.datastore = PostgresDatastore(
            dbname=dbname,
            host=host,
            port=port,
            user=user,
            password=password,
            conn_max_age=conn_max_age,
            pre_ping=pre_ping,
        )

    def aggregate_recorder(self, purpose: str = "events") -> AggregateRecorder:
        prefix = self.application_name.lower() or "stored"
        events_table_name = prefix + "_" + purpose
        recorder = PostgresAggregateRecorder(
            datastore=self.datastore, events_table_name=events_table_name
        )
        if self.env_create_table():
            recorder.create_table()
        return recorder

    def application_recorder(self) -> ApplicationRecorder:
        prefix = self.application_name.lower() or "stored"
        events_table_name = prefix + "_events"
        recorder = PostgresApplicationRecorder(
            datastore=self.datastore, events_table_name=events_table_name
        )
        if self.env_create_table():
            recorder.create_table()
        return recorder

    def process_recorder(self) -> ProcessRecorder:
        prefix = self.application_name.lower() or "stored"
        events_table_name = prefix + "_events"
        prefix = self.application_name.lower() or "notification"
        tracking_table_name = prefix + "_tracking"
        recorder = PostgresProcessRecorder(
            datastore=self.datastore,
            events_table_name=events_table_name,
            tracking_table_name=tracking_table_name,
        )
        if self.env_create_table():
            recorder.create_table()
        return recorder

    def env_create_table(self) -> bool:
        default = "yes"
        return bool(strtobool(self.getenv(self.CREATE_TABLE) or default))

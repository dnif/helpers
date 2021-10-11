#!/usr/bin/env python3
import orjson
import time
import pyodbc
import unicodedata
import datetime
import ipaddr
import uuid
import yaml
import os
import jaydebeapi
import traceback
import sys
import ast

from collections import OrderedDict
from event_publisher import EventPublish
from utils import yaml_handler, logg_helper

init_config_path = sys.argv[1]

try:
    if os.path.isfile(init_config_path):
        config = yaml_handler.load(init_config_path)
    else:
        raise Exception("Connector not configured")
except Exception as e:
    raise Exception(e)

connector_path = os.path.dirname(os.path.abspath(__file__))

connector_config = config.get('connector_config', {})
db_config = config.get("database_config", {})
forwarding_config = config.get("forwarding_config", {})

log_level = connector_config.get('log_level', 1)
log_max_bytes = connector_config.get('log_max_bytes', 10000000)  # 10 mb log file limit
log_max_bkup_count = connector_config.get('log_max_backup_count', 10)  # 10 backup log file limit

connector_name = init_config_path.split('/')[-1].split('.')[0]

# Log config file handling
default_log_file = f"{connector_path}/log/{connector_name}/{connector_name}.log"
log_file = connector_config.get('log_file_path', default_log_file)

# Create directory for logging if not exist
log_directory = os.path.dirname(os.path.abspath(log_file))
if not os.path.exists(log_directory):
    os.makedirs(log_directory)

# Bookmark config file handling
default_bookmark_file = f"{connector_path}/bookmark/{connector_name}.yml"
bookmark_file = connector_config.get('bookmark_path', default_bookmark_file)

# Create directory for bookmarking if not exist
bookmark_directory = os.path.dirname(os.path.abspath(bookmark_file))
if not os.path.exists(bookmark_directory):
    os.makedirs(bookmark_directory)

bookmark = dict()

marker_initial_value = db_config.get('initial_value', '')
query_vars = dict()
query_vars['field_name'] = db_config.get('field_name', '')

logger = logg_helper.get_logger(f"RDBMS_{connector_name}", int(log_level), file_name=log_file,
                                max_bytes=log_max_bytes, backup_count=log_max_bkup_count)
logger.info(f"Log level set to {log_level}")

if db_config.get('connection_mode', 'odbc').lower() == 'jdbc':
    default_jars = [":",
                    f"{connector_path}/rdbms_jar/postgresql-42.2.9.jar",
                    f"{connector_path}/rdbms_jar/ojdbc8.jar",
                    f"{connector_path}/rdbms_jar/mysql-connector-java-8.0.21.jar",
                    f"{connector_path}/rdbms_jar/mssql-jdbc-7.4.1.jre11.jar"
                    ]

    # Set CLASSPATH for JDBC driver
    os.environ['CLASSPATH'] = db_config.get('classpath', ":".join(default_jars))


def fix_datatype(row):
    for key, value in row.items():
        try:
            if key in ['AnalyzerIPV4', 'SourceIPV4', 'TargetIPV4']:
                if value is not None:
                    row[key] = str(ipaddr.IPv4Address(abs(value)))
            elif key in ['AnalyzerIPV6', 'SourceIPV6', 'TargetIPV6']:
                if value is not None:
                    row[key] = str(ipaddr.IPv6Address(ipaddr.Bytes(value)))
            elif value is None:
                row[key] = 'NULL'
            elif type(value) is bytes:
                row[key] = unicodedata.normalize('NFKD', value).encode('ascii', 'ignore')
            elif type(value) is datetime.datetime:
                row[key] = value.isoformat()
            elif type(value) is uuid.UUID:
                row[key] = value.hex
            elif 'java class' in str(type(value)):
                try:
                    row[key] = value.toString()
                except:
                    row[key] = value
        except Exception as e:
            logger.warning(f"value of '{key}' is not valid. Warning: {e}")
    return row


def get_connection():
    try:
        logger.debug("Connecting to the database server..")
        mode = db_config.get('connection_mode', 'odbc').lower()

        if mode == 'odbc':
            connection = pyodbc.connect(db_config.get('connection_string'))
            logger.debug("Connected to the database server by ODBC!")
            return connection
        elif mode == 'jdbc':
            connection = jaydebeapi.connect(db_config.get("connection_driver", ''),
                                            db_config.get('connection_string', ''),
                                            [db_config.get('user', ''), db_config.get('password', '')])
            logger.debug("Connected to the database server by JDBC!")
            return connection
        else:
            logger.error(f"Invalid connection_mode '{mode}' configured")
            logger.info("Valid connection_mode [odbc, jdbc]")
            raise Exception(f"Invalid connection_mode '{mode}' configured")

    except Exception as e:
        logger.error(f"Error in database connectivity : {e}")


def fetch_log(connection):
    global bookmark, query_vars

    try:
        log_data = []

        logger.debug("Creating DB cursor")
        cursor = connection.cursor()

        marker_value = bookmark.get('marker_value', marker_initial_value)
        query_vars['initial_value'] = marker_value
        query_str = db_config.get('query', '')
        query = query_str.format(**query_vars)
        logger.debug(f"SQL query : {query}")

        cursor.execute(query)
        logger.debug("SQL query executed!")

        rows = cursor.fetchall()
        logger.debug("SQL query Result Fetched!")

        if rows is not None:
            desc = [unicodedata.normalize('NFKD', d[0]).encode('ascii', 'ignore') for d in cursor.description]
            key = []
            for i in desc:
                key.append(i.decode())
            # create list of dictionaries from desc and rows
            count = 0
            for row in rows:
                count += 1
                result = fix_datatype(dict(zip(key, row)))

                if count == len(rows):
                    # Set the bookmark and write into config file
                    bookmark['marker_value'] = str(result[db_config.get('field_name', '')])
                    with open(bookmark_file, 'w') as yaml_file:
                        yaml.safe_dump(bookmark, yaml_file)
                log_data.append(str(result))

        logger.debug(f"Log sent till : {db_config.get('field_name', '')} = {bookmark.get('marker_value', '')}")
        return log_data

    except Exception as e:
        logger.error(f"Error in rdbms connector: {e}")
        logger.debug(traceback.format_exc())

    finally:
        try:
            cursor.close()
        except:
            logger.error("Unable to close cursor")


def execute():
    try:
        if config:
            logger.info("configuration received")
        else:
            logger.error("connector not configured")
            sys.exit(0)

        global bookmark, bunch_ar

        backoff = connector_config.get('backoff_duration', 10)

        if os.path.exists(bookmark_file):
            try:
                with open(bookmark_file, 'r') as stream:
                    bookmark = yaml.safe_load(stream)
            except Exception as e:
                logger.error(f"Could not open bookmark file : {e}")
                sys.exit(0)

        connection = get_connection()

        if connection:
            bunch_ar = []

            evt_pub_config = {}
            evt_pub_config.update(connector_config)
            evt_pub_config.update(forwarding_config)

            obj = EventPublish(evt_pub_config)
            obj.spawn_threads()
            while True:
                logs = fetch_log(connection)
                if not logs:
                    logger.info(f"No logs to fetch. Sleeping for {backoff} seconds")
                    if isinstance(backoff, str):
                        time.sleep(int(backoff))
                    else:
                        time.sleep(backoff)
                else:
                    logger.debug("Logs received")
                    for raw_log in logs:
                        try:
                            log_event = OrderedDict()
                            log_event['log_source'] = connector_config.get('log_source', '')
                            log_event.update(ast.literal_eval(raw_log))
                        except Exception as e:
                            logger.error(f"Error updating 'log_source' : {e}")
                            logger.warning("Skipping log. Check 'raw_log' in DEBUG mode")
                            logger.debug(f"raw_log : {raw_log}")
                            continue
                        obj.sendtoevtbuffer(orjson.dumps(log_event))
                        logger.debug("Log sent to buffer")

    except Exception as e:
        logger.error(e)
        sys.exit(0)

    finally:
        try:
            connection.close()
        except:
            logger.error("Unable to close database connection")


if __name__ == "__main__":
    execute()
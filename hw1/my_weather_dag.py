from datetime import datetime
from datetime import timezone
from airflow import DAG
from airflow.operators.python import PythonOperator
from airflow.providers.common.sql.operators.sql import SQLExecuteQueryOperator
from airflow.providers.http.sensors.http import HttpSensor
from airflow.models.variable import Variable
from airflow.providers.http.operators.http import HttpOperator

import json

coords = {
    "Lviv": (49.84, 4.03),
    "Kyiv": (50.45, 30.52),
    "Harkiv": (49.99, 36.23),
    "Odesa": (46.48, 30.72),
    "Zhmerynka": (49.04, 28.11),
}

with DAG(
    dag_id="my_weather_dag",
    schedule="@daily",
    start_date=datetime(2023, 11, 15),
    catchup=True,
) as dag:
    db_create = SQLExecuteQueryOperator(
        task_id="create_table_sqlite",
        conn_id="weather_sqlite_conn",
        sql="""
            CREATE TABLE IF NOT EXISTS measures
            (
            city VARCHAR(255),
            timestamp TIMESTAMP,
            temperature FLOAT,
            humidity FLOAT,
            clouds FLOAT,
            wind_speed FLOAT
            );
        """,
    )

    check_api = HttpSensor(
        task_id="check_api",
        http_conn_id="weather_api_conn",
        endpoint="data/3.0/onecall",
        request_params={
            "appid": Variable.get("WEATHER_API_KEY"),
            "lat": "49.04",
            "lon": "28.11",
        },
    )

    for city, coordinates in coords.items():
        lat, lon = coordinates

        def _get_timestamp(ti, ds):
            timestamp = int(
                datetime.strptime(ds, "%Y-%m-%d")
                .replace(tzinfo=timezone.utc)
                .timestamp()
            )
            return str(timestamp)

        get_timestamp = PythonOperator(
            task_id=f"get_timestamp_{city}", python_callable=_get_timestamp
        )

        extract_data = HttpOperator(
            task_id=f"extract_data_{city}",
            http_conn_id="weather_api_conn",
            endpoint="data/3.0/onecall/timemachine",
            data={
                "appid": Variable.get("WEATHER_API_KEY"),
                "lat": str(lat),
                "lon": str(lon),
                "dt": f"""{{{{ti.xcom_pull(task_ids='get_timestamp_{city}')}}}}""",
                "exclude": "minutely,hourly,daily,alerts",
            },
            method="GET",
            response_filter=lambda x: json.loads(x.text),
            log_response=True,
        )

        def _process_weather(ti, ds):
            info = ti.xcom_pull(task_ids=f"extract_data_{city}")
            current = info["data"][0]
            timestamp = current["dt"]
            temperature = current["temp"]
            humidity = current["humidity"]
            clouds = float(current["clouds"])
            wind_speed = current["wind_speed"]
            return timestamp, temperature, humidity, clouds, wind_speed

        process_weather = PythonOperator(
            task_id=f"process_weather_{city}", python_callable=_process_weather
        )

        save_data = SQLExecuteQueryOperator(
            task_id=f"save_data_{city}",
            conn_id="weather_sqlite_conn",
            sql=f"""
            INSERT INTO measures (city, timestamp, temperature, humidity, clouds, wind_speed) VALUES
            ('{city}',
            {{{{ti.xcom_pull(task_ids='process_weather_{city}')[0]}}}},
            {{{{ti.xcom_pull(task_ids='process_weather_{city}')[1]}}}},
            {{{{ti.xcom_pull(task_ids='process_weather_{city}')[2]}}}},
            {{{{ti.xcom_pull(task_ids='process_weather_{city}')[3]}}}},
            {{{{ti.xcom_pull(task_ids='process_weather_{city}')[4]}}}});
            """,
        )

        (
            db_create
            >> check_api
            >> get_timestamp
            >> extract_data
            >> process_weather
            >> save_data
        )
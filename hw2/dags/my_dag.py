import requests
import easyocr
import json
from airflow import DAG
from datetime import datetime
from openai import OpenAI
from bs4 import BeautifulSoup
from airflow.operators.python import PythonOperator
from airflow.models import Variable
from airflow.providers.postgres.operators.postgres import PostgresOperator
from airflow.hooks.postgres_hook import PostgresHook


def get_websites_ocr(phrases):
    api = OpenAI(
        api_key=Variable.get("OPEN_AI_API_KEY"),
    )

    chat_completion = api.chat.completions.create(
        model="gpt-3.5-turbo",
        messages=[
            {
                "role": "system",
                "content": "Return response only in the JSON in the format: {\"websites\": ['website1, 'website2']} and without duplicates.",
            },
            {
                "role": "assistant",
                "content": f"Extract website names (end with .com, .net, and so on) from the phrase that I'll give you and return them as JSON list format. If no website domain detected - return empty list. Don't write any code, just give me a JSON: \\n{phrases}",
            },
        ],
        temperature=1,
        max_tokens=512,
        top_p=1,
        frequency_penalty=0,
        presence_penalty=0,
    )
    try:
        response = json.loads(chat_completion.choices[0].message.content)

        if type(response) is dict:
            if "websites" in response:
                websites = response["websites"]

                if type(websites) is list:
                    return websites

        if type(response) is list:
            return response

    except:
        return []

    return []


def add_websites_info():
    postgres_hook = PostgresHook(postgres_conn_id="postgres_hw2")
    select = "SELECT domain FROM Scrapes WHERE scrapped = False;"
    insert = "INSERT INTO DomainsInfo (domain, info) VALUES (%s, %s);"
    load = "UPDATE Scrapes SET scrapped = True WHERE domain = %s;"


    entries = postgres_hook.get_records(sql=select)
    websites = [entry[0] for entry in entries]
    api = OpenAI(api_key=Variable.get("OPEN_AI_API_KEY"))

    for website in websites:
        try:
            data = api.chat.completions.create(
                model="gpt-3.5-turbo",
                messages=[
                    {
                        "role": "user",
                        "content": f"Give the information about this domain: {website}",
                    }
                ],
                temperature=1,
                max_tokens=512,
                top_p=1,
                frequency_penalty=0,
                presence_penalty=0,
            )

            data = data.choices[0].message.content.strip()
            postgres_hook.run(insert, parameters=(website, data))
            postgres_hook.run(load, parameters=(website,))

        except Exception as e:
            print(f"Error requesting domain info: {e}")


def detect_websites(ti):
    reader = easyocr.Reader(["en"])
    urls = ti.xcom_pull(key="urls", task_ids="scrape_images")
    postgres_hook = PostgresHook(postgres_conn_id="postgres_hw2")

    conn = postgres_hook.get_conn()
    cursor = conn.cursor()

    for url in urls:
        try:
            result = reader.readtext(url, detail=0)
            websites = get_websites_ocr(result)

            for website in websites:
                cursor.execute(
                    "INSERT INTO Scrapes (domain, scrapped) VALUES (%s, %s) ON CONFLICT (domain) DO NOTHING;",
                    (website, False),
                )
            conn.commit()

        except Exception as e:
            print("Error: ", e)

    cursor.close()
    conn.close()



def get_images_urls(ti, url):
    try:
        response = requests.get(url)

        if response.status_code != 200:
            response.raise_for_status()

        img_tags = BeautifulSoup(response.text, "html.parser").find_all("img")
        urls = [img.get("src") for img in img_tags if img.get("src") is not None]
        ti.xcom_push("urls", urls)

    except Exception as e:
        print(f"Error scraping images: {e}")
        ti.xcom_push("urls", [])


with DAG(
    "my_dag",
    schedule_interval="@daily",
    start_date=datetime(2023, 12, 13),
    catchup=False,
) as dag:
    create_domains_table = PostgresOperator(
        task_id="create_domains_table",
        postgres_conn_id="postgres_hw2",
        sql="""
        CREATE TABLE IF NOT EXISTS Scrapes (
            id  SERIAL PRIMARY KEY,
            domain VARCHAR(128) UNIQUE,
            scrapped BOOlEAN
        );
        """,
    )

    create_data_table = PostgresOperator(
        task_id="create_data_table",
        postgres_conn_id="postgres_hw2",
        sql="""
        CREATE TABLE IF NOT EXISTS DomainsInfo (
            id  SERIAL PRIMARY KEY,
            domain VARCHAR(128),
            info TEXT
        );
        """,
    )

    scrape_images_task = PythonOperator(
        task_id="scrape_images",
        python_callable=get_images_urls,
        op_kwargs={
            "url": Variable.get("URL_WITH_IMAGES")
        },
        dag=dag,
    )

    detect_websites_ocr = PythonOperator(
        task_id="detect_websites",
        python_callable=detect_websites,
        dag=dag,
    )

    get_website_info = PythonOperator(
        task_id="add_websites_info",
        python_callable=add_websites_info,
        dag=dag,
    )



    create_domains_table >> create_data_table >> scrape_images_task >> detect_websites_ocr >> get_website_info
    
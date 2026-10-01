import psycopg2

connection = psycopg2.connect(
    "postgresql://postgres:[YOUR-PASSWORD]@db.gqkthxeqrnppllajsxws.supabase.co:5432/postgres"
)

print("Connected successfully!")

connection.close()
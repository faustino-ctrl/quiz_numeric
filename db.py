import os
import mysql.connector

def conectar_banco():
    return mysql.connector.connect(
        host=os.environ.get("DB_HOST", "localhost"),
        user=os.environ.get("DB_USER", "root"),
        password=os.environ.get("DB_PASSWORD", ""),
        database=os.environ.get("DB_NAME", "quiz_guias")
    )


def buscar_perguntas():
    conexao = conectar_banco()
    cursor = conexao.cursor(dictionary=True)

    cursor.execute("SELECT id, pergunta, resposta_correta FROM perguntas")
    perguntas = cursor.fetchall()

    cursor.close()
    conexao.close()

    return perguntas
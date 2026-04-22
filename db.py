import os
import mysql.connector

def conectar_banco():
    return mysql.connector.connect(
        host=os.environ.get("DB_HOST", "localhost"),
        user=os.environ.get("DB_USER", "root"),
        password=os.environ.get("DB_PASSWORD", ""),
        database=os.environ.get("DB_NAME", "quiz_guias")
    )

def buscar_perguntas(tema=None, dificuldade=None):
    conexao = conectar_banco()
    cursor = conexao.cursor(dictionary=True)

    sql = """
        SELECT id, tema, dificuldade, pergunta, resposta_correta
        FROM perguntas
        WHERE 1=1
    """
    valores = []

    if tema and tema.lower() != "aleatorio":
        sql += " AND tema = %s"
        valores.append(tema)

    if dificuldade and dificuldade.lower() != "aleatorio":
        sql += " AND dificuldade = %s"
        valores.append(dificuldade)

    cursor.execute(sql, valores)
    perguntas = cursor.fetchall()

    cursor.close()
    conexao.close()

    return perguntas
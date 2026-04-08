import mysql.connector


def conectar_banco():
    return mysql.connector.connect(
        host="localhost",
        user="root",
        password="",
        database="quiz_guias"
    )


def buscar_perguntas():
    conexao = conectar_banco()
    cursor = conexao.cursor(dictionary=True)

    cursor.execute("SELECT id, pergunta, resposta_correta FROM perguntas")
    perguntas = cursor.fetchall()

    cursor.close()
    conexao.close()

    return perguntas
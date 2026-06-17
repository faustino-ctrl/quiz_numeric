import os
import random
import uuid

import mysql.connector
from flask import Flask, render_template, request, redirect, url_for, session
from flask_socketio import SocketIO, emit, join_room, leave_room
from werkzeug.security import generate_password_hash, check_password_hash

from db import buscar_perguntas


app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "sua_chave_secreta_aqui_123")

# Para Gunicorn + Socket.IO, use:
# gunicorn --worker-class eventlet -w 1 app:app
socketio = SocketIO(app, async_mode=None, cors_allowed_origins="*")

salas = {}
player_sid_map = {}

TOTAL_RODADAS = 7
TEMPO_POR_TURNO = 15


# =========================
# FUNÇÕES AUXILIARES
# =========================
def gerar_codigo_sala():
    while True:
        codigo = str(random.randint(10000, 99999))
        if codigo not in salas:
            return codigo


def gerar_token_jogador():
    return str(uuid.uuid4())


def conectar_banco():
    return mysql.connector.connect(
        host=os.environ.get("DB_HOST", "localhost"),
        user=os.environ.get("DB_USER", "root"),
        password=os.environ.get("DB_PASSWORD", ""),
        database=os.environ.get("DB_NAME", "quiz_guias")
    )


def montar_ranking(codigo):
    sala = salas[codigo]
    ranking = []

    for token, dados in sala["jogadores"].items():
        ranking.append({
            "nome": dados["nome"],
            "pontos": dados["pontos"]
        })

    ranking.sort(key=lambda item: item["pontos"], reverse=True)
    return ranking


def obter_turno_token(sala):
    if not sala["em_jogo"] or not sala["ordem_turnos"]:
        return ""

    if sala["indice_turno"] >= len(sala["ordem_turnos"]):
        sala["indice_turno"] = 0

    return sala["ordem_turnos"][sala["indice_turno"]]


def serializar_sala(codigo):
    sala = salas[codigo]
    turno_token = obter_turno_token(sala)
    turno_nome = sala["jogadores"][turno_token]["nome"] if turno_token in sala["jogadores"] else ""

    return {
        "codigo": codigo,
        "criador_token": sala["criador_token"],
        "criador_nome": sala["jogadores"][sala["criador_token"]]["nome"],
        "tema": sala["tema"],
        "dificuldade": sala["dificuldade"],
        "jogadores": [
            {
                "token": token,
                "nome": dados["nome"],
                "pontos": dados["pontos"],
                "ordem_chegada": dados["ordem_chegada"]
            }
            for token, dados in sala["jogadores"].items()
        ],
        "mensagens": sala["mensagens"],
        "em_jogo": sala["em_jogo"],
        "rodada_atual": sala["rodada_atual"],
        "total_rodadas": TOTAL_RODADAS,
        "fase": sala["fase"],
        "tempo_por_turno": TEMPO_POR_TURNO,
        "pergunta_atual": sala["pergunta_atual"]["pergunta"] if sala["pergunta_atual"] else "",
        "historico_respostas": sala["historico_respostas"] if sala["em_jogo"] else [],
        "turno_token": turno_token,
        "turno_nome": turno_nome,
        "aviso_rodada": sala.get("aviso_rodada", ""),
        "resposta_revelada": sala.get("resposta_revelada"),
        "ranking": montar_ranking(codigo) if sala["mostrar_ranking_final"] else [],
        "mostrar_ranking_final": sala["mostrar_ranking_final"]
    }


def emitir_estado_sala(codigo):
    if codigo not in salas:
        return

    emit("estado_sala", serializar_sala(codigo), room=codigo)


def escolher_pergunta(codigo):
    sala = salas[codigo]
    ids_usados = set(sala["perguntas_usadas"])

    disponiveis = [
        pergunta for pergunta in sala["banco_perguntas"]
        if pergunta["id"] not in ids_usados
    ]

    if not disponiveis:
        return None

    pergunta = random.choice(disponiveis)
    sala["perguntas_usadas"].append(pergunta["id"])
    return pergunta


def ordenar_tokens_por_chegada(sala):
    tokens = list(sala["jogadores"].keys())
    tokens.sort(key=lambda token: sala["jogadores"][token]["ordem_chegada"])
    return tokens


def rotacionar_lista(lista, posicao):
    if not lista:
        return []
    posicao = posicao % len(lista)
    return lista[posicao:] + lista[:posicao]


def preparar_ordem_turnos(codigo):
    """
    Ordem refinada:
    - Rodada 1: ordem de chegada.
    - Rodadas seguintes: rotação regular pela ordem de chegada.
    - Se a nova rodada começaria com o mesmo jogador que acabou de responder,
      a lista é rotacionada mais uma vez para evitar duas respostas seguidas.
    """
    sala = salas[codigo]
    tokens = ordenar_tokens_por_chegada(sala)

    if len(tokens) <= 1:
        sala["ordem_turnos"] = tokens
        sala["indice_turno"] = 0
        return

    if sala["rodada_atual"] == 1 and sala["fase"] == "normal":
        nova_ordem = tokens
    else:
        deslocamento = (sala["rodada_atual"] - 1) % len(tokens)
        nova_ordem = rotacionar_lista(tokens, deslocamento)

    ultimo = sala.get("ultimo_respondente_token", "")
    if ultimo and len(nova_ordem) > 1 and nova_ordem[0] == ultimo:
        nova_ordem = rotacionar_lista(nova_ordem, 1)

    sala["ordem_turnos"] = nova_ordem
    sala["indice_turno"] = 0


def iniciar_rodada(codigo):
    sala = salas[codigo]

    pergunta = escolher_pergunta(codigo)
    if pergunta is None:
        sala["mensagens"].append("Não há perguntas suficientes no banco de dados.")
        sala["em_jogo"] = False
        sala["mostrar_ranking_final"] = True
        emitir_estado_sala(codigo)
        return

    sala["pergunta_atual"] = pergunta
    sala["historico_respostas"] = []
    sala["ultimas_respostas"] = {}
    sala["aviso_rodada"] = ""
    sala["resposta_revelada"] = None
    preparar_ordem_turnos(codigo)

    sala["mensagens"].append(f"Rodada {sala['rodada_atual']} iniciada.")
    emitir_estado_sala(codigo)


def frase_por_proximidade(resposta, correta):
    diferenca = abs(correta - resposta)
    base = max(abs(correta), 1)
    proporcao = diferenca / base
    direcao = "alto" if resposta > correta else "baixo"

    muito_quente = [
        "Está mais quente que vulcão!",
        "Quase queimou a resposta!",
        "Está fervendo de tão perto!",
        "Passou raspando!",
        "Você está praticamente em cima da resposta!"
    ]
    quente = [
        "Está quente!",
        "Boa tentativa, está chegando!",
        "O caminho está certo!",
        "Está perto, ajusta só um pouco!",
        "Essa foi boa!"
    ]
    morno = [
        "Está morno.",
        "Não está longe, mas ainda precisa ajustar.",
        "Dá para melhorar esse chute.",
        "Você está no meio do caminho.",
        "Ainda tem jogo!"
    ]
    frio = [
        "Está frio!",
        "A resposta está longe daí.",
        "Chute arriscado!",
        "Você se afastou bastante.",
        "Pensa melhor no próximo número."
    ]
    congelando = [
        "Está congelando!",
        "Você está perdido?",
        "Essa passou longe!",
        "O mapa da resposta ficou para trás.",
        "Esse número viajou demais!"
    ]

    if diferenca == 0:
        return "Acertou em cheio!"

    if proporcao <= 0.05 or diferenca <= 2:
        return random.choice(muito_quente)

    if proporcao <= 0.15 or diferenca <= 5:
        return random.choice(quente)

    if proporcao <= 0.35 or diferenca <= 15:
        if direcao == "alto":
            return random.choice(morno + ["Chutou um pouco alto."])
        return random.choice(morno + ["Chutou um pouco baixo."])

    if proporcao <= 0.75:
        if direcao == "alto":
            return random.choice(frio + ["Chutou muito alto!"])
        return random.choice(frio + ["Chutou muito baixo!"])

    if direcao == "alto":
        return random.choice(congelando + ["Chutou alto demais!"])
    return random.choice(congelando + ["Chutou baixo demais!"])


def calcular_pontos(resposta_jogador, resposta_correta):
    """
    Pontuação refinada:
    - Só é aplicada quando alguém acerta a rodada.
    - Quem acerta recebe 100.
    - Os demais recebem pontos pela proximidade da última resposta enviada.
    - Quem não respondeu na rodada recebe 0.
    """
    diferenca = abs(resposta_correta - resposta_jogador)
    base = max(abs(resposta_correta), 1)
    proporcao = diferenca / base

    if diferenca == 0:
        return 100
    if proporcao <= 0.05 or diferenca <= 2:
        return 80
    if proporcao <= 0.10 or diferenca <= 5:
        return 65
    if proporcao <= 0.25 or diferenca <= 15:
        return 45
    if proporcao <= 0.50:
        return 25
    if proporcao <= 1.00:
        return 10
    return 5


def avancar_turno(codigo):
    sala = salas[codigo]

    if not sala["ordem_turnos"]:
        return

    sala["indice_turno"] += 1

    if sala["indice_turno"] >= len(sala["ordem_turnos"]):
        sala["indice_turno"] = 0
        sala["mensagens"].append("Todos tentaram. A mesma pergunta continua até alguém acertar.")

    turno_token = obter_turno_token(sala)
    ultimo = sala.get("ultimo_respondente_token", "")

    if len(sala["ordem_turnos"]) > 1 and turno_token == ultimo:
        sala["indice_turno"] = (sala["indice_turno"] + 1) % len(sala["ordem_turnos"])


def verificar_empate_no_topo(codigo):
    ranking = montar_ranking(codigo)
    if len(ranking) < 2:
        return False
    return ranking[0]["pontos"] == ranking[1]["pontos"]


def finalizar_ou_continuar(codigo):
    sala = salas[codigo]

    if sala["fase"] == "normal":
        if sala["rodada_atual"] < TOTAL_RODADAS:
            sala["rodada_atual"] += 1
            iniciar_rodada(codigo)
            return

        if verificar_empate_no_topo(codigo):
            sala["fase"] = "desempate"
            sala["rodadas_desempate_no_bloco"] = 0
            sala["rodada_atual"] += 1
            sala["mensagens"].append("Empate no topo. Começando desempate com 2 rodadas extras.")
            iniciar_rodada(codigo)
            return

        sala["em_jogo"] = False
        sala["mostrar_ranking_final"] = True
        sala["mensagens"].append("Partida encerrada sem empate.")
        emitir_estado_sala(codigo)
        return

    sala["rodadas_desempate_no_bloco"] += 1

    if sala["rodadas_desempate_no_bloco"] < 2:
        sala["rodada_atual"] += 1
        iniciar_rodada(codigo)
        return

    if verificar_empate_no_topo(codigo):
        sala["rodadas_desempate_no_bloco"] = 0
        sala["rodada_atual"] += 1
        sala["mensagens"].append("Ainda houve empate. Abrindo mais 2 rodadas de prorrogação.")
        iniciar_rodada(codigo)
        return

    sala["em_jogo"] = False
    sala["mostrar_ranking_final"] = True
    sala["mensagens"].append("Desempate encerrado. Partida finalizada.")
    emitir_estado_sala(codigo)


def buscar_usuario_por_username(username):
    conexao = conectar_banco()
    cursor = conexao.cursor(dictionary=True)

    sql = "SELECT * FROM usuarios WHERE BINARY username = %s"
    cursor.execute(sql, (username,))
    usuario = cursor.fetchone()

    cursor.close()
    conexao.close()
    return usuario


def buscar_usuario_por_email(email):
    conexao = conectar_banco()
    cursor = conexao.cursor(dictionary=True)

    sql = "SELECT * FROM usuarios WHERE email = %s"
    cursor.execute(sql, (email,))
    usuario = cursor.fetchone()

    cursor.close()
    conexao.close()
    return usuario


def criar_usuario(username, email, senha):
    conexao = conectar_banco()
    cursor = conexao.cursor()

    senha_hash = generate_password_hash(senha)

    sql = """
        INSERT INTO usuarios (username, email, senha_hash)
        VALUES (%s, %s, %s)
    """
    cursor.execute(sql, (username, email, senha_hash))
    conexao.commit()

    cursor.close()
    conexao.close()


def atualizar_senha_por_email(email, nova_senha):
    conexao = conectar_banco()
    cursor = conexao.cursor()

    nova_hash = generate_password_hash(nova_senha)

    sql = "UPDATE usuarios SET senha_hash = %s WHERE email = %s"
    cursor.execute(sql, (nova_hash, email))
    conexao.commit()

    cursor.close()
    conexao.close()


# =========================
# ROTAS HTTP
# =========================
@app.route("/")
def index():
    if "usuario_id" in session:
        return render_template("auth_inicio.html", usuario_logado=session["username"])
    return render_template("auth_inicio.html", usuario_logado=None)


@app.route("/lobby")
def lobby():
    if "usuario_id" not in session:
        return redirect(url_for("login"))
    return render_template("lobby.html", nome=session["username"])


@app.route("/configurar_partida")
def configurar_partida():
    if "usuario_id" not in session:
        return redirect(url_for("login"))
    return render_template("configurar_partida.html", nome=session["username"])


@app.route("/criar_sala", methods=["POST"])
def criar_sala():
    if "usuario_id" not in session:
        return redirect(url_for("login"))

    nome = session["username"]
    tema = request.form.get("tema", "aleatorio").strip()
    dificuldade = request.form.get("dificuldade", "aleatorio").strip()

    codigo = gerar_codigo_sala()
    token = gerar_token_jogador()
    perguntas = buscar_perguntas(tema, dificuldade)

    if len(perguntas) == 0:
        return render_template(
            "configurar_partida.html",
            nome=nome,
            erro="Não há perguntas cadastradas para esse tema e dificuldade."
        )

    salas[codigo] = {
        "criador_token": token,
        "tema": tema,
        "dificuldade": dificuldade,
        "jogadores": {
            token: {
                "nome": nome,
                "pontos": 0,
                "ordem_chegada": 1
            }
        },
        "mensagens": [f"{nome} criou a sala."],
        "em_jogo": False,
        "rodada_atual": 0,
        "fase": "espera",
        "pergunta_atual": None,
        "historico_respostas": [],
        "ultimas_respostas": {},
        "ordem_turnos": [],
        "indice_turno": 0,
        "ultimo_respondente_token": "",
        "banco_perguntas": perguntas,
        "perguntas_usadas": [],
        "mostrar_ranking_final": False,
        "rodadas_desempate_no_bloco": 0,
        "aviso_rodada": "",
        "resposta_revelada": None
    }

    return render_template("sala.html", nome=nome, codigo=codigo, token=token, sala=salas[codigo])


@app.route("/pagina_entrar_sala")
def pagina_entrar_sala():
    if "usuario_id" not in session:
        return redirect(url_for("login"))
    return render_template("entrar_sala.html", nome=session["username"])


@app.route("/entrar_sala", methods=["POST"])
def entrar_sala():
    if "usuario_id" not in session:
        return redirect(url_for("login"))

    nome = session["username"]
    codigo = request.form.get("codigo", "").strip()

    if codigo == "":
        return render_template("entrar_sala.html", nome=nome, erro="Digite o código da sala.")

    if codigo not in salas:
        return render_template("entrar_sala.html", nome=nome, erro="Sala não encontrada.")

    sala = salas[codigo]

    if sala["em_jogo"]:
        return render_template("entrar_sala.html", nome=nome, erro="A partida já começou nesta sala.")

    for token_existente, dados in sala["jogadores"].items():
        if dados["nome"] == nome:
            return render_template("sala.html", nome=nome, codigo=codigo, token=token_existente, sala=sala)

    token = gerar_token_jogador()
    ordem_chegada = len(sala["jogadores"]) + 1

    sala["jogadores"][token] = {
        "nome": nome,
        "pontos": 0,
        "ordem_chegada": ordem_chegada
    }

    sala["mensagens"].append(f"{nome} entrou na sala.")
    return render_template("sala.html", nome=nome, codigo=codigo, token=token, sala=sala)


@app.route("/cadastro")
def cadastro():
    return render_template("cadastro.html")


@app.route("/cadastrar", methods=["POST"])
def cadastrar():
    username = request.form.get("username", "").strip()
    email = request.form.get("email", "").strip()
    senha = request.form.get("senha", "").strip()

    if username == "" or email == "" or senha == "":
        return render_template("cadastro.html", erro="Preencha todos os campos.")

    if len(username) > 12:
        return render_template("cadastro.html", erro="O nome de usuário pode ter no máximo 12 caracteres.")

    if len(senha) < 6:
        return render_template("cadastro.html", erro="A senha deve ter pelo menos 6 caracteres.")

    usuario_existente = buscar_usuario_por_username(username)
    if usuario_existente:
        return render_template("cadastro.html", erro="Esse nome de usuário já está em uso.")

    email_existente = buscar_usuario_por_email(email)
    if email_existente:
        return render_template("cadastro.html", erro="Esse email já está cadastrado.")

    criar_usuario(username, email, senha)
    return render_template("login.html", sucesso="Cadastro realizado com sucesso. Faça login.")


@app.route("/login")
def login():
    return render_template("login.html")


@app.route("/logar", methods=["POST"])
def logar():
    username = request.form.get("username", "").strip()
    senha = request.form.get("senha", "").strip()

    if username == "" or senha == "":
        return render_template("login.html", erro="Preencha usuário e senha.")

    usuario = buscar_usuario_por_username(username)

    if not usuario:
        return render_template("login.html", erro="Usuário não encontrado.")

    if not check_password_hash(usuario["senha_hash"], senha):
        return render_template("login.html", erro="Senha incorreta.")

    session.clear()
    session["usuario_id"] = usuario["id"]
    session["username"] = usuario["username"]

    return redirect(url_for("lobby"))


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))


@app.route("/recuperar")
def recuperar():
    return render_template("recuperar.html")


@app.route("/buscar_conta", methods=["POST"])
def buscar_conta():
    email = request.form.get("email", "").strip()

    if email == "":
        return render_template("recuperar.html", erro="Digite seu email.")

    usuario = buscar_usuario_por_email(email)

    if not usuario:
        return render_template("recuperar.html", erro="Nenhuma conta foi encontrada com esse email.")

    return render_template("redefinir_senha.html", email=email, username=usuario["username"])


@app.route("/redefinir_senha", methods=["POST"])
def redefinir_senha():
    email = request.form.get("email", "").strip()
    nova_senha = request.form.get("nova_senha", "").strip()

    if email == "" or nova_senha == "":
        return render_template("recuperar.html", erro="Preencha os campos corretamente.")

    if len(nova_senha) < 6:
        usuario = buscar_usuario_por_email(email)
        username = usuario["username"] if usuario else ""
        return render_template(
            "redefinir_senha.html",
            email=email,
            username=username,
            erro="A nova senha deve ter pelo menos 6 caracteres."
        )

    atualizar_senha_por_email(email, nova_senha)
    return render_template("login.html", sucesso="Senha redefinida com sucesso.")


@app.route("/trocar_conta")
def trocar_conta():
    session.clear()
    return redirect(url_for("login"))


@app.route("/sobre")
def sobre():
    return render_template("sobre.html")


# =========================
# SOCKET.IO
# =========================
@socketio.on("connect")
def ao_conectar():
    pass


@socketio.on("entrar_socket_sala")
def entrar_socket_sala(dados):
    codigo = dados.get("codigo")
    token = dados.get("token")

    if codigo not in salas:
        return

    if token not in salas[codigo]["jogadores"]:
        return

    join_room(codigo)
    player_sid_map[token] = request.sid
    emitir_estado_sala(codigo)


@socketio.on("iniciar_partida")
def iniciar_partida_socket(dados):
    codigo = dados.get("codigo")
    token = dados.get("token")

    if codigo not in salas:
        return

    sala = salas[codigo]

    if token != sala["criador_token"]:
        emit("erro_jogo", {"mensagem": "Somente a criadora pode iniciar a partida."}, room=request.sid)
        return

    if len(sala["jogadores"]) < 2:
        emit("erro_jogo", {"mensagem": "É necessário pelo menos 2 jogadores para iniciar."}, room=request.sid)
        return

    for token_jogador in sala["jogadores"]:
        sala["jogadores"][token_jogador]["pontos"] = 0

    sala["em_jogo"] = True
    sala["rodada_atual"] = 1
    sala["fase"] = "normal"
    sala["pergunta_atual"] = None
    sala["historico_respostas"] = []
    sala["ultimas_respostas"] = {}
    sala["ordem_turnos"] = []
    sala["indice_turno"] = 0
    sala["ultimo_respondente_token"] = ""
    sala["perguntas_usadas"] = []
    sala["mostrar_ranking_final"] = False
    sala["rodadas_desempate_no_bloco"] = 0
    sala["aviso_rodada"] = ""
    sala["resposta_revelada"] = None
    sala["mensagens"].append("A partida começou!")

    iniciar_rodada(codigo)


@socketio.on("enviar_resposta")
def enviar_resposta(dados):
    codigo = dados.get("codigo")
    token = dados.get("token")
    resposta_texto = str(dados.get("resposta", "")).strip()

    if codigo not in salas:
        return

    sala = salas[codigo]

    if not sala["em_jogo"] or token not in sala["jogadores"]:
        return

    if resposta_texto == "":
        emit("erro_jogo", {"mensagem": "Digite uma resposta."}, room=request.sid)
        return

    if not resposta_texto.lstrip("-").isdigit():
        emit("erro_jogo", {"mensagem": "A resposta deve ser numérica inteira."}, room=request.sid)
        return

    turno_token = obter_turno_token(sala)

    if token != turno_token:
        emit("erro_jogo", {"mensagem": "Ainda não é a sua vez."}, room=request.sid)
        return

    resposta = int(resposta_texto)
    correta = int(sala["pergunta_atual"]["resposta_correta"])
    nome_jogador = sala["jogadores"][token]["nome"]

    sala["ultimo_respondente_token"] = token
    sala["ultimas_respostas"][token] = resposta

    if resposta == correta:
        sala["aviso_rodada"] = f"{nome_jogador} acertou a resposta! {correta}"
        sala["resposta_revelada"] = correta

        novo_historico = []
        for token_jogador, dados_jogador in sala["jogadores"].items():
            if token_jogador in sala["ultimas_respostas"]:
                ultima_resposta = sala["ultimas_respostas"][token_jogador]
                pontos = calcular_pontos(ultima_resposta, correta)
                dados_jogador["pontos"] += pontos

                novo_historico.append({
                    "nome": dados_jogador["nome"],
                    "resposta": ultima_resposta,
                    "pontos_recebidos": pontos,
                    "frase": "Acertou em cheio!" if ultima_resposta == correta else frase_por_proximidade(ultima_resposta, correta),
                    "acertou": ultima_resposta == correta
                })
            else:
                novo_historico.append({
                    "nome": dados_jogador["nome"],
                    "resposta": None,
                    "pontos_recebidos": 0,
                    "frase": "Não respondeu nesta rodada.",
                    "acertou": False
                })

        sala["historico_respostas"] = novo_historico
        emitir_estado_sala(codigo)

        socketio.sleep(3)
        if codigo in salas:
            finalizar_ou_continuar(codigo)
        return

    sala["historico_respostas"].append({
        "nome": nome_jogador,
        "resposta": resposta,
        "pontos_recebidos": 0,
        "frase": frase_por_proximidade(resposta, correta),
        "acertou": False
    })

    avancar_turno(codigo)
    emitir_estado_sala(codigo)


@socketio.on("tempo_esgotado")
def tempo_esgotado(dados):
    codigo = dados.get("codigo")
    token = dados.get("token")

    if codigo not in salas:
        return

    sala = salas[codigo]

    if not sala["em_jogo"] or token not in sala["jogadores"]:
        return

    turno_token = obter_turno_token(sala)

    if token != turno_token:
        return

    nome_jogador = sala["jogadores"][token]["nome"]
    sala["ultimo_respondente_token"] = token
    sala["historico_respostas"].append({
        "nome": nome_jogador,
        "resposta": None,
        "pontos_recebidos": 0,
        "frase": "O tempo acabou!",
        "acertou": False
    })

    avancar_turno(codigo)
    emitir_estado_sala(codigo)


@socketio.on("voltar_para_espera")
def voltar_para_espera(dados):
    codigo = dados.get("codigo")

    if codigo not in salas:
        return

    sala = salas[codigo]
    sala["em_jogo"] = False
    sala["rodada_atual"] = 0
    sala["fase"] = "espera"
    sala["pergunta_atual"] = None
    sala["historico_respostas"] = []
    sala["ultimas_respostas"] = {}
    sala["ordem_turnos"] = []
    sala["indice_turno"] = 0
    sala["ultimo_respondente_token"] = ""
    sala["mostrar_ranking_final"] = False
    sala["rodadas_desempate_no_bloco"] = 0
    sala["aviso_rodada"] = ""
    sala["resposta_revelada"] = None
    sala["mensagens"].append("Os jogadores voltaram para a sala de espera.")
    emitir_estado_sala(codigo)


@socketio.on("sair_da_sala")
def sair_da_sala_socket(dados):
    codigo = dados.get("codigo")
    token = dados.get("token")

    if codigo not in salas:
        return

    sala = salas[codigo]

    if token not in sala["jogadores"]:
        return

    nome = sala["jogadores"][token]["nome"]

    leave_room(codigo)
    player_sid_map.pop(token, None)
    del sala["jogadores"][token]

    if len(sala["jogadores"]) == 0:
        del salas[codigo]
        return

    if sala["em_jogo"]:
        sala["mensagens"].append(f"{nome} saiu da partida.")
    else:
        sala["mensagens"].append(f"{nome} saiu da sala.")

    if sala["criador_token"] == token:
        novo_criador = list(sala["jogadores"].keys())[0]
        sala["criador_token"] = novo_criador
        sala["mensagens"].append(f"{sala['jogadores'][novo_criador]['nome']} agora é a pessoa criadora da sala.")

    if sala["em_jogo"] and sala["ordem_turnos"]:
        if token in sala["ordem_turnos"]:
            indice_removido = sala["ordem_turnos"].index(token)
            sala["ordem_turnos"].remove(token)

            if len(sala["ordem_turnos"]) == 0:
                sala["em_jogo"] = False
                sala["mostrar_ranking_final"] = True
            else:
                if indice_removido <= sala["indice_turno"] and sala["indice_turno"] > 0:
                    sala["indice_turno"] -= 1

                if sala["indice_turno"] >= len(sala["ordem_turnos"]):
                    sala["indice_turno"] = 0

    emitir_estado_sala(codigo)


if __name__ == "__main__":
    socketio.run(app, debug=True)

import os
import random
import uuid
import secrets
import hmac
import re
from functools import wraps
from urllib.parse import urlencode

from authlib.integrations.flask_client import OAuth
from dotenv import load_dotenv

import mysql.connector
from flask import Flask, render_template, request, redirect, url_for, session, abort
from flask_socketio import SocketIO, emit, join_room, leave_room

from db import buscar_perguntas


load_dotenv()

# Configure estas variáveis no arquivo .env (nunca as publique no GitHub).
AUTH0_DOMAIN = os.environ.get("AUTH0_DOMAIN", "").strip().removeprefix("https://").rstrip("/")
AUTH0_CLIENT_ID = os.environ.get("AUTH0_CLIENT_ID", "").strip()
AUTH0_CLIENT_SECRET = os.environ.get("AUTH0_CLIENT_SECRET", "").strip()
APP_BASE_URL = os.environ.get("APP_BASE_URL", "http://localhost:5000").rstrip("/")
SECRET_KEY = os.environ.get("SECRET_KEY", "")

if not all((AUTH0_DOMAIN, AUTH0_CLIENT_ID, AUTH0_CLIENT_SECRET, SECRET_KEY)):
    raise RuntimeError("Configure AUTH0_DOMAIN, AUTH0_CLIENT_ID, AUTH0_CLIENT_SECRET e SECRET_KEY no .env")

app = Flask(__name__)

app.config.update(
    SECRET_KEY=SECRET_KEY,
    SESSION_COOKIE_NAME="quiz_numerico_session",
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=APP_BASE_URL.startswith("https://"),
    SESSION_COOKIE_DOMAIN=None,
    SESSION_COOKIE_PATH="/",
)

oauth = OAuth(app)
oauth.register(
    "auth0",
    client_id=AUTH0_CLIENT_ID,
    client_secret=AUTH0_CLIENT_SECRET,
    server_metadata_url=f"https://{AUTH0_DOMAIN}/.well-known/openid-configuration",
    client_kwargs={"scope": "openid profile email"},
)

# Para Gunicorn + Socket.IO no Google Cloud, use:
# gunicorn --worker-class eventlet -w 1 app:app
# (Ou defina SOCKETIO_ASYNC_MODE=threading localmente se desejar forçar threading)
socketio = SocketIO(app, async_mode=os.environ.get("SOCKETIO_ASYNC_MODE") or None)


@app.context_processor
def injetar_csrf():
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_urlsafe(32)
    return {"csrf_token": session["csrf_token"]}


def validar_csrf():
    recebido = request.form.get("csrf_token", "")
    esperado = session.get("csrf_token", "")
    if not esperado or not hmac.compare_digest(recebido, esperado):
        abort(400, description="Token CSRF inválido. Atualize a página e tente novamente.")


def exigir_login(func):
    @wraps(func)
    def protegido(*args, **kwargs):
        if not session.get("auth0_sub"):
            return redirect(url_for("login"))
        if not session.get("usuario_id") or not session.get("username"):
            return redirect(url_for("configurar_usuario"))
        return func(*args, **kwargs)
    return protegido


def jogador_autorizado(codigo, token):
    if codigo not in salas or not token or not session.get("auth0_sub"):
        return False
    dados = salas[codigo]["jogadores"].get(token)
    return bool(dados and dados.get("auth0_sub") == session["auth0_sub"])

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


def buscar_perfil_por_sub(auth0_sub):
    conexao = conectar_banco()
    cursor = conexao.cursor(dictionary=True)
    try:
        cursor.execute("SELECT id, username FROM perfis_auth0 WHERE auth0_sub = %s", (auth0_sub,))
        return cursor.fetchone()
    finally:
        cursor.close()
        conexao.close()


def buscar_email_usuario_legado(username):
    """Reserva nomes de usuários antigos; apenas o dono do e-mail verificado pode reutilizá-los."""
    conexao = conectar_banco()
    cursor = conexao.cursor(dictionary=True)
    try:
        cursor.execute("SELECT email FROM usuarios WHERE BINARY username = %s", (username,))
        usuario = cursor.fetchone()
        return usuario["email"] if usuario else None
    finally:
        cursor.close()
        conexao.close()


def criar_perfil(auth0_sub, username, email):
    conexao = conectar_banco()
    cursor = conexao.cursor()
    try:
        cursor.execute(
            "INSERT INTO perfis_auth0 (auth0_sub, username, email) VALUES (%s, %s, %s)",
            (auth0_sub, username, email),
        )
        conexao.commit()
    finally:
        cursor.close()
        conexao.close()


# =========================
# AUTENTICAÇÃO VIA AUTH0
# =========================
@app.route("/")
def index():
    return render_template("auth_inicio.html", usuario_logado=session.get("username"))



def limpar_estados_auth0():
    # Remove estados pendentes de tentativas anteriores.
    # Um novo login iniciado nesta sessão invalida os anteriores.
    for chave in list(session.keys()):
        if chave.startswith("_state_auth0_"):
            session.pop(chave, None)


@app.route("/login")
def login():
    limpar_estados_auth0()

    callback_url = url_for("callback", _external=True)

    print("INICIO LOGIN:", request.host_url, flush=True)
    print("CALLBACK URL:", callback_url, flush=True)

    resposta = oauth.auth0.authorize_redirect(callback_url)

    print(
        "ESTADOS CRIADOS:",
        sum(
            chave.startswith("_state_auth0_")
            for chave in session.keys()
        ),
        flush=True
    )

    return resposta


@app.route("/cadastro")
def cadastro():
    limpar_estados_auth0()

    callback_url = url_for("callback", _external=True)

    return oauth.auth0.authorize_redirect(
        callback_url,
        screen_hint="signup",
        prompt="login"
    )


@app.route("/callback")
def callback():
    print("Host:", request.host)
    print("Cookie de sessão recebido:",
      "quiz_numerico_session" in request.cookies)
    print("Estados Auth0 na sessão:",
      len([k for k in session.keys()
           if k.startswith("_state_auth0_")]))
    try:
        token = oauth.auth0.authorize_access_token()
        # userinfo vem de um ID token validado pela biblioteca Authlib.
        usuario_auth0 = token.get("userinfo") or {}
        sub = usuario_auth0.get("sub", "")
        if not sub:
            raise ValueError("Resposta de autenticação sem identificador do usuário")
    except Exception as erro:
        import traceback

        print("\n========== ERRO AUTH0 ==========", flush=True)
        print("Tipo:", type(erro).__name__, flush=True)
        print("Mensagem:", str(erro), flush=True)
        traceback.print_exc()
        print("===============================\n", flush=True)

        return "Falha na autenticação. Consulte o terminal.", 400
    # Não persistir access tokens, ID tokens ou senhas no cookie de sessão.
    session.clear()
    session["auth0_sub"] = sub
    session["auth0_email"] = (usuario_auth0.get("email") or "")[:255]
    session["auth0_email_verified"] = usuario_auth0.get("email_verified") is True

    perfil = buscar_perfil_por_sub(sub)
    if perfil:
        session["usuario_id"] = perfil["id"]
        session["username"] = perfil["username"]
        return redirect(url_for("lobby"))
    return redirect(url_for("configurar_usuario"))


@app.route("/configurar_usuario", methods=["GET", "POST"])
def configurar_usuario():
    if not session.get("auth0_sub"):
        return redirect(url_for("login"))
    if session.get("usuario_id"):
        return redirect(url_for("lobby"))

    if request.method == "POST":
        validar_csrf()
        username = request.form.get("username", "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_]{5,12}", username, flags=re.ASCII):
            return render_template(
                "configurar_usuario.html",
                erro="Use de 5 a 12 caracteres: letras, números ou sublinhado (_).",
            )
        email_antigo = buscar_email_usuario_legado(username)
        if email_antigo and not (
            session.get("auth0_email_verified")
            and email_antigo.casefold() == (session.get("auth0_email") or "").casefold()
        ):
            return render_template(
                "configurar_usuario.html",
                erro="Nome reservado por uma conta antiga. Cadastre-se no Auth0 com o e-mail antigo e verifique-o para recuperá-lo.",
            )
        try:
            criar_perfil(session["auth0_sub"], username, session.get("auth0_email"))
        except mysql.connector.IntegrityError:
            # Pode ser usuário duplicado ou um POST repetido após a criação.
            perfil = buscar_perfil_por_sub(session["auth0_sub"])
            if perfil:
                session["usuario_id"] = perfil["id"]
                session["username"] = perfil["username"]
                return redirect(url_for("lobby"))
            return render_template("configurar_usuario.html", erro="Nome de usuário já está em uso.")
        perfil = buscar_perfil_por_sub(session["auth0_sub"])
        session["usuario_id"] = perfil["id"]
        session["username"] = perfil["username"]
        return redirect(url_for("lobby"))
    return render_template("configurar_usuario.html")


@app.route("/logout", methods=["POST"])
def logout():
    validar_csrf()
    session.clear()
    params = urlencode({"client_id": AUTH0_CLIENT_ID, "returnTo": APP_BASE_URL + "/"})
    return redirect(f"https://{AUTH0_DOMAIN}/v2/logout?{params}")


@app.route("/trocar_conta", methods=["POST"])
def trocar_conta():
    return logout()


@app.route("/recuperar")
def recuperar():
    # O formulário seguro 'Esqueci minha senha' está na tela Auth0.
    return redirect(url_for("login"))


@app.route("/sobre")
def sobre():
    return render_template("sobre.html")


# =========================
# ROTAS HTTP DO JOGO
# =========================
@app.route("/lobby")
@exigir_login
def lobby():
    return render_template("lobby.html", nome=session["username"])


@app.route("/configurar_partida")
@exigir_login
def configurar_partida():
    return render_template("configurar_partida.html", nome=session["username"])


@app.route("/criar_sala", methods=["POST"])
@exigir_login
def criar_sala():
    validar_csrf()
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
                "auth0_sub": session["auth0_sub"],
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
@exigir_login
def pagina_entrar_sala():
    return render_template("entrar_sala.html", nome=session["username"])


@app.route("/entrar_sala", methods=["POST"])
@exigir_login
def entrar_sala():
    validar_csrf()
    nome = session["username"]
    codigo = request.form.get("codigo", "").strip()
    if codigo == "":
        return render_template("entrar_sala.html", nome=nome, erro="Digite o código da sala.")
    if codigo not in salas:
        return render_template("entrar_sala.html", nome=nome, erro="Sala não encontrada.")

    sala = salas[codigo]
    for token_existente, dados in sala["jogadores"].items():
        if dados.get("auth0_sub") == session["auth0_sub"]:
            return render_template("sala.html", nome=nome, codigo=codigo, token=token_existente, sala=sala)

    if sala["em_jogo"]:
        return render_template("entrar_sala.html", nome=nome, erro="A partida já começou nesta sala.")

    token = gerar_token_jogador()
    ordem_chegada = max((d["ordem_chegada"] for d in sala["jogadores"].values()), default=0) + 1
    sala["jogadores"][token] = {
        "nome": nome,
        "auth0_sub": session["auth0_sub"],
        "pontos": 0,
        "ordem_chegada": ordem_chegada
    }
    sala["mensagens"].append(f"{nome} entrou na sala.")
    return render_template("sala.html", nome=nome, codigo=codigo, token=token, sala=sala)


# =========================
# SOCKET.IO
# =========================
@socketio.on("connect")
def ao_conectar(auth=None):
    if not session.get("auth0_sub") or not session.get("usuario_id"):
        return False


@socketio.on("entrar_socket_sala")
def entrar_socket_sala(dados):
    codigo = dados.get("codigo")
    token = dados.get("token")

    if codigo not in salas:
        return

    if not jogador_autorizado(codigo, token):
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

    if not jogador_autorizado(codigo, token):
        return

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

    if not sala["em_jogo"] or not jogador_autorizado(codigo, token):
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

    if not sala["em_jogo"] or not jogador_autorizado(codigo, token):
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
    if not jogador_autorizado(codigo, sala["criador_token"]):
        return
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

    if not jogador_autorizado(codigo, token):
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
    socketio.run(app, debug=os.environ.get("FLASK_DEBUG", "0") == "1")

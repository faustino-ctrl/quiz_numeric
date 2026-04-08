from flask import Flask, render_template, request, redirect, url_for
from flask_socketio import SocketIO, emit, join_room, leave_room
from db import buscar_perguntas
import random
import uuid

app = Flask(__name__)
app.config["SECRET_KEY"] = "guias_anonimas_2000"
socketio = SocketIO(app)

salas = {}
player_sid_map = {}

def gerar_codigo_sala():
    while True:
        codigo = str(random.randint(10000, 99999))
        if codigo not in salas:
            return codigo


def gerar_token_jogador():
    return str(uuid.uuid4())

def serializar_sala(codigo):
    sala = salas[codigo]

    return {
        "codigo": codigo,
        "criador_token": sala["criador_token"],
        "criador_nome": sala["jogadores"][sala["criador_token"]]["nome"],
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
        "fase": sala["fase"],
        "pergunta_atual": sala["pergunta_atual"]["pergunta"] if sala["pergunta_atual"] else "",
        "historico_respostas": sala["historico_respostas"] if sala["em_jogo"] else [],
        "turno_token": sala["ordem_turnos"][sala["indice_turno"]] if sala["em_jogo"] and sala["ordem_turnos"] else "",
        "turno_nome": sala["jogadores"][sala["ordem_turnos"][sala["indice_turno"]]]["nome"] if sala["em_jogo"] and sala["ordem_turnos"] else "",
        "ranking": montar_ranking(codigo) if sala["mostrar_ranking_final"] else [],
        "mostrar_ranking_final": sala["mostrar_ranking_final"]
    }

def emitir_estado_sala(codigo):
    if codigo not in salas:
        return

    emit("estado_sala", serializar_sala(codigo), room=codigo)

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

def escolher_pergunta(codigo):
    sala = salas[codigo]

    ids_usados = set(sala["perguntas_usadas"])
    disponiveis = []

    for pergunta in sala["banco_perguntas"]:
        if pergunta["id"] not in ids_usados:
            disponiveis.append(pergunta)

    if not disponiveis:
        return None

    pergunta = random.choice(disponiveis)
    sala["perguntas_usadas"].append(pergunta["id"])
    return pergunta

def preparar_ordem_turnos(codigo):
    sala = salas[codigo]
    tokens = list(sala["jogadores"].keys())

    if sala["rodada_atual"] == 1 and sala["fase"] == "normal":
        tokens.sort(key=lambda token: sala["jogadores"][token]["ordem_chegada"])
        sala["ordem_turnos"] = tokens
        sala["indice_turno"] = 0
        return

    candidatos = tokens[:]

    if len(tokens) > 1 and sala["ultimo_primeiro_token"] in candidatos:
        sem_ultimo = [token for token in candidatos if token != sala["ultimo_primeiro_token"]]
        primeiro = random.choice(sem_ultimo)
        restantes = [token for token in candidatos if token != primeiro]
        random.shuffle(restantes)
        nova_ordem = [primeiro] + restantes
    else:
        random.shuffle(candidatos)
        nova_ordem = candidatos

    sala["ordem_turnos"] = nova_ordem
    sala["indice_turno"] = 0
    sala["ultimo_primeiro_token"] = nova_ordem[0]

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
    preparar_ordem_turnos(codigo)

    sala["mensagens"].append(f"Rodada {sala['rodada_atual']} iniciada.")

    emitir_estado_sala(codigo)

def calcular_pontos(resposta_jogador, resposta_correta):
    diferenca = abs(resposta_correta - resposta_jogador)

    if diferenca == 0:
        return 100
    if diferenca <= 1:
        return 80
    if diferenca <= 3:
        return 60
    if diferenca <= 5:
        return 40
    if diferenca <= 10:
        return 20
    return 10

def avancar_turno(codigo):
    sala = salas[codigo]

    sala["indice_turno"] += 1

    if sala["indice_turno"] >= len(sala["ordem_turnos"]):
        sala["indice_turno"] = 0
        sala["mensagens"].append("Todas tentaram. A mesma pergunta continua até alguém acertar.")

def verificar_empate_no_topo(codigo):
    ranking = montar_ranking(codigo)

    if len(ranking) < 2:
        return False

    return ranking[0]["pontos"] == ranking[1]["pontos"]

def finalizar_ou_continuar(codigo):
    sala = salas[codigo]

    if sala["fase"] == "normal":
        if sala["rodada_atual"] < 7:
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

    # fase desempate
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


#ROTAS#

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/lobby", methods=["POST"])
def lobby():
    nome = request.form.get("nome", "").strip()

    if nome == "":
        return render_template("index.html", erro="Digite seu nome.")

    return render_template("lobby.html", nome=nome)


@app.route("/criar_sala", methods=["POST"])
def criar_sala():
    nome = request.form.get("nome", "").strip()

    if nome == "":
        return redirect(url_for("index"))

    codigo = gerar_codigo_sala()
    token = gerar_token_jogador()
    perguntas = buscar_perguntas()

    salas[codigo] = {
    "criador_token": token,
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
    "ultimo_primeiro_token": "",
    "banco_perguntas": perguntas,
    "perguntas_usadas": [],
    "mostrar_ranking_final": False,
    "rodadas_desempate_no_bloco": 0
}

    return render_template(
        "sala.html",
        nome=nome,
        codigo=codigo,
        token=token,
        sala=salas[codigo]
    )


@app.route("/pagina_entrar_sala", methods=["POST"])
def pagina_entrar_sala():
    nome = request.form.get("nome", "").strip()

    if nome == "":
        return redirect(url_for("index"))

    return render_template("entrar_sala.html", nome=nome)


@app.route("/entrar_sala", methods=["POST"])
def entrar_sala():
    nome = request.form.get("nome", "").strip()
    codigo = request.form.get("codigo", "").strip()

    if nome == "":
        return redirect(url_for("index"))

    if codigo == "":
        return render_template("entrar_sala.html", nome=nome, erro="Digite o código da sala.")

    if codigo not in salas:
        return render_template("entrar_sala.html", nome=nome, erro="Sala não encontrada.")

    sala = salas[codigo]

    if sala["em_jogo"]:
        return render_template("entrar_sala.html", nome=nome, erro="A partida já começou nesta sala.")

    token = gerar_token_jogador()
    ordem_chegada = len(sala["jogadores"]) + 1

    sala["jogadores"][token] = {
        "nome": nome,
        "pontos": 0,
        "ordem_chegada": ordem_chegada
    }
    sala["mensagens"].append(f"{nome} entrou na sala.")

    return render_template(
        "sala.html",
        nome=nome,
        codigo=codigo,
        token=token,
        sala=sala
    )


#CONEXÕES#

@socketio.on("connect")
def ao_conectar():
    pass


@socketio.on("entrar_socket_sala")
def entrar_socket_sala(dados):
    codigo = dados["codigo"]
    token = dados["token"]

    if codigo not in salas:
        return

    join_room(codigo)
    player_sid_map[token] = request.sid
    emitir_estado_sala(codigo)


@socketio.on("iniciar_partida")
def iniciar_partida_socket(dados):
    codigo = dados["codigo"]
    token = dados["token"]

    if codigo not in salas:
        return

    sala = salas[codigo]

    if token != sala["criador_token"]:
        emit("erro_jogo", {"mensagem": "Somente a criadora pode iniciar a partida."}, room=request.sid)
        return

    if len(sala["jogadores"]) < 2:
        emit("erro_jogo", {"mensagem": "É necessário pelo menos 2 jogadoras para iniciar."}, room=request.sid)
        return

    # reset da partida
    for token_jogadora in sala["jogadores"]:
        sala["jogadores"][token_jogadora]["pontos"] = 0

    sala["em_jogo"] = True
    sala["rodada_atual"] = 1
    sala["fase"] = "normal"
    sala["pergunta_atual"] = None
    sala["historico_respostas"] = []
    sala["ultimas_respostas"] = {}
    sala["ordem_turnos"] = []
    sala["indice_turno"] = 0
    sala["ultimo_primeiro_token"] = ""
    sala["perguntas_usadas"] = []
    sala["mostrar_ranking_final"] = False
    sala["rodadas_desempate_no_bloco"] = 0

    iniciar_rodada(codigo)


@socketio.on("enviar_resposta")
def enviar_resposta(dados):
    codigo = dados["codigo"]
    token = dados["token"]
    resposta_texto = str(dados["resposta"]).strip()

    if codigo not in salas:
        return

    sala = salas[codigo]

    if not sala["em_jogo"]:
        return

    if resposta_texto == "":
        emit("erro_jogo", {"mensagem": "Digite uma resposta."}, room=request.sid)
        return

    if not resposta_texto.lstrip("-").isdigit():
        emit("erro_jogo", {"mensagem": "A resposta deve ser numérica inteira."}, room=request.sid)
        return

    if not sala["ordem_turnos"]:
        return

    turno_token = sala["ordem_turnos"][sala["indice_turno"]]

    if token != turno_token:
        emit("erro_jogo", {"mensagem": "Ainda não é a sua vez."}, room=request.sid)
        return

    resposta = int(resposta_texto)
    correta = sala["pergunta_atual"]["resposta_correta"]
    nome_jogadora = sala["jogadores"][token]["nome"]

    # guarda a última resposta da jogadora na rodada
    sala["ultimas_respostas"][token] = resposta

    # mostra em tempo real a resposta dada, mas sem pontuar ainda
    sala["historico_respostas"].append({
        "nome": nome_jogadora,
        "resposta": resposta,
        "pontos_recebidos": 0
    })

    sala["mensagens"].append(f"{nome_jogadora} respondeu {resposta}.")

    # se acertou, pontua todo mundo com base na última resposta
    if resposta == correta:
        sala["mensagens"].append(f"{nome_jogadora} acertou a resposta da rodada.")

        # recalcula o histórico da rodada já com os pontos corretos
        novo_historico = []

        for token_jogadora, ultima_resposta in sala["ultimas_respostas"].items():
            pontos = calcular_pontos(ultima_resposta, correta)
            sala["jogadores"][token_jogadora]["pontos"] += pontos

            nome_atual = sala["jogadores"][token_jogadora]["nome"]

            novo_historico.append({
                "nome": nome_atual,
                "resposta": ultima_resposta,
                "pontos_recebidos": pontos
            })

            sala["mensagens"].append(
                f"{nome_atual} recebeu {pontos} pontos com base na última resposta {ultima_resposta}."
            )

        sala["historico_respostas"] = novo_historico

        emitir_estado_sala(codigo)
        finalizar_ou_continuar(codigo)
        return

    avancar_turno(codigo)
    emitir_estado_sala(codigo)


@socketio.on("voltar_para_espera")
def voltar_para_espera(dados):
    codigo = dados["codigo"]

    if codigo not in salas:
        return

    sala = salas[codigo]
    sala["em_jogo"] = False
    sala["em_jogo"] = False
    sala["rodada_atual"] = 0
    sala["fase"] = "espera"
    sala["pergunta_atual"] = None
    sala["historico_respostas"] = []
    sala["ultimas_respostas"] = {}
    sala["ordem_turnos"] = []
    sala["indice_turno"] = 0
    sala["ultimo_primeiro_token"] = ""
    sala["mostrar_ranking_final"] = False
    sala["rodadas_desempate_no_bloco"] = 0
    sala["mensagens"].append("As jogadoras voltaram para a sala de espera.")

    emitir_estado_sala(codigo)


@socketio.on("sair_da_sala")
def sair_da_sala_socket(dados):
    codigo = dados["codigo"]
    token = dados["token"]

    if codigo not in salas:
        return

    sala = salas[codigo]

    if token not in sala["jogadores"]:
        return

    nome = sala["jogadores"][token]["nome"]

    leave_room(codigo)
    if token in player_sid_map:
        del player_sid_map[token]

    del sala["jogadores"][token]
    sala["mensagens"].append(f"{nome} saiu da sala.")

    if len(sala["jogadores"]) == 0:
        del salas[codigo]
        return

    if sala["criador_token"] == token:
        novo_criador = list(sala["jogadores"].keys())[0]
        sala["criador_token"] = novo_criador
        sala["mensagens"].append(
            f"{sala['jogadores'][novo_criador]['nome']} agora é a criadora da sala."
        )

    # se a jogadora que saiu era a da vez e o jogo está ativo
    if sala["em_jogo"] and len(sala["ordem_turnos"]) > 0:
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
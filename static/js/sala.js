const socket = io();

const jogadorNome = JSON.parse(document.body.dataset.nome);
const salaCodigo = JSON.parse(document.body.dataset.codigo);
const jogadorToken = JSON.parse(document.body.dataset.token);

const listaJogadoras = document.getElementById("lista-jogadoras");
const listaPontuacao = document.getElementById("lista-pontuacao");
const criadorSala = document.getElementById("criador-sala");
const codigoSalaMenu = document.getElementById("codigo-sala-menu");
const chatSala = document.getElementById("chat-sala");
const historicoRespostas = document.getElementById("historico-respostas");
const telaEspera = document.getElementById("tela-espera");
const telaJogo = document.getElementById("tela-jogo");
const textoPergunta = document.getElementById("texto-pergunta");
const textoRodada = document.getElementById("texto-rodada");
const textoTurno = document.getElementById("texto-turno");
const campoResposta = document.getElementById("campo-resposta");
const botaoResponder = document.getElementById("botao-responder");
const botaoIniciar = document.getElementById("botao-iniciar");
const botaoSair = document.getElementById("botao-sair");
const mensagemErro = document.getElementById("mensagem-erro");
const avisoRodada = document.getElementById("aviso-rodada");
const rankingFinal = document.getElementById("ranking-final");
const listaRanking = document.getElementById("lista-ranking");
const botaoVoltarEspera = document.getElementById("botao-voltar-espera");
const botaoMenu = document.getElementById("botao-menu");
const dropdownMenu = document.getElementById("dropdown-menu");
const barraTempo = document.getElementById("barra-tempo");

let timerIntervalo = null;
let turnoAtualControlado = "";
let timerTimeout = null;

function mostrarErro(texto) {
    mensagemErro.textContent = texto;
    mensagemErro.style.display = "block";

    setTimeout(function () {
        mensagemErro.style.display = "none";
    }, 2500);
}

function ordenarPorChegada(jogadores) {
    return jogadores.slice().sort(function (a, b) {
        return a.ordem_chegada - b.ordem_chegada;
    });
}

function atualizarListaJogadoras(jogadores) {
    listaJogadoras.innerHTML = "";
    const lista = ordenarPorChegada(jogadores);

    for (let i = 0; i < lista.length; i++) {
        const li = document.createElement("li");
        li.textContent = lista[i].nome;
        listaJogadoras.appendChild(li);
    }
}

function atualizarPontuacao(jogadores, turnoToken) {
    listaPontuacao.innerHTML = "";
    const lista = ordenarPorChegada(jogadores);

    for (let i = 0; i < lista.length; i++) {
        const li = document.createElement("li");
        li.className = "item-placar";

        if (lista[i].token === turnoToken) {
            li.classList.add("ativo");
        }

        li.textContent = lista[i].nome + " - " + lista[i].pontos + " pts";
        listaPontuacao.appendChild(li);
    }
}

function atualizarMensagens(container, mensagens) {
    container.innerHTML = "";

    for (let i = 0; i < mensagens.length; i++) {
        const p = document.createElement("p");
        p.textContent = mensagens[i];
        container.appendChild(p);
    }

    container.scrollTop = container.scrollHeight;
}

function atualizarHistoricoRespostas(respostas) {
    historicoRespostas.innerHTML = "";

    for (let i = 0; i < respostas.length; i++) {
        const p = document.createElement("p");
        p.className = respostas[i].acertou ? "resposta-certa" : "resposta-errada";

        let textoResposta = respostas[i].resposta === null || respostas[i].resposta === undefined
            ? "sem resposta"
            : respostas[i].resposta;

        if (respostas[i].pontos_recebidos > 0) {
            p.textContent = respostas[i].nome + " respondeu " + textoResposta + " | +" + respostas[i].pontos_recebidos + " pts | " + respostas[i].frase;
        } else {
            p.textContent = respostas[i].nome + " respondeu " + textoResposta + " | " + respostas[i].frase;
        }

        historicoRespostas.appendChild(p);
    }

    historicoRespostas.scrollTop = historicoRespostas.scrollHeight;
}

function atualizarRanking(ranking) {
    listaRanking.innerHTML = "";

    for (let i = 0; i < ranking.length; i++) {
        const li = document.createElement("li");
        li.textContent = ranking[i].nome + " - " + ranking[i].pontos + " pts";
        listaRanking.appendChild(li);
    }
}

function pararTimer() {
    if (timerIntervalo !== null) {
        clearInterval(timerIntervalo);
        timerIntervalo = null;
    }

    if (timerTimeout !== null) {
        clearTimeout(timerTimeout);
        timerTimeout = null;
    }

    barraTempo.style.transition = "none";
    barraTempo.style.width = "100%";
    barraTempo.classList.remove("acabando");
}

function iniciarTimer(dados) {
    const chaveTurno = dados.turno_token + "-" + dados.rodada_atual + "-" + dados.historico_respostas.length;

    if (!dados.em_jogo || !dados.turno_token) {
        turnoAtualControlado = "";
        pararTimer();
        return;
    }

    if (turnoAtualControlado === chaveTurno) {
        return;
    }

    pararTimer();
    turnoAtualControlado = chaveTurno;

    const souDaVez = jogadorToken === dados.turno_token;
    const tempo = Number(dados.tempo_por_turno || 15);

    barraTempo.style.transition = "none";
    barraTempo.style.width = "100%";
    barraTempo.classList.remove("acabando");

    requestAnimationFrame(function () {
        requestAnimationFrame(function () {
            barraTempo.style.transition = "width " + tempo + "s linear";
            barraTempo.style.width = "0%";
        });
    });

    timerTimeout = setTimeout(function () {
        barraTempo.classList.add("acabando");

        if (souDaVez) {
            socket.emit("tempo_esgotado", {
                codigo: salaCodigo,
                token: jogadorToken
            });
        }
    }, tempo * 1000);
}

function atualizarTela(dados) {
    criadorSala.textContent = dados.criador_nome;
    codigoSalaMenu.textContent = dados.codigo;

    const souCriador = jogadorToken === dados.criador_token;

    if (dados.aviso_rodada) {
        avisoRodada.textContent = dados.aviso_rodada;
        avisoRodada.style.display = "block";
    } else {
        avisoRodada.style.display = "none";
    }

    if (!dados.em_jogo) {
        pararTimer();

        telaEspera.style.display = "block";
        telaJogo.style.display = "none";

        textoPergunta.textContent = "Aguardando início da partida";
        textoRodada.textContent = "";

        campoResposta.value = "";
        campoResposta.disabled = true;
        botaoResponder.disabled = true;

        atualizarListaJogadoras(dados.jogadores);
        atualizarMensagens(chatSala, dados.mensagens);
        historicoRespostas.innerHTML = "";
        listaPontuacao.innerHTML = "";

        if (dados.mostrar_ranking_final) {
            rankingFinal.style.display = "block";
            atualizarRanking(dados.ranking);
            botaoIniciar.style.display = "none";
        } else {
            rankingFinal.style.display = "none";
            botaoIniciar.style.display = souCriador && dados.jogadores.length >= 2 ? "inline-block" : "none";
        }

        return;
    }

    telaEspera.style.display = "none";
    telaJogo.style.display = "block";
    rankingFinal.style.display = "none";
    botaoIniciar.style.display = "none";

    textoPergunta.textContent = dados.pergunta_atual;
    textoRodada.textContent = "Rodada " + dados.rodada_atual + " de " + dados.total_rodadas;
    textoTurno.textContent = "Vez de: " + dados.turno_nome;

    atualizarPontuacao(dados.jogadores, dados.turno_token);
    atualizarHistoricoRespostas(dados.historico_respostas);

    const minhaVez = jogadorToken === dados.turno_token;
    campoResposta.disabled = !minhaVez;
    botaoResponder.disabled = !minhaVez;

    if (minhaVez) {
        campoResposta.focus();
    } else {
        campoResposta.value = "";
    }

    iniciarTimer(dados);
}

socket.on("connect", function () {
    socket.emit("entrar_socket_sala", {
        codigo: salaCodigo,
        token: jogadorToken
    });
});

socket.on("estado_sala", function (dados) {
    atualizarTela(dados);
});

socket.on("erro_jogo", function (dados) {
    mostrarErro(dados.mensagem);
});

botaoResponder.addEventListener("click", function () {
    const resposta = campoResposta.value.trim();

    socket.emit("enviar_resposta", {
        codigo: salaCodigo,
        token: jogadorToken,
        resposta: resposta
    });

    campoResposta.value = "";
});

campoResposta.addEventListener("keydown", function (evento) {
    if (evento.key === "Enter" && !botaoResponder.disabled) {
        botaoResponder.click();
    }
});

botaoIniciar.addEventListener("click", function () {
    socket.emit("iniciar_partida", {
        codigo: salaCodigo,
        token: jogadorToken
    });
});

botaoSair.addEventListener("click", function () {
    const confirmar = confirm("Deseja realmente sair da sala?");

    if (confirmar) {
        socket.emit("sair_da_sala", {
            codigo: salaCodigo,
            token: jogadorToken
        });

        window.location.href = "/lobby";
    }
});

botaoVoltarEspera.addEventListener("click", function () {
    socket.emit("voltar_para_espera", {
        codigo: salaCodigo
    });
});

botaoMenu.addEventListener("click", function () {
    dropdownMenu.style.display = dropdownMenu.style.display === "none" ? "block" : "none";
});
